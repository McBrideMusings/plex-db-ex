//! `fold-merge-decisions`: a person's accept, reject or clear of a keyword pair,
//! from Plex DB-EX's `merge_decisions.json` into `keyword_pairs.decision`
//! (`plexdb/synonyms.py` `fold_merge_decisions`, ADR-0018, ADR-0020).
//!
//! An entry names a pair by one spelling of each keyword. The spellings
//! normalize to the pair stored now, so a decision survives a migration that
//! renames stored values.

use std::path::Path;

use plexdb_keywords::normalize_keyword;
use rusqlite::{Connection, TransactionBehavior, params};

use crate::decisions::{self, FoldError, FoldStats, Spec, no_values_problem, thousands};

pub const NAME: &str = "fold-merge-decisions";
pub const MERGE_DECISIONS_FILE: &str = "merge_decisions.json";

fn key_problem(key: &[String]) -> Option<String> {
    if key.iter().any(String::is_empty) {
        return Some("has an empty surface".into());
    }
    (key[0] == key[1]).then(|| "names one surface twice".into())
}

/// `MergeDecisions`: one entry per pair, keyed `(surface_a, surface_b)`.
pub const SPEC: Spec = Spec {
    key: &["surface_a", "surface_b"],
    decisions: &["accepted", "rejected", "cleared"],
    values: &[],
    key_problem,
    values_problem: no_values_problem,
};

/// The stored pair, in `keyword_a < keyword_b` order; `None` when a spelling
/// normalizes to nothing or both normalize to one value, which is no pair.
pub fn resolve(key: &[String]) -> Option<(String, String)> {
    let a = normalize_keyword(&key[0]);
    let b = normalize_keyword(&key[1]);
    if a.is_empty() || b.is_empty() || a == b {
        return None;
    }
    Some(if a < b { (a, b) } else { (b, a) })
}

/// Whether some title carries `keyword` as a stored keyword value.
fn is_stored(conn: &Connection, keyword: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM enrichment WHERE namespace = 'keywords' \
         AND key = 'keyword' AND value = ?)",
        [keyword],
        |row| row.get(0),
    )
}

/// Apply `path` to `keyword_pairs.decision`; a missing file is no decisions.
///
/// `cleared` sets `decision` and `decided_at` back to NULL, and deletes a row no
/// judge wrote. A decision on a pair with no row, whose two keywords are both
/// stored, inserts a row with only the decision; one naming a keyword no title
/// carries, or two spellings that now normalize to one value, is unmatched.
/// Entries apply in list order, so the last for a pair wins; the whole file is
/// checked before the first row changes and applied in one transaction.
pub fn fold(conn: &mut Connection, path: &Path) -> Result<FoldStats, FoldError> {
    let mut stats = FoldStats::default();
    let Some(entries) = decisions::load(&SPEC, path)? else {
        return Ok(stats);
    };
    stats.file_found = true;
    stats.entries = entries.len();
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for entry in &entries {
        let Some((a, b)) = resolve(&entry.key) else {
            stats.unmatched += 1;
            continue;
        };
        let changed = if entry.decision == "cleared" {
            tx.execute(
                "DELETE FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ? \
                 AND judged_at IS NULL",
                params![a, b],
            )? + tx.execute(
                "UPDATE keyword_pairs SET decision = NULL, decided_at = NULL \
                 WHERE keyword_a = ? AND keyword_b = ?",
                params![a, b],
            )?
        } else {
            let mut changed = tx.execute(
                "UPDATE keyword_pairs SET decision = ?, decided_at = ? \
                 WHERE keyword_a = ? AND keyword_b = ?",
                params![entry.decision, entry.decided_at, a, b],
            )?;
            if changed == 0 && is_stored(&tx, &a)? && is_stored(&tx, &b)? {
                changed = tx.execute(
                    "INSERT INTO keyword_pairs (keyword_a, keyword_b, decision, decided_at) \
                     VALUES (?, ?, ?, ?)",
                    params![a, b, entry.decision, entry.decided_at],
                )?;
            }
            changed
        };
        if changed > 0 {
            stats.matched += 1;
        } else {
            stats.unmatched += 1;
        }
    }
    tx.commit()?;
    Ok(stats)
}

pub fn describe(stats: &FoldStats, path: &Path) -> String {
    let path = path.display();
    if !stats.file_found {
        return format!("merge decisions: no file at {path}");
    }
    format!(
        "merge decisions: {} entr(ies) in {path}, {} applied, {} named no pair of stored keywords",
        thousands(stats.entries),
        thousands(stats.matched),
        thousands(stats.unmatched)
    )
}

/// `plexdb fold-merge-decisions`.
pub fn run() -> Result<(), FoldError> {
    decisions::run_fold(MERGE_DECISIONS_FILE, fold, describe)
}
