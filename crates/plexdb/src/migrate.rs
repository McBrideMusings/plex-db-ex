//! `plexdb migrate` — bring the store up to the current schema, creating it if it
//! does not exist yet, with a copy taken first and kept.
//!
//! The store holds twenty months of watch history and a rate-limited TMDB sweep,
//! neither of which a re-scan reproduces. [`plexdb_schema::apply`] runs every step
//! in a transaction, so a process killed partway leaves the file at the last step
//! that landed — but that protects against a crash, not against a migration that
//! is simply wrong. Bad SQL commits happily. So a migration copies the store
//! first, checks it afterwards, and puts the copy back when the check fails.
//!
//! The container runs this as it starts (`plexdb/schedule.py`) and every sweep
//! runs it first, so a store already current costs one read and no copy.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use plexdb_schema::{
    GUARDED_TABLES, MIGRATIONS, Migration as Step, apply_chain, current_version, preflight,
};
use plexdb_store::{Config, MigrateLock, StoreError, create_store, open_store};
use rusqlite::Connection;

pub const NAME: &str = "migrate";

/// Row counts for every guarded table that exists yet, by name. A table missing
/// from the store is left out rather than counted as zero: before the migration
/// that creates it, absent and empty are the same thing.
pub type Counts = Vec<(String, i64)>;

/// What a migration did, in enough detail to print or to recover from.
#[derive(Debug)]
pub struct Migration {
    pub was: i64,
    pub now: i64,
    /// The copy taken before anything was applied, or `None` when there was
    /// nothing to copy — a store that did not exist yet, or one already current.
    pub backup: Option<PathBuf>,
    pub counts_before: Counts,
    pub counts_after: Counts,
}

pub fn guarded_counts(conn: &Connection) -> Result<Counts, StoreError> {
    let mut counts = Vec::new();
    for table in GUARDED_TABLES {
        let present: bool = conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)",
            [table],
            |row| row.get(0),
        )?;
        if present {
            let rows: i64 =
                conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                    row.get(0)
                })?;
            counts.push((table.to_string(), rows));
        }
    }
    Ok(counts)
}

/// `<stem>.db`, or `<stem>.2.db` if that name is taken. A migration can be
/// attempted, rolled back and attempted again, all at the same target version;
/// the second attempt must not overwrite the copy taken before the first, the
/// older and so the more valuable of the two.
fn unique_path(directory: &Path, stem: &str) -> PathBuf {
    let mut candidate = directory.join(format!("{stem}.db"));
    let mut attempt = 2;
    while candidate.exists() {
        candidate = directory.join(format!("{stem}.{attempt}.db"));
        attempt += 1;
    }
    candidate
}

/// Write a self-contained copy of the store into `backup_dir` with `VACUUM
/// INTO`: one file with no `-wal` or `-shm` sidecars, read through a normal
/// connection, so a copy taken while another process writes still lands
/// consistent. A plain file copy can miss committed rows still in the sidecar.
fn take_backup(store: &Path, backup_dir: &Path, stem: &str) -> Result<PathBuf, StoreError> {
    std::fs::create_dir_all(backup_dir).map_err(|err| {
        StoreError::Store(format!("cannot create {}: {err}", backup_dir.display()))
    })?;
    let destination = unique_path(backup_dir, stem);
    let copied = Connection::open(store)
        .and_then(|conn| conn.execute("VACUUM INTO ?1", [destination.to_string_lossy().as_ref()]));
    if let Err(err) = copied {
        let _ = std::fs::remove_file(&destination);
        return Err(StoreError::Store(format!(
            "cannot back up {}: {err}",
            store.display()
        )));
    }
    Ok(destination)
}

/// Put a backup back over the store. The `-wal` and `-shm` sidecars describe the
/// file being replaced, and SQLite reading a stale sidecar beside a different
/// database is how a restore turns into a corruption, so they go.
fn restore(backup: &Path, store: &Path) -> Result<(), StoreError> {
    if !backup.is_file() {
        return Err(StoreError::Store(format!(
            "cannot restore {} from {}: the backup does not exist",
            store.display(),
            backup.display()
        )));
    }
    std::fs::copy(backup, store).map_err(|err| {
        StoreError::Store(format!(
            "cannot restore {} from {}: {err}",
            store.display(),
            backup.display()
        ))
    })?;
    for suffix in ["-wal", "-shm"] {
        let mut sidecar = store.as_os_str().to_os_string();
        sidecar.push(suffix);
        match std::fs::remove_file(PathBuf::from(sidecar)) {
            Err(err) if err.kind() != std::io::ErrorKind::NotFound => return Err(err.into()),
            _ => {}
        }
    }
    Ok(())
}

/// Put the backup back after a failed migration, and say why it was needed. When
/// the copy back fails too, the error names both, so the reason the store was
/// being rolled back is not lost behind the reason the rollback failed.
fn roll_back(backup: &Path, store: &Path, reason: String) -> StoreError {
    match restore(backup, store) {
        Ok(()) => StoreError::Store(reason),
        Err(err) => StoreError::Store(format!(
            "{reason} — but the rollback failed, so {} may be half-migrated: {err}",
            store.display()
        )),
    }
}

/// `n` with thousands separators, as Python's `{n:,}` prints it.
pub fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// Check a just-migrated store, returning the first problem or `None`, in the
/// order where a failure of one makes the next meaningless: is the store at the
/// version it claims, is the file structurally sound, and is the history still
/// there — no guarded table holds fewer rows than before.
fn verify(conn: &Connection, target: i64, before: &Counts) -> Result<Option<String>, StoreError> {
    let now = current_version(conn)?;
    if now != target {
        return Ok(Some(format!(
            "store reports schema v{now} after migrating to v{target}"
        )));
    }
    let check: String = conn.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
    if check != "ok" {
        return Ok(Some(format!(
            "sqlite reports the migrated store is damaged: {check}"
        )));
    }
    let after = guarded_counts(conn)?;
    for (table, count_before) in before {
        let count_after = after
            .iter()
            .find(|(name, _)| name == table)
            .map_or(0, |(_, rows)| *rows);
        if count_after < *count_before {
            return Ok(Some(format!(
                "{table} went from {} rows to {} — a migration lost {} rows",
                thousands(*count_before),
                thousands(count_after),
                thousands(count_before - count_after)
            )));
        }
    }
    Ok(None)
}

/// Bring the store at `path` up to [`plexdb_schema::SCHEMA_VERSION`].
pub fn migrate(path: &Path, backup_dir: &Path) -> Result<Migration, StoreError> {
    migrate_chain(path, backup_dir, MIGRATIONS)
}

/// Bring the store up to the last of `chain`, holding the migration lock
/// throughout. A test passes its own chain, as [`plexdb_schema::apply_chain`]
/// takes one.
///
/// A store already current is not copied: every sweep and every container start
/// runs this, and a backup each time would fill the disk with identical files.
/// When the migration fails, or lands a store that fails [`verify`], the copy is
/// put back and the error names it.
pub fn migrate_chain(
    path: &Path,
    backup_dir: &Path,
    chain: &[Step],
) -> Result<Migration, StoreError> {
    let _lock = MigrateLock::exclusive(path)?;
    let target = chain.len() as i64;
    let mut counts_before = Counts::new();
    let mut taken = None;

    if path.exists() {
        let store = open_store(path)?;
        let start = preflight(&store.conn, chain)?;
        counts_before = guarded_counts(&store.conn)?;
        drop(store);
        if start == target {
            return Ok(Migration {
                was: start,
                now: start,
                backup: None,
                counts_after: counts_before.clone(),
                counts_before,
            });
        }
        taken = Some(take_backup(
            path,
            backup_dir,
            &format!("plexdb.pre-v{target}"),
        )?);
    }

    let landed = (|| {
        let mut store = create_store(path)?;
        let (was, now) = apply_chain(&mut store.conn, chain)?;
        let problem = match taken {
            Some(_) => verify(&store.conn, target, &counts_before)?,
            None => None,
        };
        let counts_after = guarded_counts(&store.conn)?;
        Ok::<_, StoreError>((was, now, problem, counts_after))
    })();

    let (was, now, problem, counts_after) = match (landed, &taken) {
        (Ok(landed), _) => landed,
        (Err(err), Some(backup)) => {
            return Err(roll_back(
                backup,
                path,
                format!(
                    "migration failed and the store was rolled back from {}: {err}",
                    backup.display()
                ),
            ));
        }
        (Err(err), None) => return Err(err),
    };
    if let (Some(problem), Some(backup)) = (problem, &taken) {
        return Err(roll_back(
            backup,
            path,
            format!("migration rolled back from {}: {problem}", backup.display()),
        ));
    }
    Ok(Migration {
        was,
        now,
        backup: taken,
        counts_before,
        counts_after,
    })
}

/// What `plexdb migrate` prints about one migration of the store at `where_`.
pub fn report(result: &Migration, where_: &Path) -> String {
    let at = where_.display();
    if result.was == result.now && result.was > 0 {
        return format!("store already current at schema v{}: {at}\n", result.now);
    }
    if result.was == 0 {
        return format!("created store at schema v{}: {at}\n", result.now);
    }
    let mut out = String::new();
    if let Some(backup) = &result.backup {
        let _ = writeln!(out, "backed up to {}", backup.display());
    }
    let _ = writeln!(
        out,
        "migrated store v{} -> v{}: {at}",
        result.was, result.now
    );
    let mut after = result.counts_after.clone();
    after.sort();
    for (table, rows) in after {
        let before = result
            .counts_before
            .iter()
            .find(|(name, _)| *name == table)
            .map(|(_, rows)| *rows);
        let _ = match before {
            None => writeln!(out, "  {table}: {} rows (new)", thousands(rows)),
            Some(before) if before == rows => writeln!(out, "  {table}: {} rows", thousands(rows)),
            Some(before) => writeln!(
                out,
                "  {table}: {} -> {} rows",
                thousands(before),
                thousands(rows)
            ),
        };
    }
    out
}

/// The `migrate` step: the configured store, its configured backup directory.
pub fn run() -> Result<(), StoreError> {
    let config = Config::from_env()?;
    let result = migrate(&config.store_path, &config.backup_dir)?;
    let where_ = std::fs::canonicalize(&config.store_path).unwrap_or(config.store_path);
    print!("{}", report(&result, &where_));
    Ok(())
}
