//! Fetching TMDB recommendations and similar titles into affinity edges
//! (`plexdb/tmdb_edges.py`).
//!
//! Two edge types, one TMDB source, kept apart because they measure different
//! things: `tmdb_recommendations` is behavioural, `tmdb_similar` is content-derived.
//! A re-pull replaces the whole `(from_id, edge_type)` set, so an edge the source
//! dropped disappears rather than going stale. Rank is TMDB's own array position,
//! 1-indexed, stored verbatim.
//!
//! **Only edges between two titles this store already knows are representable.**
//! `edges.from_id` and `edges.to_id` both reference `items`, which only `plexdb walk`
//! fills (ADR-0005). A recommendation pointing at a title this library never walked
//! is counted in `edges_skipped_not_in_library` and dropped, not stored under an
//! invented id that a later walk could derive differently (ADR-0002).
//!
//! **Caching needs a cursor `edges` rows cannot supply**: a title whose
//! recommendations are all outside the library leaves no `edges` row to read a
//! `fetched_at` off. `enrichment_cursor` holds a per-`(item_id, edge_type)` row for
//! it, under the `tmdb_edges` namespace and the `tmdb` source (ADR-0013).

use std::collections::{HashMap, HashSet};

use rusqlite::{Connection, TransactionBehavior};

use crate::cursors::{delete_cursors, load_fetched, write_fetched};
use crate::enrich_tmdb::SweepError;
use crate::staleness::{Now, is_stale};
use crate::tmdb::TmdbSource;
use crate::tmdb_common::{Candidate, FailureRun, candidates, media_type_for};

pub const RECOMMENDATIONS_EDGE_TYPE: &str = "tmdb_recommendations";
pub const SIMILAR_EDGE_TYPE: &str = "tmdb_similar";

const CURSOR_NAMESPACE: &str = "tmdb_edges";
const CURSOR_SOURCE: &str = "tmdb";

fn cursor_key(edge_type: &str) -> &'static str {
    if edge_type == RECOMMENDATIONS_EDGE_TYPE {
        "fetched_recommendations"
    } else {
        "fetched_similar"
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct EdgeStats {
    pub titles_seen: usize,
    pub titles_fetched: usize,
    pub titles_cached: usize,
    pub titles_skipped_no_tmdb_id: usize,
    pub titles_failed: usize,
    pub edges_written: usize,
    /// A related TMDB id this sweep could not turn into a stored edge: no matching
    /// item in this store, or a title pointing at itself.
    pub edges_skipped_not_in_library: usize,
}

/// Delete every `edges` row and cursor row for one TMDB edge type, leaving every
/// other edge type and enrichment namespace untouched. Returns
/// `(edges_removed, cursor_rows_removed)`.
pub fn wipe_edge_type(conn: &mut Connection, edge_type: &str) -> rusqlite::Result<(usize, usize)> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let edges = tx.execute("DELETE FROM edges WHERE edge_type = ?", [edge_type])?;
    let cursors = delete_cursors(
        &tx,
        CURSOR_NAMESPACE,
        CURSOR_SOURCE,
        Some(cursor_key(edge_type)),
    )?;
    tx.commit()?;
    Ok((edges, cursors))
}

/// tmdb id -> local item_id: the reverse index every fetched TMDB id is resolved
/// through. An id absent from here is a title nobody walked.
fn tmdb_to_item(conn: &Connection) -> rusqlite::Result<HashMap<String, String>> {
    conn.prepare("SELECT item_id, value FROM external_ids WHERE ns = 'tmdb'")?
        .query_map([], |row| {
            Ok((row.get::<_, String>(1)?, row.get::<_, String>(0)?))
        })?
        .collect()
}

type Fetch<'a> = &'a dyn Fn(&str, &str) -> Result<Vec<String>, crate::tmdb::TmdbError>;

fn sweep(
    conn: &mut Connection,
    fetch: Fetch,
    edge_type: &str,
    stale_days: i64,
    tmdb_to_item: &HashMap<String, String>,
    candidates: &[Candidate],
) -> Result<EdgeStats, SweepError> {
    let now = Now::read();
    let cutoff = now.cutoff(stale_days);
    let now_iso = now.iso();
    let mut stats = EdgeStats::default();
    let key = cursor_key(edge_type);
    let cached = load_fetched(conn, CURSOR_NAMESPACE, CURSOR_SOURCE, key)?;
    let mut run = FailureRun::default();

    for row in candidates {
        stats.titles_seen += 1;
        let (Some(media_type), Some(tmdb_id)) = (media_type_for(&row.item_type), &row.tmdb_id)
        else {
            stats.titles_skipped_no_tmdb_id += 1;
            continue;
        };
        if let Some(fetched_at) = cached.get(&row.item_id)
            && !is_stale(fetched_at, cutoff)?
        {
            stats.titles_cached += 1;
            continue;
        }
        let related = match fetch(tmdb_id, media_type) {
            Ok(related) => related,
            Err(err) => {
                stats.titles_failed += 1;
                run.failed(stats.titles_seen, stats.titles_failed, &err)?;
                continue;
            }
        };
        run.succeeded();

        // Rank is array position, 1-indexed. A target outside the library, or a title
        // recommending itself, is dropped and counted. `seen` guards two TMDB ids
        // landing on one item_id, which would insert one `(from, to, type)` key twice.
        let mut edges: Vec<(&str, i64)> = Vec::new();
        let mut seen: HashSet<&str> = HashSet::new();
        for (index, related_id) in related.iter().enumerate() {
            let position = i64::try_from(index + 1).unwrap_or(i64::MAX);
            match tmdb_to_item.get(related_id) {
                Some(to) if *to != row.item_id => {
                    if seen.insert(to) {
                        edges.push((to, position));
                    }
                }
                _ => stats.edges_skipped_not_in_library += 1,
            }
        }

        let tx = conn.transaction()?;
        tx.execute(
            "DELETE FROM edges WHERE from_id = ? AND edge_type = ?",
            [&row.item_id, edge_type],
        )?;
        for (to, rank) in &edges {
            tx.execute(
                "INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) \
                 VALUES (?, ?, ?, ?, ?)",
                rusqlite::params![row.item_id, to, edge_type, rank, now_iso],
            )?;
        }
        write_fetched(
            &tx,
            CURSOR_NAMESPACE,
            CURSOR_SOURCE,
            &row.item_id,
            &now_iso,
            key,
        )?;
        tx.commit()?;
        stats.titles_fetched += 1;
        stats.edges_written += edges.len();
    }
    Ok(stats)
}

/// Refresh both TMDB edge types, in `(edge_type, stats)` order. Each is swept
/// independently — a failure in one does not touch the other's cursor or rows — but
/// the two read-only prefetch queries are shared rather than run twice.
pub fn refresh_tmdb_edges(
    conn: &mut Connection,
    source: &dyn TmdbSource,
    stale_days: i64,
) -> Result<Vec<(&'static str, EdgeStats)>, SweepError> {
    let index = tmdb_to_item(conn)?;
    let walked = candidates(conn, false)?;
    let recommendations = sweep(
        conn,
        &|id, media| source.recommendations(id, media),
        RECOMMENDATIONS_EDGE_TYPE,
        stale_days,
        &index,
        &walked,
    )?;
    let similar = sweep(
        conn,
        &|id, media| source.similar(id, media),
        SIMILAR_EDGE_TYPE,
        stale_days,
        &index,
        &walked,
    )?;
    Ok(vec![
        (RECOMMENDATIONS_EDGE_TYPE, recommendations),
        (SIMILAR_EDGE_TYPE, similar),
    ])
}
