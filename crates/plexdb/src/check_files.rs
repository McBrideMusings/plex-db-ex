//! `check-files`: judge each file against the rules a healthy one keeps, and decode a
//! batch of them in full each run.
//!
//! `media_check` holds one verdict per `(part_id, kind)` for the files Plex lists, and
//! `extra_check` one per `(path, kind)` for the files an extras library holds (ADR-0030).
//! The `header` kind: ffprobe reads the file's container and streams, and the file passes
//! when it
//!
//! - parses (ffprobe exits 0 with a report),
//! - is not empty on disk,
//! - has a video stream and an audio stream, and
//! - has a duration within 2% of the one recorded for it.
//!
//! **Probe once per change.** A file is probed only when it has no `header` row.
//! `media-walk` replaces a changed record's parts whole, and `extras-walk` deletes an
//! extra's checks when it re-reads a file whose size or mtime changed; the checks go with
//! the row, so a file that is new or changed is the one with no row. A file a run did not
//! judge (not on the mount, ffprobe missing or timed out) gets no row either, so the next
//! run tries it again rather than recording a down mount as a damaged file.
//!
//! **Decode a batch a night.** The `decode` kind runs ffmpeg over every frame of a file
//! and fails it on any error ffmpeg prints. A run decodes at most `decode_batch` parts,
//! and at most `decode_batch` extras: the ones never decoded first, then the one decoded
//! longest ago. A decode that ran past its time limit is recorded as [`DECODE_TIMED_OUT`]
//! so the file goes to the back of the next batch; it is a slow mount, not a damaged file.
//! A file the run could not decode gets no row and does not use up the batch.

use std::path::{Path, PathBuf};

use plexdb_store::{Config, StoreError, open_store};
use rusqlite::Connection;
use serde_json::Value;

use crate::args::{self, Spec};
use crate::frame_media::{FfmpegMedia, MediaError, local_path, py_float, rounded_ms};
use crate::frames::FrameConfig;
use crate::staleness::Now;
use crate::steps::{Failure, failure};

pub const NAME: &str = "check-files";

/// The `error` a decode that ran past its time limit leaves on its `ok = 0` row.
pub const DECODE_TIMED_OUT: &str = "ffmpeg timed out";

/// A file's duration may differ from the recorded one by this fraction before it fails.
const DURATION_TOLERANCE: f64 = 0.02;

const DEFAULT_DECODE_BATCH: i64 = 50;

/// What one run did to one table's files.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct CheckStats {
    pub checked: usize,
    pub passed: usize,
    pub failed: usize,
    /// Left unchecked for the next run: not on the mount, or ffprobe could not judge.
    pub skipped: usize,
    pub decoded: usize,
    pub decode_failed: usize,
    /// Decodes that ran past the time limit: recorded, but not counted as damaged.
    pub decode_timed_out: usize,
    /// Could not be decoded (not on the mount, ffmpeg missing).
    pub decode_skipped: usize,
}

/// Both tables' stats.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Stats {
    pub parts: CheckStats,
    pub extras: CheckStats,
}

/// What ffprobe made of a file.
pub enum Probe {
    Report(Value),
    /// ffprobe ran and rejected the file; the text says why.
    Rejected(String),
}

/// The two things this step asks of a video file.
pub trait FileChecker {
    fn probe(&self, path: &Path) -> Result<Probe, MediaError>;
    /// ffmpeg's error output for a full decode, `""` when it printed none.
    fn decode(&self, path: &Path) -> Result<String, MediaError>;
}

impl FileChecker for FfmpegMedia {
    fn probe(&self, path: &Path) -> Result<Probe, MediaError> {
        Ok(match self.probe_report(path)? {
            Ok(report) => Probe::Report(report),
            Err(why) => Probe::Rejected(why),
        })
    }

    fn decode(&self, path: &Path) -> Result<String, MediaError> {
        self.decode_errors(path)
    }
}

fn duration_ms(report: &Value) -> Option<i64> {
    let seconds = match report.get("format")?.get("duration")? {
        Value::String(text) => py_float(text)?,
        Value::Number(n) => n.as_f64()?,
        _ => return None,
    };
    seconds.is_finite().then(|| rounded_ms(seconds))
}

/// Every rule `report` (ffprobe's `-show_format -show_streams` JSON) breaks.
pub fn rule_failures(report: &Value, recorded_duration_ms: Option<i64>) -> Vec<String> {
    let mut failures = Vec::new();
    let has = |kind: &str| {
        report
            .get("streams")
            .and_then(Value::as_array)
            .is_some_and(|streams| {
                streams
                    .iter()
                    .any(|s| s.get("codec_type").and_then(Value::as_str) == Some(kind))
            })
    };
    if !has("video") {
        failures.push("no video stream".to_string());
    }
    if !has("audio") {
        failures.push("no audio stream".to_string());
    }
    if let Some(recorded) = recorded_duration_ms.filter(|ms| *ms != 0) {
        match duration_ms(report) {
            None => failures.push(format!(
                "no duration; Plex says {:.1} s",
                recorded as f64 / 1000.0
            )),
            Some(found) => {
                let off = (found - recorded).abs();
                if off as f64 > recorded as f64 * DURATION_TOLERANCE {
                    failures.push(format!(
                        "duration {:.1} s is {:.1}% from Plex's {:.1} s",
                        found as f64 / 1000.0,
                        (off * 100) as f64 / recorded as f64,
                        recorded as f64 / 1000.0
                    ));
                }
            }
        }
    }
    failures
}

/// `None` when the file at `path` passes, else why it fails. A [`MediaError`] or an I/O
/// error means the file was not judged.
fn judge(
    media: &dyn FileChecker,
    path: &Path,
    recorded_duration_ms: Option<i64>,
) -> Result<Option<String>, ()> {
    let size = std::fs::metadata(path).map_err(|_| ())?.len();
    if size == 0 {
        return Ok(Some("file is empty".into()));
    }
    match media.probe(path).map_err(|_| ())? {
        Probe::Rejected(why) => Ok(Some(format!("does not parse: {why}"))),
        Probe::Report(report) => {
            let failures = rule_failures(&report, recorded_duration_ms);
            Ok((!failures.is_empty()).then(|| failures.join("; ")))
        }
    }
}

/// One table of files: what it checks, and how.
struct Table {
    name: &'static str,
    key: &'static str,
    /// Rows with no `header` verdict: `(key, file, recorded duration)`, in key order.
    pending: &'static str,
    /// Every file with its `decode` row's time, never-decoded first: `(key, file)`.
    decode_order: &'static str,
}

const PARTS: Table = Table {
    name: "media_check",
    key: "part_id",
    // Plex gives a record with several parts one duration for the whole title, so a
    // part's own length is compared only when it is the record's single part.
    pending: "SELECT p.part_id, p.file, CASE WHEN (SELECT COUNT(*) FROM media_part o \
              WHERE o.rating_key = p.rating_key) = 1 THEN p.duration_ms END \
              FROM media_part p WHERE NOT EXISTS \
              (SELECT 1 FROM media_check c WHERE c.part_id = p.part_id AND c.kind = 'header') \
              ORDER BY p.part_id",
    decode_order: "SELECT p.part_id, p.file FROM media_part p LEFT JOIN media_check c \
                   ON c.part_id = p.part_id AND c.kind = 'decode' \
                   ORDER BY c.checked_at IS NOT NULL, c.checked_at, p.part_id",
};

const EXTRAS: Table = Table {
    name: "extra_check",
    key: "path",
    pending: "SELECT f.path, f.path, f.duration_ms FROM extra_file f \
              WHERE f.missing_since IS NULL AND NOT EXISTS \
              (SELECT 1 FROM extra_check c WHERE c.path = f.path AND c.kind = 'header') \
              ORDER BY f.path",
    decode_order: "SELECT f.path, f.path FROM extra_file f LEFT JOIN extra_check c \
                   ON c.path = f.path AND c.kind = 'decode' WHERE f.missing_since IS NULL \
                   ORDER BY c.checked_at IS NOT NULL, c.checked_at, f.path",
};

/// A row key, which is a part id or a path.
#[derive(Clone)]
enum Key {
    Part(i64),
    Path(String),
}

impl rusqlite::ToSql for Key {
    fn to_sql(&self) -> rusqlite::Result<rusqlite::types::ToSqlOutput<'_>> {
        match self {
            Key::Part(id) => id.to_sql(),
            Key::Path(path) => path.to_sql(),
        }
    }
}

fn read_key(table: &Table, row: &rusqlite::Row<'_>) -> rusqlite::Result<Key> {
    Ok(if table.key == "part_id" {
        Key::Part(row.get(0)?)
    } else {
        Key::Path(row.get(0)?)
    })
}

fn write_verdict(
    conn: &Connection,
    table: &Table,
    key: &Key,
    kind: &str,
    now: &str,
    error: Option<&str>,
) -> Result<(), StoreError> {
    conn.execute(
        &format!(
            "INSERT OR REPLACE INTO {} ({}, kind, checked_at, ok, error) VALUES (?, ?, ?, ?, ?)",
            table.name, table.key
        ),
        rusqlite::params![key, kind, now, i64::from(error.is_none()), error],
    )?;
    Ok(())
}

/// Judge `table`'s files with no `header` verdict, at most `limit` of them, then decode
/// `decode_batch` of its files in full. `locate` maps a stored file to where this process
/// reads it.
fn check_table(
    conn: &Connection,
    table: &Table,
    media: &dyn FileChecker,
    locate: &dyn Fn(&str) -> Result<PathBuf, MediaError>,
    limit: Option<usize>,
    decode_batch: usize,
    now: &str,
) -> Result<CheckStats, StoreError> {
    let mut stats = CheckStats::default();
    let pending: Vec<(Key, String, Option<i64>)> = conn
        .prepare(table.pending)?
        .query_map([], |row| {
            Ok((read_key(table, row)?, row.get(1)?, row.get(2)?))
        })?
        .collect::<Result<_, _>>()?;
    for (key, file, recorded) in pending {
        if limit.is_some_and(|limit| stats.checked >= limit) {
            break;
        }
        let Ok(path) = locate(&file) else {
            stats.skipped += 1;
            continue;
        };
        let Ok(error) = judge(media, &path, recorded) else {
            stats.skipped += 1;
            continue;
        };
        write_verdict(conn, table, &key, "header", now, error.as_deref())?;
        stats.checked += 1;
        if error.is_some() {
            stats.failed += 1;
        } else {
            stats.passed += 1;
        }
    }
    decode_batch_of(conn, table, media, locate, decode_batch, &mut stats, now)?;
    Ok(stats)
}

/// Decode the `batch` files whose last decode is oldest, never-decoded ones first.
fn decode_batch_of(
    conn: &Connection,
    table: &Table,
    media: &dyn FileChecker,
    locate: &dyn Fn(&str) -> Result<PathBuf, MediaError>,
    batch: usize,
    stats: &mut CheckStats,
    now: &str,
) -> Result<(), StoreError> {
    if batch == 0 {
        return Ok(());
    }
    let files: Vec<(Key, String)> = conn
        .prepare(table.decode_order)?
        .query_map([], |row| Ok((read_key(table, row)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    for (key, file) in files {
        if stats.decoded >= batch {
            break;
        }
        // A file not on the mount is left for the next run before ffmpeg runs.
        let decoded = match locate(&file) {
            Ok(path) if std::fs::metadata(&path).is_ok() => media.decode(&path),
            _ => {
                stats.decode_skipped += 1;
                continue;
            }
        };
        let error = match decoded {
            Ok(text) => text,
            // Record the timeout so the file moves to the back of the order instead of
            // heading every night's batch.
            Err(err) if err.timed_out => DECODE_TIMED_OUT.to_string(),
            Err(_) => {
                stats.decode_skipped += 1;
                continue;
            }
        };
        let error = (!error.is_empty()).then_some(error);
        write_verdict(conn, table, &key, "decode", now, error.as_deref())?;
        stats.decoded += 1;
        match error.as_deref() {
            Some(DECODE_TIMED_OUT) => stats.decode_timed_out += 1,
            Some(_) => stats.decode_failed += 1,
            None => {}
        }
    }
    Ok(())
}

/// Check every part Plex lists and, after it, every file an extras library holds. `limit`
/// caps the header checks of the whole run.
pub fn check_files(
    conn: &Connection,
    media: &dyn FileChecker,
    locate_part: &dyn Fn(&str) -> Result<PathBuf, MediaError>,
    limit: Option<usize>,
    decode_batch: usize,
    now: &str,
) -> Result<Stats, StoreError> {
    let parts = check_table(conn, &PARTS, media, locate_part, limit, decode_batch, now)?;
    // An extra's path is already where this process reads it.
    let locate_extra = |path: &str| Ok(PathBuf::from(path));
    let left = limit.map(|limit| limit.saturating_sub(parts.checked));
    let extras = check_table(conn, &EXTRAS, media, &locate_extra, left, decode_batch, now)?;
    Ok(Stats { parts, extras })
}

pub fn describe(stats: &Stats) -> Vec<String> {
    let mut lines = vec![
        format!(
            "files: {} checked, {} passed, {} failed, {} left for the next run",
            stats.parts.checked, stats.parts.passed, stats.parts.failed, stats.parts.skipped
        ),
        format!(
            "decode: {} decoded in full, {} failed, {} timed out, {} left for the next run",
            stats.parts.decoded,
            stats.parts.decode_failed,
            stats.parts.decode_timed_out,
            stats.parts.decode_skipped
        ),
    ];
    if stats.extras != CheckStats::default() {
        lines.push(format!(
            "extras: {} checked, {} passed, {} failed, {} left for the next run",
            stats.extras.checked, stats.extras.passed, stats.extras.failed, stats.extras.skipped
        ));
        lines.push(format!(
            "extras decode: {} decoded in full, {} failed, {} timed out, {} left for the next run",
            stats.extras.decoded,
            stats.extras.decode_failed,
            stats.extras.decode_timed_out,
            stats.extras.decode_skipped
        ));
    }
    lines
}

/// `PLEXDB_DECODE_BATCH`: a whole number, 0 or more; unset is 50.
fn decode_batch_setting() -> Result<usize, StoreError> {
    let raw = std::env::var("PLEXDB_DECODE_BATCH").unwrap_or_default();
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(DEFAULT_DECODE_BATCH as usize);
    }
    match crate::pyval::py_int_text(raw) {
        Some(batch) if batch >= 0 => Ok(batch as usize),
        _ => Err(StoreError::Config(format!(
            "PLEXDB_DECODE_BATCH is {}; use a whole number, 0 or more",
            crate::decisions::py_repr(raw)
        ))),
    }
}

const SPEC: Spec = Spec {
    options: &["--limit"],
    flags: &[],
    positionals: 0,
};

pub fn run(args: &[String]) -> Result<(), Failure> {
    let parsed = args::parse(args, &SPEC).map_err(Failure::Usage)?;
    let limit = args::int_option(&parsed, "--limit").map_err(Failure::Usage)?;
    (|| {
        let config = FrameConfig::from_env()?;
        let decode_batch = decode_batch_setting()?;
        let media_dir = config.media_dir.clone().ok_or_else(|| {
            StoreError::Config(
                "PLEXDB_MEDIA_DIR is not set, so no video file can be read; mount the library \
                 read-only and name the directory Plex calls PLEX_SOURCE_ROOTS"
                    .into(),
            )
        })?;
        if config.source_roots.is_empty() {
            return Err(StoreError::Config(
                "PLEX_SOURCE_ROOTS is not set, so Plex's file paths cannot be mapped".into(),
            ));
        }
        if !crate::frames_cmd::which(&config.ffprobe) {
            return Err(StoreError::Store(format!(
                "{} must be installed (or named by PLEXDB_FFPROBE) to check files",
                config.ffprobe
            )));
        }
        let media = FfmpegMedia::new(&config.ffmpeg, &config.ffprobe, "cpu", None);
        let roots = config.source_roots.clone();
        let locate = move |plex_file: &str| local_path(&media_dir, plex_file, &roots);
        let store = open_store(&Config::from_env()?.store_path)?;
        let limit = limit.map(|n| usize::try_from(n).unwrap_or(0));
        let stats = check_files(
            &store.conn,
            &media,
            &locate,
            limit,
            decode_batch,
            &Now::read().iso(),
        )?;
        for line in describe(&stats) {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::RefCell;

    fn report(streams: &[&str], duration: Option<&str>) -> Value {
        let streams: Vec<Value> = streams
            .iter()
            .map(|kind| json!({ "codec_type": kind }))
            .collect();
        match duration {
            Some(d) => json!({ "streams": streams, "format": { "duration": d } }),
            None => json!({ "streams": streams, "format": {} }),
        }
    }

    #[test]
    fn a_file_with_both_streams_and_the_recorded_length_breaks_no_rule() {
        let report = report(&["video", "audio"], Some("2.000000"));
        assert!(rule_failures(&report, Some(2000)).is_empty());
        // 1.9% off is inside the tolerance; 5% is not.
        assert!(rule_failures(&report, Some(2038)).is_empty());
        let off = rule_failures(&report, Some(2100));
        assert_eq!(off, vec!["duration 2.0 s is 4.8% from Plex's 2.1 s"]);
    }

    #[test]
    fn each_missing_stream_and_a_missing_duration_is_named() {
        let silent = report(&["video"], None);
        assert_eq!(
            rule_failures(&silent, Some(2000)),
            vec!["no audio stream", "no duration; Plex says 2.0 s"]
        );
        assert_eq!(
            rule_failures(&report(&["audio"], Some("1")), None),
            vec!["no video stream"]
        );
        // A recorded duration of 0 means Plex did not record one.
        assert!(rule_failures(&report(&["video", "audio"], None), Some(0)).is_empty());
    }

    #[test]
    fn py_float_text_in_duration_reads_as_seconds() {
        assert_eq!(duration_ms(&report(&[], Some("0.0005"))), Some(0));
        assert_eq!(duration_ms(&report(&[], Some("nan"))), None);
        assert_eq!(duration_ms(&report(&[], Some("abc"))), None);
    }

    struct Fake {
        probed: RefCell<Vec<PathBuf>>,
        decoded: RefCell<Vec<PathBuf>>,
        rejects: Vec<&'static str>,
        times_out: Option<&'static str>,
    }

    impl Fake {
        fn new() -> Self {
            Fake {
                probed: RefCell::new(Vec::new()),
                decoded: RefCell::new(Vec::new()),
                rejects: Vec::new(),
                times_out: None,
            }
        }
    }

    impl FileChecker for Fake {
        fn probe(&self, path: &Path) -> Result<Probe, MediaError> {
            self.probed.borrow_mut().push(path.to_path_buf());
            if self.rejects.iter().any(|name| path.ends_with(name)) {
                return Ok(Probe::Rejected("moov atom not found".into()));
            }
            Ok(Probe::Report(report(&["video", "audio"], Some("2.0"))))
        }

        fn decode(&self, path: &Path) -> Result<String, MediaError> {
            self.decoded.borrow_mut().push(path.to_path_buf());
            if self.times_out.is_some_and(|name| path.ends_with(name)) {
                return Err(MediaError {
                    message: "ran past".into(),
                    reason: "ffmpeg timed out".into(),
                    timed_out: true,
                });
            }
            if self.rejects.iter().any(|name| path.ends_with(name)) {
                return Ok("[mov] moov atom not found".into());
            }
            Ok(String::new())
        }
    }

    fn store(dir: &Path) -> Connection {
        let conn = Connection::open(dir.join("plexdb.db")).expect("store");
        conn.execute_batch(
            "PRAGMA foreign_keys = ON;
             CREATE TABLE items (item_id TEXT PRIMARY KEY, type TEXT, title TEXT);
             CREATE TABLE media_part (part_id INTEGER PRIMARY KEY, item_id TEXT, rating_key TEXT,
                                      file TEXT, duration_ms INTEGER);
             CREATE TABLE media_check (
                 part_id INTEGER NOT NULL REFERENCES media_part(part_id) ON DELETE CASCADE,
                 kind TEXT NOT NULL, checked_at TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
                 PRIMARY KEY (part_id, kind));
             CREATE TABLE extra_file (path TEXT PRIMARY KEY, item_id TEXT, duration_ms INTEGER,
                                      missing_since TEXT);
             CREATE TABLE extra_check (
                 path TEXT NOT NULL REFERENCES extra_file(path)
                     ON UPDATE CASCADE ON DELETE CASCADE,
                 kind TEXT NOT NULL, checked_at TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
                 PRIMARY KEY (path, kind));",
        )
        .expect("schema");
        conn
    }

    fn touch(dir: &Path, name: &str) -> String {
        let path = dir.join(name);
        std::fs::write(&path, b"x").expect("file");
        path.to_string_lossy().into_owned()
    }

    fn part(conn: &Connection, id: i64, file: &str) {
        conn.execute(
            "INSERT INTO media_part (part_id, item_id, rating_key, file, duration_ms)
             VALUES (?, 't:1', ?, ?, 2000)",
            rusqlite::params![id, id.to_string(), file],
        )
        .expect("part");
    }

    fn extra(conn: &Connection, file: &str) {
        conn.execute(
            "INSERT INTO extra_file (path, item_id) VALUES (?, 'fs:1')",
            [file],
        )
        .expect("extra");
    }

    fn here(plex_file: &str) -> Result<PathBuf, MediaError> {
        Ok(PathBuf::from(plex_file))
    }

    const NOW: &str = "2026-10-27T00:00:00+00:00";

    fn verdict(conn: &Connection, sql: &str) -> (i64, Option<String>) {
        conn.query_row(sql, [], |row| Ok((row.get(0)?, row.get(1)?)))
            .expect("verdict")
    }

    #[test]
    fn a_rejected_extra_gets_an_ok_0_row_and_a_good_one_ok_1() {
        let dir = tempfile::tempdir().expect("dir");
        let conn = store(dir.path());
        let good = touch(dir.path(), "good.mp4");
        let bad = touch(dir.path(), "truncated.mp4");
        extra(&conn, &good);
        extra(&conn, &bad);
        let mut media = Fake::new();
        media.rejects = vec!["truncated.mp4"];

        let stats = check_files(&conn, &media, &here, None, 0, NOW).expect("run");

        assert_eq!(
            (
                stats.extras.checked,
                stats.extras.passed,
                stats.extras.failed
            ),
            (2, 1, 1)
        );
        assert_eq!(stats.parts, CheckStats::default());
        let sql = |path: &str| {
            format!("SELECT ok, error FROM extra_check WHERE path = '{path}' AND kind = 'header'")
        };
        assert_eq!(verdict(&conn, &sql(&good)), (1, None));
        assert_eq!(
            verdict(&conn, &sql(&bad)),
            (0, Some("does not parse: moov atom not found".into()))
        );
    }

    #[test]
    fn an_empty_file_fails_without_asking_ffprobe_and_a_missing_one_is_left_alone() {
        let dir = tempfile::tempdir().expect("dir");
        let conn = store(dir.path());
        let empty = dir.path().join("empty.mp4");
        std::fs::write(&empty, b"").expect("file");
        part(&conn, 1, &empty.to_string_lossy());
        part(&conn, 2, &dir.path().join("gone.mp4").to_string_lossy());
        let media = Fake::new();

        let stats = check_files(&conn, &media, &here, None, 0, NOW).expect("run");

        assert_eq!(
            (stats.parts.checked, stats.parts.failed, stats.parts.skipped),
            (1, 1, 1)
        );
        assert!(media.probed.borrow().is_empty());
        assert_eq!(
            verdict(
                &conn,
                "SELECT ok, error FROM media_check WHERE part_id = 1 AND kind = 'header'"
            ),
            (0, Some("file is empty".into()))
        );
        let rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM media_check", [], |row| row.get(0))
            .expect("count");
        assert_eq!(rows, 1);
    }

    #[test]
    fn a_file_with_a_verdict_is_not_probed_again() {
        let dir = tempfile::tempdir().expect("dir");
        let conn = store(dir.path());
        part(&conn, 1, &touch(dir.path(), "a.mp4"));
        extra(&conn, &touch(dir.path(), "b.mp4"));
        check_files(&conn, &Fake::new(), &here, None, 0, NOW).expect("first");

        let second = Fake::new();
        let stats = check_files(&conn, &second, &here, None, 0, NOW).expect("second");

        assert!(second.probed.borrow().is_empty());
        assert_eq!((stats.parts.checked, stats.extras.checked), (0, 0));
    }

    #[test]
    fn the_limit_covers_parts_and_extras_together() {
        let dir = tempfile::tempdir().expect("dir");
        let conn = store(dir.path());
        part(&conn, 1, &touch(dir.path(), "a.mp4"));
        part(&conn, 2, &touch(dir.path(), "b.mp4"));
        extra(&conn, &touch(dir.path(), "c.mp4"));

        let stats = check_files(&conn, &Fake::new(), &here, Some(2), 0, NOW).expect("run");

        assert_eq!((stats.parts.checked, stats.extras.checked), (2, 0));
    }

    #[test]
    fn decode_takes_never_decoded_files_first_and_skips_missing_ones() {
        let dir = tempfile::tempdir().expect("dir");
        let conn = store(dir.path());
        for id in 1..=3 {
            part(&conn, id, &touch(dir.path(), &format!("p{id}.mp4")));
        }
        part(&conn, 4, &dir.path().join("gone.mp4").to_string_lossy());
        conn.execute_batch(
            "INSERT INTO media_check VALUES (1, 'decode', '2026-10-03T00:00:00+00:00', 1, NULL);
             INSERT INTO media_check VALUES (2, 'decode', '2026-10-01T00:00:00+00:00', 1, NULL);",
        )
        .expect("old");
        let media = Fake::new();

        let stats = check_files(&conn, &media, &here, None, 2, NOW).expect("run");

        // 3 was never decoded, 4 is not on the mount and uses no slot, then 2 is oldest.
        let names: Vec<String> = media
            .decoded
            .borrow()
            .iter()
            .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, ["p3.mp4", "p2.mp4"]);
        assert_eq!((stats.parts.decoded, stats.parts.decode_skipped), (2, 1));
    }

    #[test]
    fn a_decode_error_is_stored_and_a_timeout_is_recorded_without_counting_as_damage() {
        let dir = tempfile::tempdir().expect("dir");
        let conn = store(dir.path());
        part(&conn, 1, &touch(dir.path(), "slow.mp4"));
        part(&conn, 2, &touch(dir.path(), "bad.mp4"));
        extra(&conn, &touch(dir.path(), "bad-extra.mp4"));
        let mut media = Fake::new();
        media.times_out = Some("slow.mp4");
        media.rejects = vec!["bad.mp4", "bad-extra.mp4"];

        let stats = check_files(&conn, &media, &here, None, 5, NOW).expect("run");

        assert_eq!(
            (
                stats.parts.decoded,
                stats.parts.decode_failed,
                stats.parts.decode_timed_out
            ),
            (2, 1, 1)
        );
        assert_eq!((stats.extras.decoded, stats.extras.decode_failed), (1, 1));
        assert_eq!(
            verdict(
                &conn,
                "SELECT ok, error FROM media_check WHERE part_id = 1 AND kind = 'decode'"
            ),
            (0, Some(DECODE_TIMED_OUT.into()))
        );
        assert_eq!(
            verdict(
                &conn,
                "SELECT ok, error FROM media_check WHERE part_id = 2 AND kind = 'decode'"
            ),
            (0, Some("[mov] moov atom not found".into()))
        );
    }

    #[test]
    fn describe_prints_extras_lines_only_when_extras_were_looked_at() {
        let mut stats = Stats::default();
        assert_eq!(describe(&stats).len(), 2);
        stats.extras.checked = 1;
        assert_eq!(describe(&stats).len(), 4);
    }
}
