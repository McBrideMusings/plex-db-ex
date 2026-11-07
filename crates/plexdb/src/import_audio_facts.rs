//! `plexdb import-audio-facts <catalog.db> [--dry-run]` — seed `audio_facts` from etv-station's
//! `entry_audio` table (plex-db-ex-9ak.7), so `harvest-audio` finds facts for a file's hash and
//! records the part done without decoding it again.
//!
//! The catalog is opened read-only and its rows with facts are read whole. Each row's
//! `source_path` is mapped to a local file with [`local_path`] (`PLEXDB_MEDIA_DIR`,
//! `PLEX_SOURCE_ROOTS`) and hashed with the harvester's [`file_hash`] at the size harvest-audio
//! will use (the store's part or extra size for that path, else the file's length). An
//! unreadable media root stops the run before anything is written. All hashing
//! finishes before the store is written, so the writer lock is never held while a file is
//! read; the rows then go in [`BATCH`] per short transaction through [`open_store`]. The facts
//! text is stored exactly as the station wrote it (it is only parsed, to check it is a JSON
//! object), with the row's own `analyzed_at`. A hash that already has facts is left alone, and
//! when two rows hash the same the first one in `entry_id` order is kept.
//!
//! Exit 0 whenever the run completed, even with unreadable files; a missing setting, a bad
//! command line or a catalog without `entry_audio` is a failure.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use plexdb_store::{Config, StoreError, open_readonly, open_store};
use rusqlite::{Connection, OpenFlags, params};
use serde_json::Value;

use crate::frame_media::{file_hash, local_path};
use crate::frames::FrameConfig;
use crate::harvest_audio::media_root_error;
use crate::steps::{Failure, failure};

pub const NAME: &str = "import-audio-facts";

/// Rows written per transaction.
pub const BATCH: usize = 500;
const PROGRESS_EVERY: usize = 1000;

/// What a run did, one count per fate of a row.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Report {
    pub rows_read: usize,
    pub imported: usize,
    pub already_present: usize,
    pub duplicate_hash: usize,
    pub unmapped_path: usize,
    pub unreadable_file: usize,
    pub facts_not_object: usize,
    pub no_store_row: usize,
    pub dry_run: bool,
}

impl Report {
    pub fn describe(&self) -> String {
        let verb = if self.dry_run {
            "would import"
        } else {
            "imported"
        };
        let tail = if self.dry_run {
            " (dry run, nothing written)"
        } else {
            ""
        };
        format!(
            "{NAME}: {} rows read, {} {verb}, {} already present, {} duplicate hash within the \
             import, {} unmapped path, {} unreadable file, {} facts not a JSON object, {} with no store row (hashed at the file's length){tail}",
            self.rows_read,
            self.imported,
            self.already_present,
            self.duplicate_hash,
            self.unmapped_path,
            self.unreadable_file,
            self.facts_not_object,
            self.no_store_row,
        )
    }
}

struct Row {
    source_path: String,
    analyzed_at: String,
    facts: String,
}

fn catalog_failure(catalog: &Path, err: rusqlite::Error) -> Failure {
    Failure::Failed(format!("catalog {}: {err}", catalog.display()))
}

fn store_failure(err: rusqlite::Error) -> Failure {
    Failure::Failed(format!("store: {err}"))
}

/// The distinct sizes harvest-audio would hash `path` at (NULL is 0), from the Plex parts and
/// the primary, present extra files that carry it.
fn store_sizes(conn: &Connection, path: &str) -> Result<Vec<i64>, rusqlite::Error> {
    let mut sizes = Vec::new();
    for sql in [
        "SELECT size FROM media_part WHERE file = ?1",
        "SELECT size FROM extra_file WHERE path = ?1 AND primary_file = 1 \
         AND missing_since IS NULL",
    ] {
        let mut stmt = conn.prepare(sql)?;
        let found = stmt.query_map([path], |row| row.get::<_, Option<i64>>(0))?;
        for size in found {
            let size = size?.unwrap_or(0);
            if !sizes.contains(&size) {
                sizes.push(size);
            }
        }
    }
    Ok(sizes)
}

fn read_catalog(catalog: &Path) -> Result<Vec<Row>, Failure> {
    let conn = Connection::open_with_flags(
        catalog,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|err| catalog_failure(catalog, err))?;
    let has_table: bool = conn
        .query_row(
            "SELECT count(*) > 0 FROM sqlite_master WHERE type = 'table' AND name = 'entry_audio'",
            [],
            |row| row.get(0),
        )
        .map_err(|err| catalog_failure(catalog, err))?;
    if !has_table {
        return Err(Failure::Failed(format!(
            "catalog {} has no entry_audio table; give the path of etv-station's catalog.db",
            catalog.display()
        )));
    }
    let mut stmt = conn
        .prepare(
            "SELECT source_path, analyzed_at, facts FROM entry_audio \
             WHERE facts IS NOT NULL ORDER BY entry_id",
        )
        .map_err(|err| catalog_failure(catalog, err))?;
    let rows = stmt
        .query_map([], |row| {
            Ok(Row {
                source_path: row.get(0)?,
                analyzed_at: row.get(1)?,
                facts: row.get(2)?,
            })
        })
        .map_err(|err| catalog_failure(catalog, err))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| catalog_failure(catalog, err))?;
    Ok(rows)
}

/// `PLEXDB_MEDIA_DIR` and `PLEX_SOURCE_ROOTS`, with the messages `harvest-audio` gives.
pub fn media_settings() -> Result<(PathBuf, Vec<String>), StoreError> {
    let frames = FrameConfig::from_env()?;
    let media_dir = frames.media_dir.ok_or_else(|| {
        StoreError::Config(
            "PLEXDB_MEDIA_DIR is not set, so no audio file can be read; mount the library \
             read-only and name the directory Plex calls PLEX_SOURCE_ROOTS"
                .into(),
        )
    })?;
    if frames.source_roots.is_empty() {
        return Err(StoreError::Config(
            "PLEX_SOURCE_ROOTS is not set, so Plex's file paths cannot be mapped".into(),
        ));
    }
    Ok((media_dir, frames.source_roots))
}

/// Import `catalog`'s facts into `store`. With `dry_run` the store is only read.
pub fn import(
    store: &Path,
    catalog: &Path,
    media_dir: &Path,
    source_roots: &[String],
    dry_run: bool,
    log: &mut dyn FnMut(&str),
) -> Result<Report, Failure> {
    let rows = read_catalog(catalog)?;
    let mut report = Report {
        rows_read: rows.len(),
        dry_run,
        ..Report::default()
    };

    if let Some(reason) = media_root_error(media_dir) {
        return Err(Failure::Failed(reason));
    }
    // Hash everything first; no writer connection is open while files are read. The digest
    // includes the size, so each file is hashed at the size harvest-audio will use: the
    // store's `media_part.size` / `extra_file.size` for the path (NULL is 0), once per
    // distinct size, and the file's own length only when the store has no row for the path.
    let mut seen: HashSet<String> = HashSet::new();
    let mut candidates: Vec<(String, Row)> = Vec::new();
    let sizes = open_readonly(store).map_err(failure)?;
    for (index, row) in rows.into_iter().enumerate() {
        if index > 0 && index % PROGRESS_EVERY == 0 {
            log(&format!(
                "{NAME}: hashed {index} of {} rows",
                report.rows_read
            ));
        }
        if !matches!(
            serde_json::from_str::<Value>(&row.facts),
            Ok(Value::Object(_))
        ) {
            report.facts_not_object += 1;
            continue;
        }
        let Ok(path) = local_path(media_dir, &row.source_path, source_roots) else {
            report.unmapped_path += 1;
            continue;
        };
        let mut wanted = store_sizes(&sizes, &row.source_path).map_err(store_failure)?;
        if wanted.is_empty() {
            let Ok(meta) = std::fs::metadata(&path) else {
                if let Some(reason) = media_root_error(media_dir) {
                    return Err(Failure::Failed(reason));
                }
                report.unreadable_file += 1;
                continue;
            };
            report.no_store_row += 1;
            wanted.push(meta.len() as i64);
        }
        let mut unreadable = false;
        for size in wanted {
            let Ok(hash) = file_hash(&path, size) else {
                unreadable = true;
                break;
            };
            if !seen.insert(hash.clone()) {
                report.duplicate_hash += 1;
                continue;
            }
            candidates.push((
                hash,
                Row {
                    source_path: row.source_path.clone(),
                    analyzed_at: row.analyzed_at.clone(),
                    facts: row.facts.clone(),
                },
            ));
        }
        if unreadable {
            if let Some(reason) = media_root_error(media_dir) {
                return Err(Failure::Failed(reason));
            }
            report.unreadable_file += 1;
        }
    }
    drop(sizes);

    // Hashes the store already has facts for.
    let present: HashSet<String> = {
        let conn = open_readonly(store).map_err(failure)?;
        let mut stmt = conn
            .prepare("SELECT 1 FROM audio_facts WHERE file_hash = ?1")
            .map_err(store_failure)?;
        let mut present = HashSet::new();
        for (hash, _) in &candidates {
            if stmt.exists([hash]).map_err(store_failure)? {
                present.insert(hash.clone());
            }
        }
        present
    };
    let (have, fresh): (Vec<_>, Vec<_>) = candidates
        .into_iter()
        .partition(|(hash, _)| present.contains(hash));
    report.already_present = have.len();

    if dry_run {
        report.imported = fresh.len();
        return Ok(report);
    }
    for batch in fresh.chunks(BATCH) {
        let mut opened = open_store(store).map_err(failure)?;
        let tx = opened.conn.transaction().map_err(store_failure)?;
        for (hash, row) in batch {
            let changed = tx
                .execute(
                    "INSERT INTO audio_facts (file_hash, facts, analyzed_at) \
                     VALUES (?1, ?2, ?3) ON CONFLICT(file_hash) DO NOTHING",
                    params![hash, row.facts, row.analyzed_at],
                )
                .map_err(store_failure)?;
            if changed == 1 {
                report.imported += 1;
            } else {
                report.already_present += 1;
            }
        }
        tx.commit().map_err(store_failure)?;
    }
    Ok(report)
}

/// `plexdb import-audio-facts <catalog.db> [--dry-run]`.
pub fn run(args: &[String]) -> Result<(), Failure> {
    let mut catalog: Option<&String> = None;
    let mut dry_run = false;
    for arg in args {
        if arg == "--dry-run" {
            dry_run = true;
        } else if arg.starts_with("--") {
            return Err(Failure::Usage(format!("{NAME}: unknown option {arg}")));
        } else if catalog.replace(arg).is_some() {
            return Err(Failure::Usage(format!(
                "{NAME} takes one catalog path, got another: {arg}"
            )));
        }
    }
    let Some(catalog) = catalog else {
        return Err(Failure::Usage(format!(
            "usage: {NAME} <catalog.db> [--dry-run]"
        )));
    };
    let (media_dir, roots) = media_settings().map_err(failure)?;
    let config = Config::from_env().map_err(failure)?;
    let report = import(
        &config.store_path,
        Path::new(catalog),
        &media_dir,
        &roots,
        dry_run,
        &mut |line| println!("{line}"),
    )?;
    println!("{}", report.describe());
    Ok(())
}
