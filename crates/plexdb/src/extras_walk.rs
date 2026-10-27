//! `extras-walk`: list the files under each declared extras library and write them into
//! `items`, `extra_item` and `extra_file` (ADR-0030).
//!
//! A file is a row keyed by its path. One whose size and mtime match its row is only
//! stamped `last_seen`; any other is read in full for its SHA-256. A new path whose
//! SHA-256 matches a known file of the same library takes that file's `item_id`: when the
//! known path is gone from disk the file moved (the row's path changes, the id stays), and
//! when it is still there the new path is a copy that joins the item. Anything else is a
//! new item keyed `fs:<fnv1a of the canonical path>`. The first walk of a library with no
//! items marks what it finds organized; every file found after that waits in the queue.
//! A file the walk did not find gets `missing_since`; a library whose root cannot be read
//! is skipped whole, so an unmounted share marks nothing missing.

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

use plexdb_store::StoreError;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use sha2::{Digest, Sha256};

use crate::identity::{canonical_path, fnv1a_64};

pub const NAME: &str = "extras-walk";

/// The `items.type` of an extra.
pub const ITEM_TYPE: &str = "extra";

/// A file on disk, as one listing saw it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Found {
    pub path: String,
    pub size: i64,
    pub mtime: i64,
}

/// What one library's walk did.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LibraryStats {
    pub name: String,
    /// Why the library was not walked, or empty.
    pub skipped: String,
    pub listed: usize,
    pub hashed: usize,
    pub new_items: usize,
    /// New items marked organized because the library had none yet.
    pub first_scan: bool,
    pub copies_joined: usize,
    pub moved: usize,
    pub gone_missing: usize,
    pub returned: usize,
    pub unreadable: usize,
    /// New paths whose `fs:` id belongs to a title that is not an extra.
    pub id_conflicts: usize,
}

/// Every regular file under `root`, skipping names that start with a dot and symlinks.
pub fn list_files(root: &Path) -> std::io::Result<Vec<Found>> {
    let mut found = Vec::new();
    let mut pending: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            if entry.file_name().to_string_lossy().starts_with('.') {
                continue;
            }
            let kind = entry.file_type()?;
            if kind.is_dir() {
                pending.push(entry.path());
            } else if kind.is_file() {
                let meta = entry.metadata()?;
                let mtime = meta
                    .modified()
                    .ok()
                    .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
                    .map_or(0, |since| since.as_secs() as i64);
                found.push(Found {
                    path: entry.path().to_string_lossy().into_owned(),
                    size: meta.len() as i64,
                    mtime,
                });
            }
        }
    }
    found.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(found)
}

/// The SHA-256 of the whole file, as lowercase hex.
pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// The paths in `found` whose row is missing or whose size or mtime differ: the ones to hash.
fn needing_hash(conn: &Connection, found: &[Found]) -> Result<Vec<String>, StoreError> {
    let mut stmt = conn.prepare("SELECT size, mtime FROM extra_file WHERE path = ?")?;
    let mut need = Vec::new();
    for file in found {
        let row: Option<(Option<i64>, Option<i64>)> = stmt
            .query_row([&file.path], |row| Ok((row.get(0)?, row.get(1)?)))
            .optional()?;
        if row != Some((Some(file.size), Some(file.mtime))) {
            need.push(file.path.clone());
        }
    }
    Ok(need)
}

/// Write one library's listing. `hashes` holds the SHA-256 of each path `needing_hash`
/// named that could be read.
pub fn apply(
    conn: &mut Connection,
    library: &str,
    found: &[Found],
    hashes: &HashMap<String, String>,
    source_roots: &[String],
    now: &str,
) -> Result<LibraryStats, StoreError> {
    let mut stats = LibraryStats {
        name: library.to_string(),
        listed: found.len(),
        hashed: hashes.len(),
        ..LibraryStats::default()
    };
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let items: i64 = tx.query_row(
        "SELECT COUNT(*) FROM extra_item WHERE library = ?",
        [library],
        |row| row.get(0),
    )?;
    stats.first_scan = items == 0;
    let on_disk: HashSet<&str> = found.iter().map(|file| file.path.as_str()).collect();
    let mut moved_from: HashSet<String> = HashSet::new();
    for file in found {
        let row: Option<(Option<i64>, Option<i64>, Option<String>)> = tx
            .query_row(
                "SELECT size, mtime, missing_since FROM extra_file WHERE path = ?",
                [&file.path],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        if let Some((size, mtime, missing_since)) = row {
            let back = missing_since.is_some();
            if size == Some(file.size) && mtime == Some(file.mtime) {
                stats.returned += back as usize;
                tx.execute(
                    "UPDATE extra_file SET last_seen = ?, missing_since = NULL WHERE path = ?",
                    params![now, file.path],
                )?;
            } else if let Some(sha) = hashes.get(&file.path) {
                stats.returned += back as usize;
                tx.execute(
                    "UPDATE extra_file SET size = ?, mtime = ?, sha256 = ?, last_seen = ?, \
                     missing_since = NULL WHERE path = ?",
                    params![file.size, file.mtime, sha, now, file.path],
                )?;
            } else {
                stats.unreadable += 1;
            }
            continue;
        }
        let Some(sha) = hashes.get(&file.path) else {
            stats.unreadable += 1;
            continue;
        };
        let known: Vec<(String, String)> = {
            let mut stmt = tx.prepare(
                "SELECT f.path, f.item_id FROM extra_file f \
                 JOIN extra_item i ON i.item_id = f.item_id \
                 WHERE i.library = ? AND f.sha256 = ? ORDER BY f.path",
            )?;
            stmt.query_map(params![library, sha], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<_, _>>()?
        };
        let gone = known
            .iter()
            .find(|(path, _)| !on_disk.contains(path.as_str()) && !moved_from.contains(path));
        if let Some((old_path, _)) = gone {
            tx.execute(
                "UPDATE extra_file SET path = ?, size = ?, mtime = ?, last_seen = ?, \
                 missing_since = NULL WHERE path = ?",
                params![file.path, file.size, file.mtime, now, old_path],
            )?;
            moved_from.insert(old_path.clone());
            stats.moved += 1;
        } else if let Some((_, item_id)) = known.first() {
            insert_file(&tx, &file.path, item_id, false, file, sha, now)?;
            stats.copies_joined += 1;
        } else {
            let canonical = canonical_path(&file.path, source_roots);
            let item_id = format!("fs:{:016x}", fnv1a_64(&canonical));
            let taken: bool = tx.query_row(
                "SELECT EXISTS (SELECT 1 FROM items WHERE item_id = ?)",
                [&item_id],
                |row| row.get(0),
            )?;
            match taken {
                true => stats.id_conflicts += 1,
                false => {
                    let stem = Path::new(&file.path)
                        .file_stem()
                        .map_or_else(String::new, |stem| stem.to_string_lossy().into_owned());
                    tx.execute(
                        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
                        params![item_id, ITEM_TYPE, stem],
                    )?;
                    tx.execute(
                        "INSERT INTO extra_item (item_id, library, organized, first_seen) \
                         VALUES (?, ?, ?, ?)",
                        params![item_id, library, stats.first_scan as i64, now],
                    )?;
                    insert_file(&tx, &file.path, &item_id, true, file, sha, now)?;
                    stats.new_items += 1;
                }
            }
        }
    }
    let seen_before: Vec<String> = {
        let mut stmt = tx.prepare(
            "SELECT f.path FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
             WHERE i.library = ? AND f.missing_since IS NULL",
        )?;
        stmt.query_map([library], |row| row.get(0))?
            .collect::<Result<_, _>>()?
    };
    for path in seen_before
        .iter()
        .filter(|path| !on_disk.contains(path.as_str()))
    {
        tx.execute(
            "UPDATE extra_file SET missing_since = ? WHERE path = ?",
            params![now, path],
        )?;
        stats.gone_missing += 1;
    }
    choose_primaries(&tx, library)?;
    tx.commit()?;
    Ok(stats)
}

fn insert_file(
    tx: &Connection,
    path: &str,
    item_id: &str,
    primary: bool,
    file: &Found,
    sha: &str,
    now: &str,
) -> Result<(), StoreError> {
    tx.execute(
        "INSERT INTO extra_file (path, item_id, primary_file, size, mtime, sha256, first_seen, \
         last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        params![
            path,
            item_id,
            primary as i64,
            file.size,
            file.mtime,
            sha,
            now,
            now
        ],
    )?;
    Ok(())
}

/// Each item's primary file is its best one: a file on disk before a missing one, then the
/// greatest height, then the largest size, then the path.
fn choose_primaries(tx: &Connection, library: &str) -> Result<(), StoreError> {
    const BEST: &str = "(SELECT g.path FROM extra_file g WHERE g.item_id = extra_file.item_id \
        ORDER BY g.missing_since IS NOT NULL, COALESCE(g.height, -1) DESC, \
        COALESCE(g.size, -1) DESC, g.path LIMIT 1)";
    const OWN: &str = "item_id IN (SELECT item_id FROM extra_item WHERE library = ?1)";
    tx.execute(
        &format!("UPDATE extra_file SET primary_file = 0 WHERE primary_file = 1 AND {OWN} AND path <> {BEST}"),
        [library],
    )?;
    tx.execute(
        &format!("UPDATE extra_file SET primary_file = 1 WHERE primary_file = 0 AND {OWN} AND path = {BEST}"),
        [library],
    )?;
    Ok(())
}

/// The libraries last folded, by name, with their roots.
pub fn stored_libraries(conn: &Connection) -> Result<Vec<(String, String)>, StoreError> {
    let mut stmt = conn.prepare("SELECT name, root FROM extra_library ORDER BY name")?;
    let rows = stmt
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    Ok(rows)
}

/// One library's listing and the hashes of the files that need one, read without
/// writing the store.
pub struct Prepared {
    pub name: String,
    pub root: String,
    pub skipped: String,
    pub found: Vec<Found>,
    pub hashes: HashMap<String, String>,
}

/// List every stored library, or the one named `only`, and hash the files that are new or
/// changed. Only reads `conn`, so a caller can do this, the slow part, without holding the
/// writer lock.
pub fn prepare(conn: &Connection, only: Option<&str>) -> Result<Vec<Prepared>, StoreError> {
    let mut all = Vec::new();
    for (name, root) in stored_libraries(conn)? {
        if only.is_some_and(|only| only != name) {
            continue;
        }
        let mut prepared = Prepared {
            name,
            root: root.clone(),
            skipped: String::new(),
            found: Vec::new(),
            hashes: HashMap::new(),
        };
        if !Path::new(&root).is_dir() {
            prepared.skipped = format!("{root} is not a directory");
        } else {
            match list_files(Path::new(&root)) {
                Ok(found) => prepared.found = found,
                Err(err) => prepared.skipped = format!("cannot list {root}: {err}"),
            }
        }
        if prepared.skipped.is_empty() {
            for path in needing_hash(conn, &prepared.found)? {
                match sha256_file(Path::new(&path)) {
                    Ok(sha) => {
                        prepared.hashes.insert(path, sha);
                    }
                    Err(err) => eprintln!("extras-walk: cannot read {path}: {err}"),
                }
            }
        }
        all.push(prepared);
    }
    Ok(all)
}

/// Write what [`prepare`] read. A library whose stored root changed since is left for the
/// next walk.
pub fn apply_prepared(
    conn: &mut Connection,
    prepared: &[Prepared],
    source_roots: &[String],
    now: &str,
) -> Result<Vec<LibraryStats>, StoreError> {
    let current: HashMap<String, String> = stored_libraries(conn)?.into_iter().collect();
    let mut all = Vec::new();
    for lib in prepared {
        let skipped = if !lib.skipped.is_empty() {
            lib.skipped.clone()
        } else if current.get(&lib.name) != Some(&lib.root) {
            "its declaration changed during the walk".into()
        } else {
            all.push(apply(
                conn,
                &lib.name,
                &lib.found,
                &lib.hashes,
                source_roots,
                now,
            )?);
            continue;
        };
        all.push(LibraryStats {
            name: lib.name.clone(),
            skipped,
            ..LibraryStats::default()
        });
    }
    Ok(all)
}

/// Walk every stored library, or the one named `only`.
pub fn walk_all(
    conn: &mut Connection,
    source_roots: &[String],
    only: Option<&str>,
    now: &str,
) -> Result<Vec<LibraryStats>, StoreError> {
    let prepared = prepare(conn, only)?;
    apply_prepared(conn, &prepared, source_roots, now)
}

/// The names in `stats`, for a report line.
pub fn describe(stats: &[LibraryStats]) -> Vec<String> {
    if stats.is_empty() {
        return vec!["extras-walk: no library declared".into()];
    }
    stats
        .iter()
        .map(|lib| {
            if !lib.skipped.is_empty() {
                return format!("extras-walk {}: skipped, {}", lib.name, lib.skipped);
            }
            let organized = if lib.first_scan {
                ", first scan: all organized"
            } else {
                ""
            };
            format!(
                "extras-walk {}: {} file(s) listed, {} read in full, {} new item(s){organized}, \
                 {} copy(ies) joined, {} moved, {} gone missing, {} back, {} unreadable, \
                 {} id conflict(s)",
                lib.name,
                lib.listed,
                lib.hashed,
                lib.new_items,
                lib.copies_joined,
                lib.moved,
                lib.gone_missing,
                lib.returned,
                lib.unreadable,
                lib.id_conflicts
            )
        })
        .collect()
}
