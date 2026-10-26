//! Scraping each movie's Letterboxd themes into `keywords`, cached per title
//! (`plexdb/enrich_letterboxd.py`).
//!
//! Every theme and mini-theme label on a film's page becomes a keyword in the shared `keywords`
//! namespace under `source = 'letterboxd'` (ADR-0016), through the same normalization TMDB's
//! keywords take. Letterboxd ranks nothing and states no role, so `rank` stays NULL and no
//! `keyword_roles` row is written. Movies only: the lookup goes through a TMDB *movie* id.
//!
//! Each title's rows under this source are a snapshot. One `enrichment_cursor` row per title
//! (namespace `keywords`, source `letterboxd`, key `fetched`) is written for a film page that
//! parsed, even with no themes, and for a movie Letterboxd does not list (ADR-0013).
//!
//! There is no API contract, so two things are kept apart from a request failing:
//!
//! - A film page without the film marker is a **parse failure**. It writes no keyword and no
//!   `fetched` cursor, so the next sweep asks again. When parse failures pass
//!   [`MAX_PARSE_FAILURE_SHARE`] of the film pages fetched, the command exits non-zero after
//!   printing the count, so a layout change shows in the sweep summary instead of as empty
//!   theme sets.
//! - A failed request writes no keyword and no `fetched` cursor either; three in a row abort
//!   the run.
//!
//! Both record only an `attempted` cursor, which is scheduling, not a fact. A run fetches at
//! most `limit` titles: never-attempted titles first in `item_id` order, then attempted ones,
//! oldest attempt first, so the first pass over the library spreads across several sweeps and a
//! title that keeps failing never holds the cap.

use plexdb_keywords::{NAMESPACE, RawKeyword, write_title_keywords};
use rusqlite::{Connection, TransactionBehavior};

use crate::cursors::{FETCHED_KEY, delete_cursors, load_cursors, mark_attempted, write_fetched};
use crate::letterboxd::{LbThemesSource, Lookup, Outcome, is_tmdb_id};
use crate::staleness::{Now, is_stale};
use crate::sweep::{SweepError, abort_message};

/// This writer's source name, in `keywords` and `enrichment_cursor`.
pub const SOURCE: &str = "letterboxd";

/// Titles one run fetches at most: two requests and about 2.5 s per title, so one run takes
/// about 40 minutes and the ~11,700 movies with a TMDB id are covered in about 12 sweeps.
pub const DEFAULT_MAX_TITLES: i64 = 1000;

/// Share of film pages fetched that may lack the film marker before the run exits non-zero.
pub const MAX_PARSE_FAILURE_SHARE: f64 = 0.10;

/// Failed titles in a row that abort the run.
pub const MAX_CONSECUTIVE_FAILURES: usize = 3;

/// What one run touched: the summary `plexdb enrich-letterboxd` prints.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LetterboxdStats {
    pub titles_seen: usize,
    pub titles_cached: usize,
    /// Movies with no usable `tmdb` row in `external_ids`: skipped, not an error.
    pub titles_skipped_no_tmdb_id: usize,
    /// Due titles past the per-run cap, left for the next run.
    pub titles_capped: usize,
    /// Titles that got a cursor this run: listed and parsed, or not listed.
    pub titles_fetched: usize,
    pub titles_not_listed: usize,
    /// Parsed film pages carrying at least one theme.
    pub titles_matched: usize,
    /// Film pages without the marker. Nothing is written for them.
    pub parse_failures: usize,
    /// Titles whose request failed. Nothing is written for them.
    pub titles_failed: usize,
    pub keywords_written: usize,
}

impl LetterboxdStats {
    /// Film pages that loaded, parsed or not: the base of the failure share.
    pub fn pages_fetched(&self) -> usize {
        self.titles_fetched - self.titles_not_listed + self.parse_failures
    }

    pub fn parse_failed(&self) -> bool {
        let pages = self.pages_fetched();
        pages > 0 && self.parse_failures as f64 / pages as f64 > MAX_PARSE_FAILURE_SHARE
    }
}

/// Delete this source's keywords and cursors. Every other source and namespace stays. Returns
/// the number of rows removed.
pub fn wipe(conn: &mut Connection) -> rusqlite::Result<usize> {
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

/// Look up every walked movie with a `tmdb` external id whose cursor is missing or stale, at
/// most `limit` of them: never-attempted titles first in `item_id` order, then attempted ones,
/// oldest attempt first.
pub fn enrich_letterboxd(
    conn: &mut Connection,
    source: &mut dyn LbThemesSource,
    stale_days: i64,
    limit: usize,
) -> Result<LetterboxdStats, SweepError> {
    let cutoff = Now::read().cutoff(stale_days);
    let mut stats = LetterboxdStats::default();

    let cursors = load_cursors(conn, NAMESPACE, SOURCE)?;
    // A join, not a correlated subquery: the planner answers `e.ns = 'tmdb'` inside a subquery
    // from the (ns, value, kind) key and scans every tmdb row once per movie, which takes
    // minutes on the real store against 0.06 s for this.
    let candidates: Vec<(String, Option<String>)> = conn
        .prepare(
            "SELECT i.item_id AS item_id, MIN(e.value) AS tmdb_id \
             FROM items i \
             LEFT JOIN external_ids e ON e.item_id = i.item_id AND e.ns = 'tmdb' \
             WHERE i.type = 'movie' \
             GROUP BY i.item_id \
             ORDER BY i.item_id",
        )?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;

    let mut due: Vec<(String, String)> = Vec::new();
    for (item_id, tmdb_id) in candidates {
        stats.titles_seen += 1;
        let Some(tmdb_id) = tmdb_id.filter(|id| is_tmdb_id(id)) else {
            stats.titles_skipped_no_tmdb_id += 1;
            continue;
        };
        if let Some(fetched_at) = cursors.fetched.get(&item_id)
            && !is_stale(fetched_at, cutoff)?
        {
            stats.titles_cached += 1;
            continue;
        }
        due.push((item_id, tmdb_id));
    }
    // Stable, so titles never attempted keep their `item_id` order.
    due.sort_by_cached_key(|(item_id, _)| cursors.order_key(item_id));
    stats.titles_capped = due.len().saturating_sub(limit);

    let mut consecutive_failures = 0;
    for (item_id, tmdb_id) in due.iter().take(limit) {
        let found = match source.lookup(tmdb_id) {
            Ok(found) => found,
            Err(err) => {
                stats.titles_failed += 1;
                consecutive_failures += 1;
                mark_attempted(
                    conn,
                    NAMESPACE,
                    SOURCE,
                    &[item_id.as_str()],
                    &Now::read().iso_micros(),
                )?;
                println!("letterboxd: tmdb {tmdb_id} failed: {err}");
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES {
                    return Err(SweepError::Source(abort_message(
                        MAX_CONSECUTIVE_FAILURES,
                        "titles",
                        stats.titles_fetched,
                        stats.titles_failed,
                        &err,
                    )));
                }
                continue;
            }
        };
        consecutive_failures = 0;
        if found.outcome == Outcome::Unparsed {
            stats.parse_failures += 1;
            mark_attempted(
                conn,
                NAMESPACE,
                SOURCE,
                &[item_id.as_str()],
                &Now::read().iso_micros(),
            )?;
            continue;
        }
        write_title(conn, item_id, &found, &Now::read().iso(), &mut stats)?;
    }
    Ok(stats)
}

/// Replace one title's rows and its cursor in one transaction, so an interrupted run never
/// leaves a cursor vouching for rows already deleted.
fn write_title(
    conn: &mut Connection,
    item_id: &str,
    found: &Lookup,
    now_iso: &str,
    stats: &mut LetterboxdStats,
) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    write_fetched(&tx, NAMESPACE, SOURCE, item_id, now_iso, FETCHED_KEY)?;
    let stored = write_title_keywords(
        &tx,
        item_id,
        SOURCE,
        found.themes.iter().map(RawKeyword::plain),
        now_iso,
    )?;
    tx.commit()?;
    let keywords = stored
        .values()
        .collect::<std::collections::BTreeSet<_>>()
        .len();
    stats.titles_fetched += 1;
    stats.keywords_written += keywords;
    if found.outcome == Outcome::NotListed {
        stats.titles_not_listed += 1;
    } else if keywords > 0 {
        stats.titles_matched += 1;
    }
    Ok(())
}
