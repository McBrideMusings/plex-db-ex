//! Asking Jev what roles each stored keyword plays (`plexdb/roles.py`, ADR-0019).
//!
//! Every distinct stored keyword with no `source = 'jev'` row in `keyword_roles` is asked
//! about once: one request carrying one `noul` question per role in [`ROLES`], about the
//! keyword's shortest readable surface form (`readable_surfaces`). Each answer becomes one
//! row, five per keyword, with `score` exactly as Jev gave it and `model` the model id in
//! the response.
//!
//! A keyword that has a `jev` row is never asked again, so a rerun with no new keyword
//! sends nothing. A keyword Jev refuses with a 400 or 422 gets five rows with `error` set
//! and `score` and `model` NULL, so it is not asked again and does not hold up its batch.
//! Any other failure (429 exhausted, 5xx, a bad key, no connection) stops the run.
//!
//! Keywords are asked about in batches of [`KEYWORD_BATCH`], each judged completely before
//! its rows are written in one transaction, so a run killed partway keeps every finished
//! batch and the next run asks only about keywords still without a row. Nothing here
//! decides whether a keyword *has* a role: that threshold is the reader's.

use std::collections::HashSet;

use plexdb_keywords::readable_surfaces;
use rusqlite::{Connection, Transaction, params};

use crate::decisions::{py_repr, thousands};
use crate::jev::{Jev, RoleVerdict, judge_all};
use crate::role_decisions::ROLES;
use crate::staleness::Now;

/// This judge's source name in `keyword_roles`.
pub const SOURCE: &str = "jev";
/// Keywords judged per committed batch — 1,000 rows.
pub const KEYWORD_BATCH: usize = 200;
/// Jev requests in flight at once.
pub const JUDGE_WORKERS: usize = 8;

#[derive(Debug, Default, PartialEq, Eq)]
pub struct RoleStats {
    pub keywords_stored: usize,
    /// Keywords that already had a `jev` row, left alone.
    pub keywords_already_judged: usize,
    pub keywords_judged: usize,
    /// Keywords Jev refused with a 400 or 422, stored with `error` and no score.
    pub keywords_unjudgeable: usize,
    pub rows_written: usize,
    pub batches_committed: usize,
}

#[derive(Debug, thiserror::Error)]
pub enum RoleError {
    #[error("{0}")]
    Store(#[from] rusqlite::Error),
    #[error("{0}")]
    Jev(String),
}

/// Ask `judge` about every keyword with no `jev` row and write five rows each.
///
/// `limit` caps how many keywords this run asks about, so a first run over a whole
/// vocabulary can be taken in pieces.
pub fn judge_keyword_roles(
    conn: &mut Connection,
    judge: &dyn Jev,
    limit: Option<usize>,
    log: &mut dyn FnMut(&str),
) -> Result<RoleStats, RoleError> {
    let mut stats = RoleStats::default();
    let surfaces = readable_surfaces(conn)?;
    stats.keywords_stored = surfaces.len();
    let judged: HashSet<String> = conn
        .prepare("SELECT DISTINCT keyword FROM keyword_roles WHERE source = ?")?
        .query_map([SOURCE], |row| row.get::<_, String>(0))?
        .collect::<Result<_, _>>()?;
    let mut todo: Vec<&String> = surfaces.keys().filter(|k| !judged.contains(*k)).collect();
    stats.keywords_already_judged = stats.keywords_stored - todo.len();
    if let Some(limit) = limit {
        todo.truncate(limit);
    }
    log(&format!(
        "{} keyword(s) to judge; {} of {} already judged",
        thousands(todo.len()),
        thousands(stats.keywords_already_judged),
        thousands(stats.keywords_stored)
    ));

    for (batch_index, batch) in todo.chunks(KEYWORD_BATCH).enumerate() {
        let verdicts = judge_all(
            |keyword: &&String| judge.judge_roles(&surfaces[*keyword]),
            batch,
            JUDGE_WORKERS,
        )
        .map_err(RoleError::Jev)?;
        let stated_at = Now::read().iso();
        let tx = conn.transaction()?;
        for (keyword, verdict) in batch.iter().zip(&verdicts) {
            match verdict {
                Err(refusal) => {
                    stats.rows_written += write(&tx, keyword, &stated_at, Err(refusal))?;
                    stats.keywords_unjudgeable += 1;
                    log(&format!(
                        "unjudgeable keyword {}: {refusal}",
                        py_repr(&surfaces[*keyword])
                    ));
                }
                Ok(verdict) => {
                    stats.rows_written += write(&tx, keyword, &stated_at, Ok(verdict))?;
                    stats.keywords_judged += 1;
                }
            }
        }
        tx.commit()?;
        stats.batches_committed += 1;
        log(&format!(
            "{}/{} keyword(s) judged, {} row(s) written",
            thousands(batch_index * KEYWORD_BATCH + batch.len()),
            thousands(todo.len()),
            thousands(stats.rows_written)
        ));
    }
    Ok(stats)
}

/// One row per role: Jev's score and model, or the refusal with neither.
fn write(
    tx: &Transaction,
    keyword: &str,
    stated_at: &str,
    outcome: Result<&RoleVerdict, &String>,
) -> rusqlite::Result<usize> {
    let mut insert = tx.prepare_cached(
        "INSERT INTO keyword_roles (keyword, role, source, score, model, error, stated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
    )?;
    let mut written = 0;
    for (index, role) in ROLES.iter().enumerate() {
        let (score, model, error) = match outcome {
            Ok(verdict) => (Some(verdict.scores[index]), Some(&verdict.model), None),
            Err(refusal) => (None, None, Some(refusal)),
        };
        written += insert.execute(params![
            keyword, role, SOURCE, score, model, error, stated_at
        ])?;
    }
    Ok(written)
}
