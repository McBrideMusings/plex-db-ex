//! `prune-frames`: delete `frames.db` rows for titles no longer in `items`
//! (ADR-0024).
//!
//! A title Plex no longer has, or one whose item_id a repair re-keyed, leaves its
//! vectors, cuts, shots, queue row and state row behind in the Frame index. This
//! step reads `items` through a read-only connection and then writes `frames.db`
//! alone, so no transaction spans the two files.
//!
//! It opens `frames.db` as every Frame command does: creating it and applying a pending
//! migration first, which changes nothing in a current file.

use std::collections::BTreeSet;
use std::path::Path;

use plexdb_store::StoreError;
use rusqlite::{Connection, OpenFlags};

use crate::frames::{TITLE_TABLES, delete_vectors};

pub const NAME: &str = "prune-frames";

/// What a prune removed.
#[derive(Debug, Default)]
pub struct PruneStats {
    pub item_ids: Vec<String>,
    /// Rows deleted, per table in [`TITLE_TABLES`] order.
    pub deleted: Vec<(&'static str, usize)>,
}

/// Every item_id frames.db holds a row for. `frame_vec` is read through its shadow
/// table `frame_vec_chunks`, which holds one row per 1024 vectors with the partition
/// key beside it; selecting from `frame_vec` itself scans every vector row, which at
/// full library is tens of millions.
fn titles_held(conn: &Connection) -> Result<BTreeSet<String>, StoreError> {
    let mut held = BTreeSet::new();
    for table in ["frame_index_state", "cuts", "shots", "frame_queue"] {
        let mut stmt = conn.prepare(&format!("SELECT DISTINCT item_id FROM {table}"))?;
        for row in stmt.query_map([], |row| row.get::<_, String>(0))? {
            held.insert(row?);
        }
    }
    let mut stmt = conn.prepare("SELECT DISTINCT partition00 FROM frame_vec_chunks")?;
    for row in stmt.query_map([], |row| row.get::<_, String>(0))? {
        held.insert(row?);
    }
    Ok(held)
}

/// Delete every frames.db row whose item_id is not in `live`, one transaction per
/// title, matching the harvester's one commit per title.
pub fn prune_orphans(
    conn: &mut Connection,
    live: &BTreeSet<String>,
) -> Result<PruneStats, StoreError> {
    let orphans: Vec<String> = titles_held(conn)?.difference(live).cloned().collect();
    let mut deleted: Vec<(&'static str, usize)> = TITLE_TABLES.iter().map(|t| (*t, 0)).collect();
    for item_id in &orphans {
        let tx = conn.transaction()?;
        for (table, count) in &mut deleted {
            *count += if *table == "frame_vec" {
                delete_vectors(&tx, item_id)?
            } else {
                tx.execute(&format!("DELETE FROM {table} WHERE item_id = ?"), [item_id])?
            };
        }
        tx.commit()?;
    }
    Ok(PruneStats {
        item_ids: orphans,
        deleted,
    })
}

/// The `item_id`s the store holds, through a read-only connection.
pub fn live_item_ids(store: &Path) -> Result<BTreeSet<String>, StoreError> {
    let conn = Connection::open_with_flags(store, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|err| StoreError::Store(format!("cannot open {}: {err}", store.display())))?;
    let mut stmt = conn.prepare("SELECT item_id FROM items")?;
    let ids = stmt
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<Result<_, _>>()?;
    Ok(ids)
}

/// The lines `plexdb prune-frames` prints.
pub fn describe(stats: &PruneStats) -> Vec<String> {
    let counts = stats
        .deleted
        .iter()
        .map(|(table, count)| {
            format!(
                "{} {table} row(s)",
                crate::migrate::thousands(*count as i64)
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    let mut lines = vec![format!(
        "pruned {} title(s) no longer in items: {counts}",
        crate::migrate::thousands(stats.item_ids.len() as i64)
    )];
    lines.extend(
        stats
            .item_ids
            .iter()
            .map(|item_id| format!("pruned {item_id}")),
    );
    lines
}
