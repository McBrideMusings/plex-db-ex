//! Every enrichment writer's `enrichment_cursor` rows (`plexdb/cursors.py`).
//!
//! Each writer keeps a `fetched` cursor per title under its (namespace, source):
//! the title's facts are current as of that time, and the title leaves the next
//! run's list until the cursor goes stale. Cursors are scheduling, never facts
//! (ADR-0013), so they live in `enrichment_cursor`, not `enrichment`.

use std::collections::HashMap;

use rusqlite::{Connection, params};

pub const FETCHED_KEY: &str = "fetched";
pub const ATTEMPTED_KEY: &str = "attempted";

/// item_id -> when its facts were last written, for one writer's cursor key.
pub fn load_fetched(
    conn: &Connection,
    namespace: &str,
    source: &str,
    key: &str,
) -> rusqlite::Result<HashMap<String, String>> {
    conn.prepare(
        "SELECT item_id, fetched_at FROM enrichment_cursor \
         WHERE namespace = ? AND source = ? AND key = ?",
    )?
    .query_map(params![namespace, source, key], |row| {
        Ok((row.get(0)?, row.get(1)?))
    })?
    .collect()
}

/// Record a title as fetched at `now_iso` and clear its `attempted` cursor. Runs on
/// the caller's transaction, so it commits or rolls back together with the facts it
/// vouches for.
pub fn write_fetched(
    conn: &Connection,
    namespace: &str,
    source: &str,
    item_id: &str,
    now_iso: &str,
    key: &str,
) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
         VALUES (?, ?, ?, ?, ?) \
         ON CONFLICT(item_id, namespace, source, key) DO UPDATE SET \
         fetched_at = excluded.fetched_at",
        params![item_id, namespace, source, key, now_iso],
    )?;
    conn.execute(
        "DELETE FROM enrichment_cursor \
         WHERE item_id = ? AND namespace = ? AND source = ? AND key = ?",
        params![item_id, namespace, source, ATTEMPTED_KEY],
    )?;
    Ok(())
}

/// Delete one writer's cursors — every key, or only `key` — and no other writer's.
/// Returns the number of rows removed.
pub fn delete_cursors(
    conn: &Connection,
    namespace: &str,
    source: &str,
    key: Option<&str>,
) -> rusqlite::Result<usize> {
    match key {
        None => conn.execute(
            "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ?",
            params![namespace, source],
        ),
        Some(key) => conn.execute(
            "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ? AND key = ?",
            params![namespace, source, key],
        ),
    }
}

/// One writer's cursors, read once at the start of a run.
#[derive(Debug, Default)]
pub struct Cursors {
    /// item_id -> when its facts were last written.
    pub fetched: HashMap<String, String>,
    /// item_id -> when a run last tried it and wrote nothing.
    pub attempted: HashMap<String, String>,
}

impl Cursors {
    /// Sort key putting never-attempted titles first, then attempted ones, oldest
    /// attempt first. A stable sort keeps the order the caller built the first group in.
    pub fn order_key(&self, item_id: &str) -> (bool, String) {
        match self.attempted.get(item_id) {
            Some(at) => (true, at.clone()),
            None => (false, String::new()),
        }
    }
}

pub fn load_cursors(conn: &Connection, namespace: &str, source: &str) -> rusqlite::Result<Cursors> {
    let mut cursors = Cursors::default();
    let mut statement = conn.prepare(
        "SELECT item_id, key, fetched_at FROM enrichment_cursor \
         WHERE namespace = ? AND source = ? AND key IN (?, ?)",
    )?;
    let rows = statement.query_map(
        params![namespace, source, FETCHED_KEY, ATTEMPTED_KEY],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        },
    )?;
    for row in rows {
        let (item_id, key, at) = row?;
        if key == FETCHED_KEY {
            cursors.fetched.insert(item_id, at);
        } else {
            cursors.attempted.insert(item_id, at);
        }
    }
    Ok(cursors)
}

/// Move titles that produced nothing cacheable to the back of the next run's order.
/// Their `fetched` cursors and their facts are left as they were.
pub fn mark_attempted(
    conn: &mut Connection,
    namespace: &str,
    source: &str,
    item_ids: &[&str],
    now_iso: &str,
) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    for item_id in item_ids {
        tx.execute(
            "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
             VALUES (?, ?, ?, ?, ?) \
             ON CONFLICT(item_id, namespace, source, key) DO UPDATE SET \
             fetched_at = excluded.fetched_at",
            params![item_id, namespace, source, ATTEMPTED_KEY, now_iso],
        )?;
    }
    tx.commit()
}
