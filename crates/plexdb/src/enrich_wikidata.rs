//! Fetching what Wikidata states about each title into `keywords`, `awards` and
//! `keyword_roles`, cached per title (`plexdb/enrich_wikidata.py`).
//!
//! Four properties become keywords in the shared `keywords` namespace under
//! `source = 'wikidata'` (ADR-0016): narrative location (P840), set in period (P2408), main
//! subject (P921) and genre (P136). Each label goes through the same normalization TMDB's
//! keywords do. Two of those properties also say what the keyword *is*: a narrative location is
//! a place, and a period is a time. Those become `keyword_roles` rows with role `region` and
//! `era`, `source = 'wikidata'`, and score, model and error NULL (ADR-0019).
//!
//! Which title stated a role is kept per title in `keyword_role_statements`, replaced with the
//! title's other rows on each fetch. Each run ends by deleting every wikidata role row no
//! statement backs any more, read from the whole stored snapshot, cached titles included, so a
//! partial or failed run prunes correctly.
//!
//! Award received (P166) is not a keyword. Its labels go verbatim into their own `awards`
//! namespace, key `award`. One `enrichment_cursor` row per title (namespace `keywords`, source
//! `wikidata`) covers both namespaces, and is written even when Wikidata has nothing (ADR-0013).
//! Titles are asked in batches, one SPARQL query each; a batch that fails writes nothing for any
//! of its titles, so a re-run retries them.

use std::collections::{BTreeSet, HashMap};

use plexdb_keywords::{NAMESPACE, RawKeyword, py_strip, state_role, write_title_keywords};
use rusqlite::{Connection, TransactionBehavior};

use crate::cursors::{FETCHED_KEY, delete_cursors, load_fetched, write_fetched};
use crate::staleness::{Now, is_stale};
use crate::sweep::{SweepError, abort_message};
use crate::wikidata::{
    AWARD_RECEIVED, GENRE, MAIN_SUBJECT, NARRATIVE_LOCATION, SET_IN_PERIOD, WikidataSource,
    is_imdb_id,
};

/// This writer's source name, in `keywords`, `awards`, `keyword_roles` and
/// `enrichment_cursor`.
pub const SOURCE: &str = "wikidata";
pub const AWARDS_NAMESPACE: &str = "awards";
const AWARD_KEY: &str = "award";

/// Properties whose labels become keywords, and the role each one states, if any.
const KEYWORD_PROPERTIES: [(&str, Option<&str>); 4] = [
    (NARRATIVE_LOCATION, Some("region")),
    (SET_IN_PERIOD, Some("era")),
    (MAIN_SUBJECT, None),
    (GENRE, None),
];

/// IMDb ids per SPARQL query: half of what answered in 4.9 s, so a slow day stays an order of
/// magnitude inside the 60 s deadline.
pub const BATCH_SIZE: usize = 200;

/// Failed batches in a row that abort the sweep.
pub const MAX_CONSECUTIVE_FAILURES: usize = 3;

/// What one sweep touched: the summary `plexdb enrich-wikidata` prints.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct WikidataStats {
    pub titles_seen: usize,
    pub titles_fetched: usize,
    pub titles_cached: usize,
    /// Movies and shows with no usable `imdb` row in `external_ids`: skipped, not an error. A
    /// value that is not `tt` plus digits counts here too.
    pub titles_skipped_no_imdb_id: usize,
    /// Fetched titles Wikidata stated at least one of the five properties for.
    pub titles_matched: usize,
    /// Titles in a batch whose query failed. Nothing is written for them.
    pub titles_failed: usize,
    pub queries_sent: usize,
    pub keywords_written: usize,
    /// Distinct (keyword, role) rows this run stated or restated.
    pub roles_stated: usize,
    /// Role rows deleted because no title states them any more.
    pub roles_pruned: usize,
    pub awards_written: usize,
}

/// Delete everything this source owns: its keywords, its awards, its role rows and its
/// cursors. Another source's keywords and roles, and every other namespace, stay. Returns the
/// number of rows removed.
pub fn wipe(conn: &mut Connection) -> rusqlite::Result<usize> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut removed = tx.execute(
        "DELETE FROM enrichment WHERE namespace IN (?, ?) AND source = ?",
        [NAMESPACE, AWARDS_NAMESPACE, SOURCE],
    )?;
    tx.execute("DELETE FROM keyword_surfaces WHERE source = ?", [SOURCE])?;
    removed += tx.execute("DELETE FROM keyword_roles WHERE source = ?", [SOURCE])?;
    removed += tx.execute(
        "DELETE FROM keyword_role_statements WHERE source = ?",
        [SOURCE],
    )?;
    removed += delete_cursors(&tx, NAMESPACE, SOURCE, None)?;
    tx.commit()?;
    Ok(removed)
}

/// Joins on item_id so SQLite searches idx_external_ids_item; without that term it scans every
/// imdb row for each title.
const CANDIDATES_SQL: &str = "SELECT i.item_id AS item_id, MIN(e.value) AS imdb_id \
    FROM items i \
    LEFT JOIN external_ids e ON e.item_id = i.item_id AND e.ns = 'imdb' \
    WHERE i.type IN ('movie', 'show') \
    GROUP BY i.item_id \
    ORDER BY i.item_id";

/// Ask Wikidata about every walked movie/show with an `imdb` external id whose cursor is
/// missing or stale, `batch_size` titles per query.
pub fn enrich_wikidata(
    conn: &mut Connection,
    source: &mut dyn WikidataSource,
    stale_days: i64,
    batch_size: usize,
) -> Result<WikidataStats, SweepError> {
    let cutoff = Now::read().cutoff(stale_days);
    let mut stats = WikidataStats::default();

    let cached = load_fetched(conn, NAMESPACE, SOURCE, FETCHED_KEY)?;
    let candidates: Vec<(String, Option<String>)> = conn
        .prepare(CANDIDATES_SQL)?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;

    let mut due: Vec<(String, String)> = Vec::new();
    for (item_id, imdb_id) in candidates {
        stats.titles_seen += 1;
        let Some(imdb_id) = imdb_id.filter(|id| is_imdb_id(id)) else {
            stats.titles_skipped_no_imdb_id += 1;
            continue;
        };
        if let Some(fetched_at) = cached.get(&item_id)
            && !is_stale(fetched_at, cutoff)?
        {
            stats.titles_cached += 1;
            continue;
        }
        due.push((item_id, imdb_id));
    }

    let mut stated: BTreeSet<(String, String)> = BTreeSet::new();
    let swept = sweep(conn, source, &due, batch_size, &mut stats, &mut stated);
    // The prune reads every stored title, so an aborted run prunes as correctly as a finished
    // one.
    stats.roles_pruned = prune_roles(conn)?;
    swept?;
    stats.roles_stated = stated.len();
    Ok(stats)
}

fn sweep(
    conn: &mut Connection,
    source: &mut dyn WikidataSource,
    due: &[(String, String)],
    batch_size: usize,
    stats: &mut WikidataStats,
    stated: &mut BTreeSet<(String, String)>,
) -> Result<(), SweepError> {
    let mut consecutive_failures = 0;
    for batch in due.chunks(batch_size.max(1)) {
        let mut imdb_ids: Vec<String> = Vec::new();
        for (_, imdb_id) in batch {
            if !imdb_ids.contains(imdb_id) {
                imdb_ids.push(imdb_id.clone());
            }
        }
        stats.queries_sent += 1;
        let statements = match source.statements(&imdb_ids) {
            Ok(statements) => statements,
            Err(err) => {
                stats.titles_failed += batch.len();
                consecutive_failures += 1;
                println!("wikidata: batch of {} failed: {err}", batch.len());
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES {
                    return Err(SweepError::Source(abort_message(
                        MAX_CONSECUTIVE_FAILURES,
                        "queries",
                        stats.titles_fetched,
                        stats.titles_failed,
                        &err,
                    )));
                }
                continue;
            }
        };
        consecutive_failures = 0;
        let mut by_imdb: HashMap<String, Vec<(String, String)>> = HashMap::new();
        for (imdb_id, prop, label) in statements {
            by_imdb.entry(imdb_id).or_default().push((prop, label));
        }
        write_batch(conn, batch, &by_imdb, &Now::read().iso(), stats, stated)?;
    }
    Ok(())
}

/// Delete every wikidata role row no title's statement backs. Returns the number removed.
fn prune_roles(conn: &mut Connection) -> rusqlite::Result<usize> {
    let tx = conn.transaction()?;
    let removed = tx.execute(
        "DELETE FROM keyword_roles WHERE source = ? AND NOT EXISTS (\
         SELECT 1 FROM keyword_role_statements s WHERE s.source = keyword_roles.source \
         AND s.keyword = keyword_roles.keyword AND s.role = keyword_roles.role)",
        [SOURCE],
    )?;
    tx.commit()?;
    Ok(removed)
}

/// Replace every title's rows in one transaction, cursors included, so an interrupted sweep
/// never leaves a cursor vouching for rows already deleted.
fn write_batch(
    conn: &mut Connection,
    batch: &[(String, String)],
    by_imdb: &HashMap<String, Vec<(String, String)>>,
    now_iso: &str,
    stats: &mut WikidataStats,
    stated: &mut BTreeSet<(String, String)>,
) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    for (item_id, imdb_id) in batch {
        tx.execute(
            "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ?",
            [item_id.as_str(), AWARDS_NAMESPACE, SOURCE],
        )?;
        tx.execute(
            "DELETE FROM keyword_role_statements WHERE item_id = ? AND source = ?",
            [item_id.as_str(), SOURCE],
        )?;
        write_fetched(&tx, NAMESPACE, SOURCE, item_id, now_iso, FETCHED_KEY)?;
        let mut labels: Vec<(String, Option<&str>)> = Vec::new();
        let mut awards: Vec<String> = Vec::new();
        for (prop, label) in by_imdb.get(imdb_id).into_iter().flatten() {
            let label = py_strip(label);
            if label.is_empty() {
                continue;
            }
            if prop == AWARD_RECEIVED {
                if !awards.iter().any(|award| award == label) {
                    awards.push(label.to_string());
                }
            } else if let Some((_, role)) = KEYWORD_PROPERTIES.iter().find(|(p, _)| p == prop) {
                labels.push((label.to_string(), *role));
            }
        }
        let stored = write_title_keywords(
            &tx,
            item_id,
            SOURCE,
            labels.iter().map(|(label, _)| RawKeyword::plain(label)),
            now_iso,
        )?;
        let mut stating: Vec<(String, &str)> = Vec::new();
        for (label, role) in &labels {
            let (Some(keyword), Some(role)) = (stored.get(label), role) else {
                continue;
            };
            state_role(&tx, keyword, role, SOURCE, now_iso)?;
            stated.insert((keyword.clone(), role.to_string()));
            if !stating.iter().any(|(k, r)| k == keyword && r == role) {
                stating.push((keyword.clone(), role));
            }
        }
        let keywords: BTreeSet<&String> = stored.values().collect();
        for (keyword, role) in &stating {
            tx.execute(
                "INSERT INTO keyword_role_statements (item_id, keyword, role, source, stated_at) \
                 VALUES (?, ?, ?, ?, ?)",
                [item_id.as_str(), keyword, role, SOURCE, now_iso],
            )?;
        }
        for award in &awards {
            tx.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
                 VALUES (?, ?, ?, ?, ?, ?)",
                [
                    item_id.as_str(),
                    AWARDS_NAMESPACE,
                    SOURCE,
                    AWARD_KEY,
                    award,
                    now_iso,
                ],
            )?;
        }
        stats.titles_fetched += 1;
        stats.keywords_written += keywords.len();
        stats.awards_written += awards.len();
        if !keywords.is_empty() || !awards.is_empty() {
            stats.titles_matched += 1;
        }
    }
    tx.commit()
}
