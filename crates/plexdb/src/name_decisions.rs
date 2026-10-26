//! `fold-name-decisions`: a person's display name for a keyword, from Plex
//! DB-EX's `name_decisions.json` into `keyword_names` (`plexdb/names.py`
//! `fold_name_decisions`, ADR-0021).

use std::path::Path;

use plexdb_keywords::{is_carried, normalize_keyword};
use plexdb_store::py_strip;
use rusqlite::{Connection, TransactionBehavior, params};

use crate::decisions::{self, FoldError, FoldStats, Spec, thousands};

pub const NAME: &str = "fold-name-decisions";
pub const NAME_DECISIONS_FILE: &str = "name_decisions.json";

/// The longest display name a person may give.
pub const NAME_MAX: usize = 200;

fn key_problem(key: &[String]) -> Option<String> {
    key[0].is_empty().then(|| "has an empty surface".into())
}

fn values_problem(values: &[String]) -> Option<String> {
    let name = &values[0];
    if py_strip(name).is_empty() {
        return Some("has a blank name".into());
    }
    (name.chars().count() > NAME_MAX).then(|| format!("has a name over {NAME_MAX} characters"))
}

/// `NameDecisions`: one entry per keyword, keyed `(surface,)`.
pub const SPEC: Spec = Spec {
    key: &["surface"],
    decisions: &["named", "cleared"],
    values: &["name"],
    key_problem,
    values_problem,
};

/// Apply `path` to `keyword_names`; a missing file is no decisions.
///
/// `named` writes the row, `cleared` deletes it. An entry naming a keyword no
/// title carries is counted and ignored, and comes back once the keyword does.
/// The last entry for a keyword wins; the whole file is checked before the first
/// row changes and applied in one transaction.
pub fn fold(conn: &mut Connection, path: &Path) -> Result<FoldStats, FoldError> {
    let mut stats = FoldStats::default();
    let Some(entries) = decisions::load(&SPEC, path)? else {
        return Ok(stats);
    };
    stats.file_found = true;
    stats.entries = entries.len();
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for entry in &entries {
        let keyword = normalize_keyword(&entry.key[0]);
        if keyword.is_empty() || !is_carried(&tx, &keyword)? {
            stats.unmatched += 1;
            continue;
        }
        stats.matched += 1;
        if entry.decision == "cleared" {
            tx.execute("DELETE FROM keyword_names WHERE keyword = ?", [&keyword])?;
        } else {
            tx.execute(
                "INSERT INTO keyword_names (keyword, name, decided_at) VALUES (?, ?, ?) \
                 ON CONFLICT (keyword) DO UPDATE SET \
                 name = excluded.name, decided_at = excluded.decided_at",
                params![keyword, entry.values[0], entry.decided_at],
            )?;
        }
    }
    tx.commit()?;
    Ok(stats)
}

pub fn describe(stats: &FoldStats, path: &Path) -> String {
    let path = path.display();
    if !stats.file_found {
        return format!("name decisions: no file at {path}");
    }
    format!(
        "name decisions: {} entr(ies) in {path}, {} matched a stored keyword, {} named none",
        thousands(stats.entries),
        thousands(stats.matched),
        thousands(stats.unmatched)
    )
}

/// `plexdb fold-name-decisions`.
pub fn run() -> Result<(), FoldError> {
    decisions::run_fold(NAME_DECISIONS_FILE, fold, describe)
}
