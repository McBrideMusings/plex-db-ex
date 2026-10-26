//! Fetching TMDB keywords into the `keywords` enrichment namespace, cached
//! (`plexdb/enrich_tmdb.py`, ADR-0016).
//!
//! This module owns one `source` within the shared namespace, `tmdb`, and never
//! touches another source's rows. A title's row set under this source is a
//! snapshot: on a fresh fetch the whole set for that title and source is replaced.
//! Every keyword is normalized before it is stored and every raw spelling TMDB
//! returned is kept in `keyword_surfaces` and `keyword_forms`.
//!
//! Caching is the substance of this module: a title inside its staleness threshold
//! is never asked of TMDB at all. A title with no keywords leaves no `enrichment`
//! row to read a timestamp off, so a per-title cursor row in `enrichment_cursor`
//! (ADR-0013) makes zero keywords just as cacheable as twenty.

use std::collections::BTreeSet;

use plexdb_keywords::{NAMESPACE, RawKeyword, write_title_keywords};
use rusqlite::{Connection, TransactionBehavior};

use crate::cursors::{FETCHED_KEY, delete_cursors, load_fetched, write_fetched};
use crate::staleness::{Now, is_stale};
use crate::tmdb::{TmdbError, TmdbSource};
use crate::tmdb_common::{FailureRun, candidates, media_type_for};

/// This writer's source name within the `keywords` namespace.
pub const SOURCE: &str = "tmdb";

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct EnrichStats {
    pub titles_seen: usize,
    pub titles_fetched: usize,
    pub titles_cached: usize,
    /// Movies and shows with no `tmdb` row in `external_ids`: skipped, not an error.
    pub titles_skipped_no_tmdb_id: usize,
    /// A single title's fetch failed. Nothing is written for it, so a re-run retries
    /// it; `MAX_CONSECUTIVE_FAILURES` in a row abort the sweep.
    pub titles_failed: usize,
    pub keywords_written: usize,
}

/// Why a sweep stopped.
#[derive(Debug, thiserror::Error)]
pub enum SweepError {
    #[error(transparent)]
    Tmdb(#[from] TmdbError),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Stale(#[from] crate::staleness::BadTime),
}

/// Delete every `tmdb`-sourced `keywords` row **and** this module's fetch cursors,
/// leaving every other source's keywords and every other namespace untouched.
/// Returns the number of rows removed.
///
/// Both tables, because they hold one module's state split across two places
/// (ADR-0013): wiping the keywords and keeping the cursors would leave every title
/// looking fetched and empty, so `--rewipe` would fetch nothing.
pub fn wipe_namespace(conn: &mut Connection) -> rusqlite::Result<usize> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut removed = tx.execute(
        "DELETE FROM enrichment WHERE namespace = ? AND source = ?",
        [NAMESPACE, SOURCE],
    )?;
    tx.execute("DELETE FROM keyword_surfaces WHERE source = ?", [SOURCE])?;
    removed += delete_cursors(&tx, NAMESPACE, SOURCE, None)?;
    tx.commit()?;
    Ok(removed)
}

/// Fetch TMDB keywords for every walked movie and show carrying a `tmdb` external id.
///
/// Each title's row set is committed on its own, so a sweep interrupted partway
/// through a large library keeps everything it already fetched. A single title's
/// error is counted in `titles_failed` and the sweep moves on; the cursor and the
/// keywords it vouches for are replaced in one transaction, together or not at all.
pub fn enrich_tmdb_keywords(
    conn: &mut Connection,
    source: &dyn TmdbSource,
    stale_days: i64,
) -> Result<EnrichStats, SweepError> {
    let now = Now::read();
    let cutoff = now.cutoff(stale_days);
    let now_iso = now.iso();
    let mut stats = EnrichStats::default();
    let cached = load_fetched(conn, NAMESPACE, SOURCE, FETCHED_KEY)?;
    let mut run = FailureRun::default();

    for row in candidates(conn, false)? {
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
        let keywords = match source.keywords(tmdb_id, media_type) {
            Ok(keywords) => keywords,
            Err(err) => {
                stats.titles_failed += 1;
                run.failed(stats.titles_seen, stats.titles_failed, &err)?;
                continue;
            }
        };
        run.succeeded();

        let tx = conn.transaction()?;
        write_fetched(&tx, NAMESPACE, SOURCE, &row.item_id, &now_iso, FETCHED_KEY)?;
        let stored = write_title_keywords(
            &tx,
            &row.item_id,
            SOURCE,
            keywords.into_iter().map(RawKeyword::plain),
            &now_iso,
        )?;
        tx.commit()?;
        stats.titles_fetched += 1;
        stats.keywords_written += stored.values().collect::<BTreeSet<_>>().len();
    }
    Ok(stats)
}
