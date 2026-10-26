//! `publish`: write a read-only snapshot of the store for consumers (`plexdb/store.py`
//! `publish`, ADR-0007).
//!
//! `VACUUM INTO` writes a compacted, self-contained copy with no `-wal` or `-shm` sidecars,
//! in SQLite's default `DELETE` journal mode whatever the source's mode, so the copy opens
//! read-only from a directory with no write permission. It is written under a temporary name
//! beside the snapshot and renamed into place: a rename within one filesystem is atomic, a
//! handle already open on the old file keeps reading it, and a second publish replaces the
//! snapshot rather than failing.

use std::path::Path;

use plexdb_store::{Config, StoreError, ensure_parent, open_store, py_resolve};

use crate::gated::{config, store_failure};
use crate::steps::Failure;

pub const NAME: &str = "publish";

/// Publish the snapshot; returns the schema version and the byte size of the file written.
pub fn publish(store_path: &Path, snapshot_path: &Path) -> Result<(i64, u64), StoreError> {
    ensure_parent(snapshot_path)?;
    let mut name = snapshot_path
        .file_name()
        .ok_or_else(|| StoreError::Store(format!("{} names no file", snapshot_path.display())))?
        .to_os_string();
    name.push(".tmp");
    let tmp = snapshot_path.with_file_name(name);
    // VACUUM INTO refuses to write over an existing file, so clear one a publish that
    // crashed mid-write left behind.
    let _ = std::fs::remove_file(&tmp);
    let store = open_store(store_path)?;
    if let Err(err) = store
        .conn
        .execute("VACUUM INTO ?1", [tmp.to_string_lossy().as_ref()])
    {
        let _ = std::fs::remove_file(&tmp);
        return Err(StoreError::Store(format!("cannot publish snapshot: {err}")));
    }
    let version = plexdb_schema::current_version(&store.conn)?;
    std::fs::rename(&tmp, snapshot_path)?;
    Ok((version, std::fs::metadata(snapshot_path)?.len()))
}

/// `plexdb publish`.
pub fn run(args: &[String]) -> Result<(), Failure> {
    if !args.is_empty() {
        return Err(Failure::Usage(format!(
            "{NAME} takes no arguments, got {}",
            args.join(" ")
        )));
    }
    let config: Config = config()?;
    let Some(snapshot) = config.snapshot_path.as_ref() else {
        return Err(Failure::Config(
            "PLEXDB_SNAPSHOT_PATH is not set — point it at where the published snapshot should land"
                .into(),
        ));
    };
    let (version, size) = publish(&config.store_path, snapshot).map_err(store_failure)?;
    println!(
        "published snapshot at schema v{version}, {size} bytes: {}",
        py_resolve(snapshot).display()
    );
    Ok(())
}
