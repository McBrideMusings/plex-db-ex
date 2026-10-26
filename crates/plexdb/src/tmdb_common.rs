//! Rules shared by the three TMDB sweeps: the titles they walk, the media-type
//! mapping and the consecutive-failure threshold (`plexdb/tmdb_common.py`).
//!
//! Keywords, original language and edges all walk `items` for TMDB-linked movies
//! and shows, cache against a staleness window, and give up on a run after the same
//! number of consecutive failures. This module is the one place they read those
//! rules from, so a title shape one sweep handles and another skips cannot appear.

use rusqlite::Connection;

use crate::tmdb::TmdbError;

/// Consecutive TMDB failures before a sweep gives up on the rest of the library. One
/// flaky title fails alone; a revoked key or a rate limit fails in a run, and
/// grinding through the whole library against a dead key would skip every title
/// left while still reporting a clean-looking summary.
pub const MAX_CONSECUTIVE_FAILURES: usize = 3;

/// `items.type` -> the TMDB path segment, or `None` for a type no sweep enriches.
/// TMDB's `/keywords`, `/recommendations` and `/similar` endpoints exist only for
/// movies and shows; an episode is skipped silently.
pub fn media_type_for(item_type: &str) -> Option<&'static str> {
    match item_type {
        "movie" => Some("movie"),
        "show" => Some("tv"),
        _ => None,
    }
}

/// A walked movie or show and its local `tmdb` id, if it has one.
#[derive(Debug, Clone)]
pub struct Candidate {
    pub item_id: String,
    pub item_type: String,
    pub tmdb_id: Option<String>,
}

/// Every walked movie and show. A correlated subquery, not a JOIN, so a title is
/// visited exactly once even in the (schema-legal) case of more than one `tmdb` row
/// landing on one item_id.
pub fn candidates(conn: &Connection, ordered: bool) -> rusqlite::Result<Vec<Candidate>> {
    let order = if ordered { " ORDER BY i.item_id" } else { "" };
    conn.prepare(&format!(
        "SELECT i.item_id, i.type, \
         (SELECT e.value FROM external_ids e \
          WHERE e.item_id = i.item_id AND e.ns = 'tmdb' LIMIT 1) \
         FROM items i WHERE i.type IN ('movie', 'show'){order}"
    ))?
    .query_map([], |row| {
        Ok(Candidate {
            item_id: row.get(0)?,
            item_type: row.get(1)?,
            tmdb_id: row.get(2)?,
        })
    })?
    .collect()
}

/// Counts failures in a row. Any success resets the run.
#[derive(Debug, Default)]
pub struct FailureRun {
    consecutive: usize,
}

impl FailureRun {
    pub fn succeeded(&mut self) {
        self.consecutive = 0;
    }

    /// Note one failure; an error once `MAX_CONSECUTIVE_FAILURES` come in a row.
    pub fn failed(&mut self, seen: usize, failed: usize, err: &TmdbError) -> Result<(), TmdbError> {
        self.consecutive += 1;
        if self.consecutive >= MAX_CONSECUTIVE_FAILURES {
            return Err(TmdbError(format!(
                "aborting after {MAX_CONSECUTIVE_FAILURES} consecutive failures: \
                 {seen} title(s) processed, {failed} failed; tripping error: {err}"
            )));
        }
        Ok(())
    }
}
