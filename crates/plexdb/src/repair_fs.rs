//! `repair-fs-identities`: moving `fs:` item_ids still keyed on the disk mount point
//! to what the current identity rule produces (issue #55).
//!
//! PLEX_SOURCE_ROOTS is now required, and the `fs:` fallback hashes a title's
//! playback path with that root stripped first. That did nothing for ids written
//! before the fix: ADR-0008 keeps the walk from ever repointing an `item_id`. This is
//! the deliberate repair a person points at that known problem: re-derive every `fs:`
//! id with the unmodified rule, against a canonical path computed the way the walk
//! computes one today, and move the row and everything that references it.
//!
//! **Only `fs:` ids are touched**, since no other namespace depends on the mount. **A
//! move can land on an id another `fs:` item already owns** — two Plex records with no
//! GUID that canonicalise to one path — which is a merge, not a collision: every table
//! moves with a conflict-safe `UPDATE ... WHERE NOT EXISTS`, and the emptied source
//! row is dropped. **The rename needs `PRAGMA defer_foreign_keys`**: `items.item_id` is
//! the key every other table references with no `ON UPDATE CASCADE`, so the check waits
//! for `COMMIT`.

use std::collections::{BTreeMap, HashMap};

use plexdb_store::StoreError;
use rusqlite::Connection;

use crate::identity::derive_item_id;
use crate::plex::{PlexSource, section_passes};
use crate::pyval::{get, py_str, truthy};
use crate::walk::canonical_for;

pub const NAME: &str = "repair-fs-identities";

/// Tables carrying a plain `item_id` column with no other column in the key, so a move
/// can never collide.
const SIMPLE_TABLES: [&str; 3] = ["plex_items", "plays", "media_part"];

/// Tables carrying `item_id` beside other key columns. A move here can land on a row
/// another row already occupies, so it goes through a conflict-safe update.
const PK_COLUMNS: [(&str, &[&str]); 6] = [
    ("external_ids", &["ns", "value", "kind"]),
    ("enrichment", &["namespace", "source", "key", "value"]),
    ("enrichment_cursor", &["namespace", "source", "key"]),
    ("collection_membership", &["collection_id"]),
    ("keyword_role_statements", &["keyword", "role", "source"]),
    ("keyword_surfaces", &["source", "surface"]),
];

/// What a repair pass would do, computed read-only.
#[derive(Debug, Default)]
pub struct FsRepairPlan {
    /// Items currently keyed on an `fs:` id.
    pub fs_found: usize,
    /// `old_id -> new_id` for every `fs:` id whose re-derivation differs.
    pub moves: BTreeMap<String, String>,
    /// `fs:` ids re-derived to the id already on file.
    pub unchanged: usize,
    /// `fs:` ids none of whose rating keys are in the library Plex reports today.
    pub not_in_plex: usize,
}

/// What [`apply_fs_repairs`] did, enough for a caller to verify the guarded tables'
/// row counts against exactly the shrinkage this pass accounts for.
#[derive(Debug, Default)]
pub struct ApplyResult {
    /// Rows carried across, per table; tables with nothing carried are omitted.
    pub rows_carried: BTreeMap<String, usize>,
    /// Moves that merged into an `items` row `new_id` already held. Each removes
    /// exactly one `items` row.
    pub merges: usize,
    /// Per table, rows dropped as an exact duplicate of a fact `new_id` already carried.
    pub duplicates_dropped: BTreeMap<String, usize>,
}

fn fs_item_ids(conn: &Connection) -> Result<Vec<String>, StoreError> {
    let mut stmt = conn.prepare("SELECT item_id FROM items WHERE item_id LIKE 'fs:%'")?;
    let mut ids: Vec<String> = stmt
        .query_map([], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    ids.sort();
    Ok(ids)
}

/// Every rating key Plex currently holds, and the canonical string the path-hash
/// fallback would hash for it today.
fn canonical_by_rating_key(
    source: &dyn PlexSource,
    source_roots: &[String],
) -> Result<HashMap<String, String>, StoreError> {
    let mut canonical = HashMap::new();
    for section in source.sections()? {
        for (_kind, plex_type) in section_passes(&section.kind) {
            for record in source.items(&section.key, *plex_type)? {
                let rating_key = get(&record, "ratingKey");
                if truthy(rating_key) {
                    canonical.insert(py_str(rating_key), canonical_for(&record, source_roots));
                }
            }
        }
    }
    Ok(canonical)
}

/// Work out what a repair pass would move, without writing anything.
pub fn plan_fs_repairs(
    conn: &Connection,
    source: &dyn PlexSource,
    source_roots: &[String],
) -> Result<FsRepairPlan, StoreError> {
    let item_ids = fs_item_ids(conn)?;
    if item_ids.is_empty() {
        return Ok(FsRepairPlan::default());
    }
    let canonical = canonical_by_rating_key(source, source_roots)?;
    let mut moves = BTreeMap::new();
    let mut not_in_plex = 0;
    for old_id in &item_ids {
        // Several rating keys on one `fs:` id can happen (the same file in two
        // sections); the lowest one present in the library today keeps the choice
        // deterministic across runs.
        let mut keys: Vec<String> = conn
            .prepare("SELECT rating_key FROM plex_items WHERE item_id = ?")?
            .query_map([old_id], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        keys.sort();
        let Some(canonical) = keys.iter().find_map(|key| canonical.get(key)) else {
            not_in_plex += 1;
            continue;
        };
        // External ids are read from the store rather than assumed empty: reading
        // the real row is what makes this a re-derivation and not a guess.
        let external_ids: Vec<(String, String)> = conn
            .prepare("SELECT ns, value FROM external_ids WHERE item_id = ?")?
            .query_map([old_id], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?;
        let new_id = derive_item_id(&external_ids, canonical);
        if &new_id != old_id {
            moves.insert(old_id.clone(), new_id);
        }
    }
    let unchanged = item_ids.len() - moves.len() - not_in_plex;
    Ok(FsRepairPlan {
        fs_found: item_ids.len(),
        moves,
        unchanged,
        not_in_plex,
    })
}

/// Repoint `table.item_id` from `old_id` to `new_id`, folding a row into one `new_id`
/// already holds rather than colliding with it. Whichever copy already sits under
/// `new_id` is kept, and any leftover duplicate is dropped: it names the identical
/// fact. Returns `(moved, dropped)`.
fn move_conflict_safe(
    conn: &Connection,
    table: &str,
    columns: &[&str],
    old_id: &str,
    new_id: &str,
) -> Result<(usize, usize), StoreError> {
    let matching = columns
        .iter()
        .map(|column| format!("t2.{column} = {table}.{column}"))
        .collect::<Vec<_>>()
        .join(" AND ");
    let moved = conn.execute(
        &format!(
            "UPDATE {table} SET item_id = ? WHERE item_id = ? \
             AND NOT EXISTS (SELECT 1 FROM {table} t2 WHERE {matching} AND t2.item_id = ?)"
        ),
        (new_id, old_id, new_id),
    )?;
    let dropped = conn.execute(&format!("DELETE FROM {table} WHERE item_id = ?"), [old_id])?;
    Ok((moved, dropped))
}

/// Repoint every edge naming `old_id`, as either endpoint, to `new_id`. `edges` has
/// two columns that can hold an item_id, and its key spans both.
fn move_edges(conn: &Connection, old_id: &str, new_id: &str) -> Result<usize, StoreError> {
    let mut moved = 0;
    for (column, other) in [("from_id", "to_id"), ("to_id", "from_id")] {
        moved += conn.execute(
            &format!(
                "UPDATE edges SET {column} = ? WHERE {column} = ? AND NOT EXISTS (\
                     SELECT 1 FROM edges e2 WHERE e2.{column} = ? \
                     AND e2.{other} = edges.{other} AND e2.edge_type = edges.edge_type)"
            ),
            (new_id, old_id, new_id),
        )?;
    }
    conn.execute(
        "DELETE FROM edges WHERE from_id = ? OR to_id = ?",
        (old_id, old_id),
    )?;
    Ok(moved)
}

/// Move one identity's row and everything that references it. `items` is moved last:
/// whether `new_id` already has a row there decides rename versus merge. Returns
/// whether this move merged.
fn move_item(
    conn: &Connection,
    old_id: &str,
    new_id: &str,
    totals: &mut BTreeMap<String, usize>,
    dropped: &mut BTreeMap<String, usize>,
) -> Result<bool, StoreError> {
    for table in SIMPLE_TABLES {
        let moved = conn.execute(
            &format!("UPDATE {table} SET item_id = ? WHERE item_id = ?"),
            (new_id, old_id),
        )?;
        *totals.entry(table.into()).or_default() += moved;
    }
    for (table, columns) in PK_COLUMNS {
        let (moved, gone) = move_conflict_safe(conn, table, columns, old_id, new_id)?;
        *totals.entry(table.into()).or_default() += moved;
        *dropped.entry(table.into()).or_default() += gone;
    }
    *totals.entry("edges".into()).or_default() += move_edges(conn, old_id, new_id)?;

    let exists: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM items WHERE item_id = ?)",
        [new_id],
        |row| row.get(0),
    )?;
    if exists {
        conn.execute("DELETE FROM items WHERE item_id = ?", [old_id])?;
        return Ok(true);
    }
    conn.execute(
        "UPDATE items SET item_id = ? WHERE item_id = ?",
        (new_id, old_id),
    )?;
    Ok(false)
}

/// Carry out `plan.moves` in one transaction with `PRAGMA defer_foreign_keys = ON`,
/// sorted so a merge keeps the same survivor on every run.
pub fn apply_fs_repairs(conn: &Connection, plan: &FsRepairPlan) -> Result<ApplyResult, StoreError> {
    if plan.moves.is_empty() {
        return Ok(ApplyResult::default());
    }
    let (mut totals, mut dropped) = (BTreeMap::new(), BTreeMap::new());
    let mut merges = 0;
    conn.execute_batch("BEGIN")?;
    let moved = (|| -> Result<(), StoreError> {
        conn.execute_batch("PRAGMA defer_foreign_keys = ON")?;
        for (old_id, new_id) in &plan.moves {
            if move_item(conn, old_id, new_id, &mut totals, &mut dropped)? {
                merges += 1;
            }
        }
        Ok(())
    })();
    if let Err(err) = moved {
        let _ = conn.execute_batch("ROLLBACK");
        return Err(err);
    }
    // The deferred foreign-key check runs here; a violation fails the commit and
    // leaves the transaction open.
    if let Err(err) = conn.execute_batch("COMMIT") {
        let _ = conn.execute_batch("ROLLBACK");
        return Err(err.into());
    }
    totals.retain(|_, count| *count > 0);
    dropped.retain(|_, count| *count > 0);
    Ok(ApplyResult {
        rows_carried: totals,
        merges,
        duplicates_dropped: dropped,
    })
}

/// Put the copy back and say why. When the copy back fails too, both reasons are named,
/// so the failure that called for the rollback is not lost behind the rollback's own.
fn rolled_back(taken: &std::path::Path, store: &std::path::Path, reason: String) -> StoreError {
    match crate::migrate::restore(taken, store) {
        Ok(()) => StoreError::Store(format!(
            "{reason}; the store was rolled back from {}",
            taken.display()
        )),
        Err(err) => StoreError::Store(format!(
            "{reason} — but the rollback from {} failed, so {} may be half-repaired: {err}",
            taken.display(),
            store.display()
        )),
    }
}

fn summary(plan: &FsRepairPlan, verb: &str) -> String {
    format!(
        "{} identit(ies) keyed on fs:, {} {verb}, {} already correct, {} skipped (left Plex \
         since the last walk)",
        plan.fs_found,
        plan.moves.len(),
        plan.unchanged,
        plan.not_in_plex
    )
}

/// The `repair-fs-identities` step: what a person sees. A real run copies the store
/// first and puts the copy back when the move fails or loses a row.
pub fn run(
    config: &plexdb_store::Config,
    source: &dyn PlexSource,
    source_roots: &[String],
    dry_run: bool,
) -> Result<Vec<String>, StoreError> {
    let plan = {
        let store = plexdb_store::open_store(&config.store_path)?;
        plan_fs_repairs(&store.conn, source, source_roots)?
    };
    if dry_run {
        let mut lines = vec![summary(&plan, "would move")];
        lines.extend(
            plan.moves
                .iter()
                .take(20)
                .map(|(old, new)| format!("  {old} -> {new}")),
        );
        if plan.moves.len() > 20 {
            lines.push(format!("  ... and {} more", plan.moves.len() - 20));
        }
        return Ok(lines);
    }
    if plan.moves.is_empty() {
        return Ok(vec![if plan.fs_found == 0 {
            "no fs: identities found — nothing to repair".into()
        } else {
            format!(
                "{} identit(ies) keyed on fs:, all already correct or unreachable in Plex — \
                 nothing to move",
                plan.fs_found
            )
        }]);
    }

    let taken = crate::migrate::take_backup(
        &config.store_path,
        &config.backup_dir,
        "plexdb.pre-repair-fs-identities",
    )?;
    let landed = (|| {
        let store = plexdb_store::open_store(&config.store_path)?;
        let before = crate::migrate::guarded_counts(&store.conn)?;
        let result = apply_fs_repairs(&store.conn, &plan)?;
        let after = crate::migrate::guarded_counts(&store.conn)?;
        Ok::<_, StoreError>((before, result, after))
    })();
    let (before, result, after) = match landed {
        Ok(landed) => landed,
        Err(err) => {
            return Err(rolled_back(
                &taken,
                &config.store_path,
                format!("repair-fs-identities failed: {err}"),
            ));
        }
    };

    // `items` and `enrichment` are both allowed to shrink by design — a merge folds two
    // rows into one, or drops an exact duplicate fact. Compare against exactly the
    // shrinkage this pass itself accounts for, not a raw before/after count: a
    // legitimate merge looks identical to row loss under a plain count comparison.
    let accounted = |table: &str| match table {
        "items" => result.merges as i64,
        "enrichment" => result
            .duplicates_dropped
            .get("enrichment")
            .copied()
            .unwrap_or(0) as i64,
        _ => 0,
    };
    for (table, count_before) in &before {
        let count_after = after
            .iter()
            .find(|(name, _)| name == table)
            .map_or(0, |(_, rows)| *rows);
        let expected = count_before - accounted(table);
        if count_after != expected {
            return Err(rolled_back(
                &taken,
                &config.store_path,
                format!(
                    "repair-fs-identities: {table} went from {} rows to {}, {} expected",
                    crate::migrate::thousands(*count_before),
                    crate::migrate::thousands(count_after),
                    crate::migrate::thousands(expected)
                ),
            ));
        }
    }

    let mut lines = vec![summary(&plan, "moved")];
    for (table, count) in &result.rows_carried {
        lines.push(format!("  {count} row(s) carried in {table}"));
    }
    if result.merges > 0 {
        lines.push(format!(
            "  {} identit(ies) merged into one that another fs: id already reached",
            result.merges
        ));
    }
    for (table, count) in &result.duplicates_dropped {
        lines.push(format!(
            "  {count} duplicate row(s) in {table} dropped as part of a merge"
        ));
    }
    Ok(lines)
}
