//! Fetching each movie's and show's original language from TMDB, cached
//! (`plexdb/enrich_tmdb_language.py`).
//!
//! TMDB's details endpoint names the language a title was made in as an ISO 639-1
//! code. It becomes one row per title in `enrichment`, namespace `tmdb`, source
//! `tmdb`, key `original_language`, value the code exactly as TMDB sent it. An
//! episode carries no row of its own: its language is its show's.
//!
//! Caching follows `enrich_tmdb`: one `enrichment_cursor` row per title (namespace
//! `tmdb`, source `tmdb`, key `fetched`) makes a title TMDB gave no language for just
//! as cacheable as one it did (ADR-0013).

use rusqlite::Connection;

use crate::cursors::{FETCHED_KEY, delete_cursors, load_fetched, write_fetched};
use crate::enrich_tmdb::SweepError;
use crate::staleness::{Now, is_stale};
use crate::tmdb::TmdbSource;
use crate::tmdb_common::{FailureRun, candidates, media_type_for};

/// The namespace this writer fills, in `enrichment` and `enrichment_cursor`.
pub const NAMESPACE: &str = "tmdb";
/// This writer's source name within it.
pub const SOURCE: &str = "tmdb";
pub const KEY: &str = "original_language";

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LanguageStats {
    pub titles_seen: usize,
    pub titles_fetched: usize,
    pub titles_cached: usize,
    pub titles_skipped_no_tmdb_id: usize,
    pub titles_failed: usize,
    /// Fetched titles TMDB named no language for: cached, but no row written.
    pub titles_without_language: usize,
    pub languages_written: usize,
}

/// Delete this writer's `original_language` rows and its fetch cursors, and nothing
/// else under `tmdb`. Returns the number of rows removed.
pub fn wipe(conn: &mut Connection) -> rusqlite::Result<usize> {
    let tx = conn.transaction()?;
    let mut removed = tx.execute(
        "DELETE FROM enrichment WHERE namespace = ? AND source = ? AND key = ?",
        [NAMESPACE, SOURCE, KEY],
    )?;
    removed += delete_cursors(&tx, NAMESPACE, SOURCE, None)?;
    tx.commit()?;
    Ok(removed)
}

/// Record the original language of every walked movie and show carrying a `tmdb`
/// external id. A title inside its staleness threshold is never asked of `source`.
pub fn enrich_tmdb_language(
    conn: &mut Connection,
    source: &dyn TmdbSource,
    stale_days: i64,
) -> Result<LanguageStats, SweepError> {
    let now = Now::read();
    let cutoff = now.cutoff(stale_days);
    let now_iso = now.iso();
    let mut stats = LanguageStats::default();
    let cached = load_fetched(conn, NAMESPACE, SOURCE, FETCHED_KEY)?;
    let mut run = FailureRun::default();

    for row in candidates(conn, true)? {
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
        let language = match source.original_language(tmdb_id, media_type) {
            Ok(language) => language,
            Err(err) => {
                stats.titles_failed += 1;
                run.failed(stats.titles_seen, stats.titles_failed, &err)?;
                continue;
            }
        };
        run.succeeded();
        // An empty code is no language, as `if language:` reads it.
        let language = language.filter(|code| !code.is_empty());

        let tx = conn.transaction()?;
        tx.execute(
            "DELETE FROM enrichment \
             WHERE item_id = ? AND namespace = ? AND source = ? AND key = ?",
            [&row.item_id, NAMESPACE, SOURCE, KEY],
        )?;
        if let Some(language) = &language {
            tx.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
                 VALUES (?, ?, ?, ?, ?, ?)",
                [&row.item_id, NAMESPACE, SOURCE, KEY, language, &now_iso],
            )?;
        }
        write_fetched(&tx, NAMESPACE, SOURCE, &row.item_id, &now_iso, FETCHED_KEY)?;
        tx.commit()?;
        stats.titles_fetched += 1;
        if language.is_some() {
            stats.languages_written += 1;
        } else {
            stats.titles_without_language += 1;
        }
    }
    Ok(stats)
}
