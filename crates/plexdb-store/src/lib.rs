//! What every writer of `plexdb.db` shares, whichever language it is in: where the
//! store is, how a writing connection opens, and the writer lock (ADR-0001).
//!
//! The Python package (`plexdb/store.py`, `plexdb/config.py`) holds the same rules
//! while sweep steps move to Rust one at a time. Both take the same `flock` on the
//! same file, so `plexdb idle` and `admin host-exec` see a Rust step as a writer.

use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::Connection;
use thiserror::Error;

/// Where the store lands when `PLEXDB_PATH` says nothing (`plexdb/config.py`).
pub const DEFAULT_STORE_PATH: &str = "./data/plexdb.db";

/// The busy timeout every writing connection sets (`plexdb/store.py`
/// `BUSY_TIMEOUT_MS`): room to wait out one write batch from another writer.
pub const BUSY_TIMEOUT_MS: u64 = 5000;

/// Names a store whose writer lock the parent process already holds exclusive
/// (`store.holding`, which `plexdb idle <command>` takes). A child that took its
/// own shared lock on a new file handle would wait on its parent forever, so it
/// stands down for the store this names.
pub const HELD_STORE_VAR: &str = "PLEXDB_HELD_STORE";

/// Points the decisions files at Plex DB-EX's own mount (`plexdb/decisions.py`).
pub const EXPLORE_SAVED_PATH_VAR: &str = "PLEXDB_EXPLORE_SAVED_PATH";

#[derive(Debug, Error)]
pub enum StoreError {
    /// A setting is missing or malformed. The sweep reports the step as skipped.
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Store(String),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

/// Python's `str.isspace`: Unicode `White_Space`, plus the four ASCII separators
/// (U+001C to U+001F) Python also counts.
pub fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.strip()` with no argument.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(is_py_space)
}

/// `Path(raw).expanduser()` as Python spells it: a leading `~` or `~/` becomes
/// `$HOME`, `.` components and repeated or trailing slashes go, and an empty path
/// is `.`. Printing the result gives the same text Python prints.
pub fn py_path(raw: &str) -> PathBuf {
    let expanded = match raw.strip_prefix('~') {
        Some(rest) if rest.is_empty() || rest.starts_with('/') => {
            format!("{}{rest}", std::env::var("HOME").unwrap_or_default())
        }
        _ => raw.to_string(),
    };
    // POSIX keeps exactly two leading slashes; one, or three and more, is root.
    let root = if expanded.starts_with("//") && !expanded.starts_with("///") {
        "//"
    } else if expanded.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = expanded
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    let text = format!("{root}{}", parts.join("/"));
    PathBuf::from(if text.is_empty() { "." } else { &text })
}

/// `Path.parent`: the path itself for a root, `.` for a bare name.
fn py_parent(path: &Path) -> PathBuf {
    match path.parent() {
        None => path.to_path_buf(),
        Some(parent) if parent.as_os_str().is_empty() => PathBuf::from("."),
        Some(parent) => parent.to_path_buf(),
    }
}

/// `parent / name` as Python joins it: `Path('.') / 'x'` is `x`.
fn py_join(parent: &Path, name: &str) -> PathBuf {
    if parent == Path::new(".") {
        PathBuf::from(name)
    } else {
        parent.join(name)
    }
}

fn env_text(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|raw| py_strip(&raw).to_string())
}

/// The part of `plexdb/config.py`'s `Config` a store-writing step needs.
#[derive(Debug, Clone)]
pub struct Config {
    pub store_path: PathBuf,
    /// `PLEXDB_SNAPSHOT_PATH`, or `None` when unset.
    pub snapshot_path: Option<PathBuf>,
}

impl Config {
    /// Read the process environment. `.env` is not read here: the Python `plexdb`
    /// that hands a step over has already loaded it into the environment this
    /// process inherits.
    pub fn from_env() -> Result<Self, StoreError> {
        let raw = match std::env::var("PLEXDB_PATH") {
            Ok(raw) => py_strip(&raw).to_string(),
            Err(_) => DEFAULT_STORE_PATH.to_string(),
        };
        if raw.is_empty() {
            return Err(StoreError::Config(
                "PLEXDB_PATH is set but empty; unset it or give it a path".into(),
            ));
        }
        let snapshot_path = env_text("PLEXDB_SNAPSHOT_PATH")
            .filter(|raw| !raw.is_empty())
            .map(|raw| py_path(&raw));
        Ok(Config {
            store_path: py_path(&raw),
            snapshot_path,
        })
    }

    /// Where Plex DB-EX's files sit (`decisions.decisions_dir`): the directory of
    /// `PLEXDB_EXPLORE_SAVED_PATH` when set, else beside the snapshot when one is
    /// configured, else beside the store.
    pub fn decisions_dir(&self) -> PathBuf {
        if let Some(raw) = env_text(EXPLORE_SAVED_PATH_VAR).filter(|raw| !raw.is_empty()) {
            return py_parent(&py_path(&raw));
        }
        py_parent(self.snapshot_path.as_ref().unwrap_or(&self.store_path))
    }

    /// `name` inside `decisions_dir`.
    pub fn decisions_file(&self, name: &str) -> PathBuf {
        py_join(&self.decisions_dir(), name)
    }
}

/// `<store>.write-lock`, the file both writers `flock`.
pub fn write_lock_path(store: &Path) -> PathBuf {
    let mut name = store.file_name().unwrap_or_default().to_os_string();
    name.push(".write-lock");
    store.with_file_name(name)
}

fn ensure_parent(path: &Path) -> Result<(), StoreError> {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => std::fs::create_dir_all(parent)
            .map_err(|err| StoreError::Store(format!("cannot create {}: {err}", parent.display()))),
        _ => Ok(()),
    }
}

/// Whether the parent process holds this store exclusive (`HELD_STORE_VAR`).
fn held_by_parent(store: &Path) -> bool {
    let Some(held) = std::env::var_os(HELD_STORE_VAR) else {
        return false;
    };
    std::fs::canonicalize(store).is_ok_and(|resolved| resolved == Path::new(&held))
}

/// The writer lock, held shared for as long as this value lives (`store.writing`).
///
/// Shared, so writers never wait on each other here and SQLite serialises their
/// transactions; it waits only on an exclusive claim (`store.holding`), and it
/// is what `store.held_by` sees from outside the process.
#[derive(Debug)]
pub struct WriteLock {
    _file: Option<File>,
}

impl WriteLock {
    pub fn shared(store: &Path) -> Result<Self, StoreError> {
        if held_by_parent(store) {
            return Ok(WriteLock { _file: None });
        }
        ensure_parent(store)?;
        let file = OpenOptions::new()
            .append(true)
            .create(true)
            .open(write_lock_path(store))?;
        file.lock_shared()?;
        Ok(WriteLock { _file: Some(file) })
    }
}

/// A writing connection and the lock it was opened under. The connection closes
/// before the lock is released, as `open_store` does.
#[derive(Debug)]
pub struct Store {
    pub conn: Connection,
    _lock: WriteLock,
}

/// Open the store for writing (`store.open_store`): refuse a directory or a
/// missing file, take the writer lock, then open with WAL, the busy timeout and
/// foreign keys on.
pub fn open_store(path: &Path) -> Result<Store, StoreError> {
    if path.is_dir() {
        return Err(StoreError::Store(format!(
            "{} is a directory, not a store — point PLEXDB_PATH at a file",
            path.display()
        )));
    }
    if !path.exists() {
        return Err(StoreError::Store(format!(
            "no store at {} — run `plexdb migrate` first",
            path.display()
        )));
    }
    let lock = WriteLock::shared(path)?;
    let conn = Connection::open(path)
        .map_err(|err| StoreError::Store(format!("cannot open {}: {err}", path.display())))?;
    configure(&conn).map_err(|err| {
        StoreError::Store(format!("{} is not a plexdb store: {err}", path.display()))
    })?;
    Ok(Store { conn, _lock: lock })
}

fn configure(conn: &Connection) -> rusqlite::Result<()> {
    // WAL lets a reader work while a writer is mid-sweep, the normal state of
    // this store.
    conn.pragma_update_and_check(None, "journal_mode", "WAL", |_| Ok(()))?;
    conn.busy_timeout(Duration::from_millis(BUSY_TIMEOUT_MS))?;
    conn.pragma_update(None, "foreign_keys", "ON")
}

/// The schema version the store is at (`schema.current_version`): zero when no
/// migration has run, an error when the version table has no row.
pub fn schema_version(conn: &Connection) -> Result<i64, StoreError> {
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
        None => Err(StoreError::Store(
            "store has a schema_version table but no version row — it is damaged. Restore it \
             from a backup, or delete the file and re-run `plexdb migrate` to rebuild it from Plex."
                .into(),
        )),
    }
}
