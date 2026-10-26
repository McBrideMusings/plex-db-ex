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

/// One step of the chain.
pub enum Migration {
    Sql(&'static str),
    Step(fn(&Transaction) -> Result<(), SchemaError>),
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
pub fn apply(conn: &mut Connection) -> Result<(i64, i64), SchemaError> {
    apply_chain(conn, MIGRATIONS)
}

/// Bring a store up to the last of `chain`. [`apply`] passes [`MIGRATIONS`]; a
/// test passes a prefix of it to build a store at an older version, or a chain
/// with a bad last step to prove what a failure leaves behind.
///
/// Each step runs in one transaction with its version row, so a process killed
/// mid-migration leaves the store at the last step that fully landed rather than
/// half-built.
pub fn apply_chain(conn: &mut Connection, chain: &[Migration]) -> Result<(i64, i64), SchemaError> {
    let target = chain.len() as i64;
    let start = preflight(conn, chain)?;
    if start == target {
        return Ok((start, start));
    }
    for (index, migration) in chain.iter().enumerate().skip(start as usize) {
        let tx = conn.transaction()?;
        // Inside the step's transaction, so a first step that fails leaves an
        // empty file rather than a version table with no row, which reads as
        // damaged.
        tx.execute_batch("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")?;
        match migration {
            Sql(sql) => tx.execute_batch(sql)?,
            Step(step) => step(&tx)?,
        }
        tx.execute("DELETE FROM schema_version", [])?;
        tx.execute(
            "INSERT INTO schema_version (version) VALUES (?1)",
            [index as i64 + 1],
        )?;
        tx.commit()?;
    }
    Ok((start, target))
}

/// The version the store is at, or why `chain` will not be applied to it, decided
/// by reading alone. [`apply_chain`] runs this first; `plexdb migrate` runs it
/// before it copies the store, so a store the chain refuses costs no copy.
///
/// Refused: a store newer than the chain, a damaged one (see [`current_version`]),
/// and a store below v17 holding keyword rows when the chain crosses version 10 or
/// 17 — those steps re-normalize every keyword, and the keyword normalizer has not
/// moved to Rust yet (`keyword_steps`).
pub fn preflight(conn: &Connection, chain: &[Migration]) -> Result<i64, SchemaError> {
    let target = chain.len() as i64;
    let start = current_version(conn)?;
    if start > target {
        return Err(SchemaError::Refused(format!(
            "store is at schema version {start}, but this build only understands {target} — \
             upgrade plex-db-ex rather than writing with an older one"
        )));
    }
    // An empty store (version 0) has no keyword rows, nor yet a table to hold any.
    let renormalizes = if start == 0 {
        None
    } else if start < 10 && target >= 10 {
        Some(10)
    } else if start < 17 && target >= 17 {
        Some(17)
    } else {
        None
    };
    if let Some(version) = renormalizes {
        let rows = keyword_steps::keyword_rows(conn)?;
        if rows > 0 {
            return Err(SchemaError::Refused(format!(
                "migration {version} re-normalizes this store's {rows} keyword rows, and keyword \
                 normalization has not moved to the Rust writer yet, so this build cannot migrate \
                 a store below v17 that holds keywords"
            )));
        }
    }
    Ok(start)
}
