//! `plexdb migrate` copies the store before it changes the schema, keeps the
//! copy, and puts it back when the migration fails or loses history. The
//! transaction around each step catches a crash; these are the failures it
//! cannot see — SQL that runs and does the wrong thing.

use std::fs::File;
use std::path::{Path, PathBuf};

use plexdb::migrate::{migrate, migrate_chain, report};
use plexdb_schema::{MIGRATIONS, Migration, SCHEMA_VERSION, SchemaError, current_version};
use rusqlite::{Connection, Transaction};

fn chain_with(last: Migration) -> Vec<Migration> {
    let mut chain: Vec<Migration> = MIGRATIONS[..MIGRATIONS.len() - 1]
        .iter()
        .map(|migration| match migration {
            Migration::Sql(sql) => Migration::Sql(sql),
            Migration::Step(step) => Migration::Step(*step),
        })
        .collect();
    chain.push(last);
    chain
}

/// A real store one version behind, holding `plays` rows of history: every
/// migration but the last applied, so re-applying the last meets the shape it
/// shipped over.
fn store_one_behind(path: &Path, plays: i64) {
    let mut conn = Connection::open(path).expect("open");
    plexdb_schema::apply_chain(&mut conn, &MIGRATIONS[..MIGRATIONS.len() - 1]).expect("apply");
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')",
        [],
    )
    .expect("item");
    for n in 0..plays {
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) \
             VALUES (?1, 'imdb:tt1', 1, ?2)",
            (format!("h{n}"), 1000 + n),
        )
        .expect("play");
    }
}

fn count(path: &Path, table: &str) -> i64 {
    let conn = Connection::open(path).expect("open");
    conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
        row.get(0)
    })
    .expect("count")
}

fn version(path: &Path) -> i64 {
    current_version(&Connection::open(path).expect("open")).expect("version")
}

fn names(dir: &Path) -> Vec<String> {
    let mut found: Vec<String> = std::fs::read_dir(dir)
        .map(|entries| {
            entries
                .map(|entry| {
                    entry
                        .expect("entry")
                        .file_name()
                        .to_string_lossy()
                        .into_owned()
                })
                .collect()
        })
        .unwrap_or_default();
    found.sort();
    found
}

#[test]
fn creating_a_store_from_nothing_takes_no_backup() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("nested").join("plexdb.db");
    let backups = dir.path().join("backups");

    let result = migrate(&path, &backups).expect("migrate");

    assert_eq!((result.was, result.now), (0, SCHEMA_VERSION));
    assert!(result.backup.is_none());
    assert!(!backups.exists());
    assert_eq!(
        report(&result, &path),
        format!(
            "created store at schema v{SCHEMA_VERSION}: {}\n",
            path.display()
        )
    );
}

#[test]
fn migrating_an_already_current_store_takes_no_backup_and_writes_nothing() {
    // Every sweep and every container start runs it; a copy each time would fill
    // the disk with identical files.
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let backups = dir.path().join("backups");
    migrate(&path, &backups).expect("create");
    let before = std::fs::read(&path).expect("read");

    let result = migrate(&path, &backups).expect("migrate again");

    assert_eq!((result.was, result.now), (SCHEMA_VERSION, SCHEMA_VERSION));
    assert!(result.backup.is_none());
    assert!(!backups.exists());
    assert_eq!(std::fs::read(&path).expect("read"), before);
    assert!(report(&result, &path).starts_with("store already current at schema v"));
}

#[test]
fn a_real_migration_takes_a_backup_keeps_it_and_reports_the_counts() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let backups = dir.path().join("backups");
    store_one_behind(&path, 2);

    let result = migrate(&path, &backups).expect("migrate");

    let backup = backups.join(format!("plexdb.pre-v{SCHEMA_VERSION}.db"));
    assert_eq!(
        (result.was, result.now),
        (SCHEMA_VERSION - 1, SCHEMA_VERSION)
    );
    assert_eq!(result.backup.as_deref(), Some(backup.as_path()));
    assert_eq!(count(&backup, "plays"), 2);
    assert_eq!(version(&backup), SCHEMA_VERSION - 1);
    assert_eq!(
        report(&result, &path),
        format!(
            "backed up to {}\nmigrated store v{} -> v{SCHEMA_VERSION}: {}\n  enrichment: 0 rows\n  \
             items: 1 rows\n  plays: 2 rows\n",
            backup.display(),
            SCHEMA_VERSION - 1,
            path.display()
        )
    );
}

#[test]
fn a_migration_that_loses_history_is_rolled_back() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    store_one_behind(&path, 3);
    let chain = chain_with(Migration::Sql("DELETE FROM plays;"));

    let err = migrate_chain(&path, &dir.path().join("backups"), &chain)
        .expect_err("the row guard refuses it");

    assert!(err.to_string().contains("a migration lost 3 rows"), "{err}");
    assert!(err.to_string().contains("rolled back from"), "{err}");
    assert_eq!(count(&path, "plays"), 3);
    assert_eq!(version(&path), SCHEMA_VERSION - 1);
}

fn raises(_: &Transaction) -> Result<(), SchemaError> {
    Err(SchemaError::Refused("this step always fails".into()))
}

#[test]
fn a_migration_that_raises_is_rolled_back_and_names_the_backup() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let backups = dir.path().join("backups");
    store_one_behind(&path, 1);

    let err = migrate_chain(&path, &backups, &chain_with(Migration::Step(raises)))
        .expect_err("the step fails");

    let backup = backups.join(format!("plexdb.pre-v{SCHEMA_VERSION}.db"));
    assert!(
        err.to_string().contains(&format!(
            "rolled back from {}: this step always fails",
            backup.display()
        )),
        "{err}"
    );
    assert_eq!(count(&path, "plays"), 1);
}

#[test]
fn a_second_attempt_does_not_overwrite_the_first_attempts_backup() {
    // Attempted, rolled back and attempted again, all at one target version; the
    // copy taken before the first attempt is the older and more valuable one.
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let backups = dir.path().join("backups");
    store_one_behind(&path, 1);
    let chain = chain_with(Migration::Sql("DELETE FROM plays;"));
    migrate_chain(&path, &backups, &chain).expect_err("first attempt");

    migrate(&path, &backups).expect("second attempt");

    assert_eq!(
        names(&backups),
        [
            format!("plexdb.pre-v{SCHEMA_VERSION}.2.db"),
            format!("plexdb.pre-v{SCHEMA_VERSION}.db"),
        ]
    );
}

#[test]
fn a_table_the_migration_creates_is_not_reported_as_row_loss() {
    // Before the step that creates it, absent and empty are the same thing.
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let mut conn = Connection::open(&path).expect("open");
    plexdb_schema::apply_chain(&mut conn, &MIGRATIONS[..1]).expect("v1");
    drop(conn);

    let result = migrate(&path, &dir.path().join("backups")).expect("migrate");

    assert!(
        !result
            .counts_before
            .iter()
            .any(|(table, _)| table == "plays")
    );
    assert!(report(&result, &path).contains("  plays: 0 rows (new)\n"));
}

#[test]
fn a_store_from_the_future_is_refused_without_a_copy() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let backups = dir.path().join("backups");
    migrate(&path, &backups).expect("create");
    Connection::open(&path)
        .expect("open")
        .execute(
            "UPDATE schema_version SET version = ?1",
            [SCHEMA_VERSION + 5],
        )
        .expect("stamp");

    let err = migrate(&path, &backups).expect_err("a newer store");

    assert!(err.to_string().contains("only understands"), "{err}");
    assert!(!backups.exists());
}

fn lock_path(store: &Path) -> PathBuf {
    store.with_file_name("plexdb.db.migrate-lock")
}

/// Fails unless another process — here, another file handle — is refused the
/// migration lock while this step runs.
fn sees_the_lock_held(_: &Transaction) -> Result<(), SchemaError> {
    let dir = std::env::var("MIGRATE_LOCK_PROBE").expect("the test names the store");
    let handle = File::open(lock_path(Path::new(&dir))).expect("the lock file exists");
    match handle.try_lock_shared() {
        Err(std::fs::TryLockError::WouldBlock) => Ok(()),
        other => Err(SchemaError::Refused(format!(
            "the migration lock was free: {other:?}"
        ))),
    }
}

#[test]
fn migrate_holds_the_lock_a_check_waits_on_until_it_ends() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    store_one_behind(&path, 0);
    // SAFETY: this crate's other tests read no environment variable of this name.
    unsafe { std::env::set_var("MIGRATE_LOCK_PROBE", &path) };

    let chain = chain_with(Migration::Step(sees_the_lock_held));
    migrate_chain(&path, &dir.path().join("backups"), &chain).expect("held while it ran");

    let handle = File::open(lock_path(&path)).expect("lock file");
    handle.try_lock_shared().expect("released once it ended");
}

#[test]
fn a_store_the_chain_refuses_is_left_as_it_was_and_not_copied() {
    // Below v17 with keywords: the normalizer has not moved to Rust. A container
    // restarting on such a store must not add a copy per restart.
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let backups = dir.path().join("backups");
    let mut conn = Connection::open(&path).expect("open");
    plexdb_schema::apply_chain(&mut conn, &MIGRATIONS[..16]).expect("v16");
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat');
         INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at)
             VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', 'heist', '2026-01-01T00:00:00Z');",
    )
    .expect("seed");
    drop(conn);

    let err = migrate(&path, &backups).expect_err("refused");

    assert!(err.to_string().contains("keyword normalization"), "{err}");
    assert!(!backups.exists());
    assert_eq!(version(&path), 16);
}
