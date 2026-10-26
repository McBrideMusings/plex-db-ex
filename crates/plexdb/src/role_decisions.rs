//! `fold-role-decisions`: a person's accept, reject or clear of a keyword's role,
//! from Plex DB-EX's `role_decisions.json` into `keyword_role_decisions`
//! (`plexdb/roles.py` `fold_role_decisions`, ADR-0019).

use std::path::Path;

use plexdb_keywords::normalize_keyword;
use rusqlite::{Connection, TransactionBehavior, params};

use crate::decisions::{
    self, FoldError, FoldStats, Spec, no_values_problem, py_repr, py_tuple_repr, thousands,
};

pub const NAME: &str = "fold-role-decisions";
pub const ROLE_DECISIONS_FILE: &str = "role_decisions.json";

/// Every role a `keyword_roles` or `keyword_role_decisions` row may carry
/// (`keywords.ROLES`; the v14 migration CHECKs the same set in SQL).
pub const ROLES: [&str; 5] = ["tone", "era", "region", "theme", "character_trait"];

fn key_problem(key: &[String]) -> Option<String> {
    let (surface, role) = (&key[0], &key[1]);
    if surface.is_empty() {
        return Some("has an empty surface".into());
    }
    if !ROLES.contains(&role.as_str()) {
        return Some(format!(
            "has role {}, expected one of {}",
            py_repr(role),
            py_tuple_repr(&ROLES)
        ));
    }
    None
}

/// `RoleDecisions`: one entry per (keyword, role), keyed `(surface, role)`.
pub const SPEC: Spec = Spec {
    key: &["surface", "role"],
    decisions: &["accepted", "rejected", "cleared"],
    values: &[],
    key_problem,
    values_problem: no_values_problem,
};

/// The stored `(keyword, role)`; `None` when the spelling normalizes to nothing.
pub fn resolve(key: &[String]) -> Option<(String, String)> {
    let keyword = normalize_keyword(&key[0]);
    (!keyword.is_empty()).then(|| (keyword, key[1].clone()))
}

/// Whether `keyword_roles` has a verdict (not a refusal) for `keyword` in `role`.
fn has_verdict(conn: &Connection, keyword: &str, role: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM keyword_roles WHERE keyword = ? AND role = ? \
         AND error IS NULL)",
        params![keyword, role],
        |row| row.get(0),
    )
}

/// Apply `path` to `keyword_role_decisions`; a missing file is no decisions.
///
/// `accepted` and `rejected` set the row's `decision` and `decided_at`; `cleared`
/// deletes the row. An entry naming a (keyword, role) with no verdict in
/// `keyword_roles` is counted and ignored. The last entry for a (keyword, role)
/// wins; the whole file is checked before the first row changes and applied in
/// one transaction.
pub fn fold(conn: &mut Connection, path: &Path) -> Result<FoldStats, FoldError> {
    let mut stats = FoldStats::default();
    let Some(entries) = decisions::load(&SPEC, path)? else {
        return Ok(stats);
    };
    stats.file_found = true;
    stats.entries = entries.len();
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for entry in &entries {
        let Some((keyword, role)) = resolve(&entry.key) else {
            stats.unmatched += 1;
            continue;
        };
        if !has_verdict(&tx, &keyword, &role)? {
            stats.unmatched += 1;
            continue;
        }
        stats.matched += 1;
        if entry.decision == "cleared" {
            tx.execute(
                "DELETE FROM keyword_role_decisions WHERE keyword = ? AND role = ?",
                params![keyword, role],
            )?;
        } else {
            tx.execute(
                "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) \
                 VALUES (?, ?, ?, ?) ON CONFLICT (keyword, role) DO UPDATE SET \
                 decision = excluded.decision, decided_at = excluded.decided_at",
                params![keyword, role, entry.decision, entry.decided_at],
            )?;
        }
    }
    tx.commit()?;
    Ok(stats)
}

pub fn describe(stats: &FoldStats, path: &Path) -> String {
    let path = path.display();
    if !stats.file_found {
        return format!("role decisions: no file at {path}");
    }
    format!(
        "role decisions: {} entr(ies) in {path}, {} matched a keyword role, {} named none",
        thousands(stats.entries),
        thousands(stats.matched),
        thousands(stats.unmatched)
    )
}

/// `plexdb fold-role-decisions`.
pub fn run() -> Result<(), FoldError> {
    decisions::run_fold(ROLE_DECISIONS_FILE, fold, describe)
}
