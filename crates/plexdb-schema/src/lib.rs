//! The store's schema, and the code that applies it.
//!
//! The schema **is** the public API of this project (ADR-0001). One process writes
//! the file; every consumer opens it read-only, and there is no version
//! negotiation, so a change here is a breaking change for every consumer at once.
//! The writer applies this chain and `plexdb-reader`'s tests build their stores
//! with it, so both compile against one definition of every table.
//!
//! [`MIGRATIONS`] is append-only: index 0 takes an empty database to version 1,
//! index 1 takes it to version 2, and so on. Never edit a step that has shipped;
//! add another. A step is a batch of SQL in `migrations/vNN.sql`, run inside one
//! transaction with the version row; a step needing logic no SQL statement can
//! express is a function taking that transaction instead (versions 10 and 17).

use rusqlite::{Connection, Transaction};
use thiserror::Error;

mod keyword_steps;

/// Tables whose contents no re-run reproduces, checked for row loss after a
/// migration. `plays` is watch history Tautulli eventually forgets, `enrichment`
/// is a TMDB sweep paid for against a rate limit, and `items` is what both hang
/// off. Everything else — edges, collections, cursors — is re-fetchable, so a
/// migration that rebuilds one of those is not a red flag.
pub const GUARDED_TABLES: [&str; 3] = ["items", "plays", "enrichment"];

/// The read shape a fully migrated store carries in its `reader_shape` row —
/// what `plexdb-reader`'s `SUPPORTED_READER_SHAPE` must equal.
///
/// A migration that changes something a reader reads — renames or drops a
/// column it selects, moves rows between namespaces, changes what a row means —
/// sets this to its own version and ends with
/// `UPDATE reader_shape SET version = <its version>;`, in the same change that
/// updates `plexdb-reader`. A migration that only adds leaves it alone.
pub const READER_SHAPE: i64 = 10;

#[derive(Debug, Error)]
pub enum SchemaError {
    /// The store is in a state this build will not migrate or read: newer than
    /// it, damaged, or needing a step this build cannot take.
    #[error("{0}")]
    Refused(String),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
}

/// The row count a step expects a guarded table to hold once it lands, for a table
/// it intentionally shrinks. `plexdb migrate` accepts a drop in a guarded table only
/// down to exactly this number, so a step that also lost an unrelated row lands on
/// another number and is rolled back. Empty for a step that never shrinks one on
/// purpose, which is every step but versions 10 and 17.
pub type Shrinks = Vec<(&'static str, i64)>;

/// One step of the chain.
pub enum Migration {
    Sql(&'static str),
    Step(fn(&Transaction) -> Result<Shrinks, SchemaError>),
}

use Migration::{Sql, Step};

/// Append-only. Index i takes the store from version i to version i+1.
pub const MIGRATIONS: &[Migration] = &[
    Sql(include_str!("../migrations/v01.sql")),
    Sql(include_str!("../migrations/v02.sql")),
    Sql(include_str!("../migrations/v03.sql")),
    Sql(include_str!("../migrations/v04.sql")),
    Sql(include_str!("../migrations/v05.sql")),
    Sql(include_str!("../migrations/v06.sql")),
    Sql(include_str!("../migrations/v07.sql")),
    Sql(include_str!("../migrations/v08.sql")),
    Sql(include_str!("../migrations/v09.sql")),
    Step(keyword_steps::v10),
    Sql(include_str!("../migrations/v11.sql")),
    Sql(include_str!("../migrations/v12.sql")),
    Sql(include_str!("../migrations/v13.sql")),
    Sql(include_str!("../migrations/v14.sql")),
    Sql(include_str!("../migrations/v15.sql")),
    Sql(include_str!("../migrations/v16.sql")),
    Step(keyword_steps::v17),
    Sql(include_str!("../migrations/v18.sql")),
    Sql(include_str!("../migrations/v19.sql")),
    Sql(include_str!("../migrations/v20.sql")),
    Sql(include_str!("../migrations/v21.sql")),
    Sql(include_str!("../migrations/v22.sql")),
    Sql(include_str!("../migrations/v23.sql")),
    Sql(include_str!("../migrations/v24.sql")),
    Sql(include_str!("../migrations/v25.sql")),
];

/// The version a store is at once every migration has been applied.
pub const SCHEMA_VERSION: i64 = MIGRATIONS.len() as i64;

/// The version this store is at. Zero means empty — nothing applied yet.
///
/// A store carrying a `schema_version` table with no row is refused: it is not
/// an empty store but a damaged one, and treating it as empty would re-run
/// migration 1 over tables that already exist.
pub fn current_version(conn: &Connection) -> Result<i64, SchemaError> {
    let has_table: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version')",
        [],
        |row| row.get(0),
    )?;
    if !has_table {
        return Ok(0);
    }
    let mut stmt = conn.prepare("SELECT version FROM schema_version")?;
    let mut rows = stmt.query([])?;
    match rows.next()? {
        Some(row) => Ok(row.get(0)?),
        None => Err(SchemaError::Refused(
            "store has a schema_version table but no version row — it is damaged. Restore it \
             from a backup, or delete the file and re-run `plexdb migrate` to rebuild it from Plex."
                .into(),
        )),
    }
}

/// Bring a store up to [`SCHEMA_VERSION`], returning the version it started at
/// and the one it ended at. Applying a current store is a no-op, which is what
/// makes `plexdb migrate` safe to run on every startup.
pub fn apply(conn: &mut Connection) -> Result<(i64, i64, Shrinks), SchemaError> {
    apply_chain(conn, MIGRATIONS)
}

/// Bring a store up to the last of `chain`. [`apply`] passes [`MIGRATIONS`]; a
/// test passes a prefix of it to build a store at an older version, or a chain
/// with a bad last step to prove what a failure leaves behind.
///
/// Each step runs in one transaction with its version row, so a process killed
/// mid-migration leaves the store at the last step that fully landed rather than
/// half-built.
pub fn apply_chain(
    conn: &mut Connection,
    chain: &[Migration],
) -> Result<(i64, i64, Shrinks), SchemaError> {
    let target = chain.len() as i64;
    let start = preflight(conn, chain)?;
    if start == target {
        return Ok((start, start, Vec::new()));
    }
    let mut declared = Shrinks::new();
    for (index, migration) in chain.iter().enumerate().skip(start as usize) {
        let tx = conn.transaction()?;
        // Inside the step's transaction, so a first step that fails leaves an
        // empty file rather than a version table with no row, which reads as
        // damaged.
        tx.execute_batch("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")?;
        match migration {
            Sql(sql) => tx.execute_batch(sql)?,
            Step(step) => {
                // The last step to declare a table's count wins: a later step
                // that merges more rows lands on its own number, not the first's.
                for (table, rows) in step(&tx)? {
                    declared.retain(|(held, _)| *held != table);
                    declared.push((table, rows));
                }
            }
        }
        tx.execute("DELETE FROM schema_version", [])?;
        tx.execute(
            "INSERT INTO schema_version (version) VALUES (?1)",
            [index as i64 + 1],
        )?;
        tx.commit()?;
    }
    Ok((start, target, declared))
}

/// The version the store is at, or why `chain` will not be applied to it, decided
/// by reading alone. [`apply_chain`] runs this first; `plexdb migrate` runs it
/// before it copies the store, so a store the chain refuses costs no copy.
///
/// Refused: a store newer than the chain, and a damaged one (see
/// [`current_version`]).
pub fn preflight(conn: &Connection, chain: &[Migration]) -> Result<i64, SchemaError> {
    let target = chain.len() as i64;
    let start = current_version(conn)?;
    if start > target {
        return Err(SchemaError::Refused(format!(
            "store is at schema version {start}, but this build only understands {target} — \
             upgrade plex-db-ex rather than writing with an older one"
        )));
    }
    Ok(start)
}

/// The Frame index's own chain (ADR-0024): `frames.db` is a second file with its own
/// schema version, applied by [`apply_chain`] like the store's. Its `frame_vec` table is a
/// `vec0` virtual table, so a connection applying it needs the `sqlite-vec` extension
/// registered first.
pub mod frames {
    use rusqlite::Transaction;

    use super::Migration::{self, Sql, Step};
    use super::{SchemaError, Shrinks};

    /// Width of one frame vector: CLIP ViT-L/14's projection, the model the encoder
    /// serves (ADR-0025). `frame_vec` fixes its column's width when it is created, so
    /// a model of another width is a migration, not a setting.
    pub const FRAME_DIMS: usize = 768;

    /// Append-only, like the store's chain, and versioned separately from it.
    pub const MIGRATIONS: &[Migration] = &[
        Sql(include_str!("../frames/v01.sql")),
        Sql(include_str!("../frames/v02.sql")),
        Step(v3),
        Sql(include_str!("../frames/v04.sql")),
    ];

    pub const SCHEMA_VERSION: i64 = MIGRATIONS.len() as i64;

    /// Version 3 — each Shot's boundaries, stored by the harvester so a reader never
    /// re-derives them from `cuts`. The titles already indexed get their rows here, from
    /// the rule the harvester applied when it numbered them: the boundaries are 0, each
    /// distinct cut at least 500 ms after the boundary before it and before `duration_ms`
    /// (the last frame's offset when that is NULL), then the end. The rule is written out
    /// in this step, not imported, so a later change to the harvester's rule leaves this
    /// one as it ran.
    fn v3(tx: &Transaction) -> Result<Shrinks, SchemaError> {
        tx.execute_batch(
            "CREATE TABLE shots (
            item_id TEXT NOT NULL,
            shot_ix INTEGER NOT NULL,
            in_ms   INTEGER NOT NULL,
            out_ms  INTEGER NOT NULL,
            PRIMARY KEY (item_id, shot_ix)
        ) WITHOUT ROWID\n        ",
        )?;
        let titles: Vec<(String, Option<i64>)> = tx
            .prepare("SELECT item_id, duration_ms FROM frame_index_state WHERE model IS NOT NULL")?
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?;
        for (item_id, duration_ms) in titles {
            let duration_ms = match duration_ms {
                Some(ms) => Some(ms),
                None => tx.query_row(
                    "SELECT max(t_ms) FROM frame_vec WHERE item_id = ?1",
                    [&item_id],
                    |row| row.get::<_, Option<i64>>(0),
                )?,
            };
            let Some(duration_ms) = duration_ms else {
                continue;
            };
            let mut bounds = vec![0_i64];
            let cuts: Vec<i64> = tx
                .prepare("SELECT DISTINCT t_ms FROM cuts WHERE item_id = ?1 ORDER BY t_ms")?
                .query_map([&item_id], |row| row.get(0))?
                .collect::<Result<_, _>>()?;
            for t_ms in cuts {
                if 500 <= t_ms - bounds[bounds.len() - 1] && t_ms < duration_ms {
                    bounds.push(t_ms);
                }
            }
            bounds.push(duration_ms);
            let mut insert = tx.prepare(
                "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?1, ?2, ?3, ?4)",
            )?;
            for (ix, pair) in bounds.windows(2).enumerate() {
                insert.execute(rusqlite::params![item_id, ix as i64, pair[0], pair[1]])?;
            }
        }
        Ok(Vec::new())
    }
}
