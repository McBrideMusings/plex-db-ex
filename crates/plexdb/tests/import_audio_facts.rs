//! `import-audio-facts` against a store built by the real migrations, a fixture catalog and
//! small files in a temp dir: every count, the second run, the dry run, facts already in the
//! store, and `harvest-audio` afterwards recording the part done with no analysis.

mod common;

use std::path::{Path, PathBuf};
use std::time::Duration;

use plexdb::harvest_audio::{AnalyzeError, Analyzer, Settings, harvest};
use plexdb::import_audio_facts::{Report, import};
use plexdb::plex::Section;
use plexdb::steps::Failure;
use rusqlite::Connection;
use serde_json::Value;

struct Panics;

impl Analyzer for Panics {
    fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError> {
        panic!("analyzed {}", path.display());
    }
    fn duration_ms(&self, path: &Path) -> Result<Option<i64>, AnalyzeError> {
        panic!("probed {}", path.display());
    }
}

struct World {
    _dir: tempfile::TempDir,
    store: PathBuf,
    media: PathBuf,
    catalog: PathBuf,
    conn: Connection,
}

const FACTS: &str = r#"{"bpm": 120.0,  "key": "C"}"#;

impl World {
    fn new() -> World {
        let dir = tempfile::tempdir().expect("tempdir");
        let conn = common::store(dir.path());
        let media = dir.path().join("media");
        std::fs::create_dir(&media).expect("media dir");
        let catalog = dir.path().join("catalog.db");
        let cat = Connection::open(&catalog).expect("catalog");
        cat.execute_batch(
            "CREATE TABLE entry_audio (entry_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, \
             analyzed_at TEXT NOT NULL, error TEXT, facts TEXT)",
        )
        .expect("entry_audio");
        World {
            store: dir.path().join("plexdb.db"),
            _dir: dir,
            media,
            catalog,
            conn,
        }
    }

    fn file(&self, name: &str, content: &[u8]) {
        let path = self.media.join(name);
        std::fs::write(&path, content).expect("write media");
        std::fs::File::options()
            .write(true)
            .open(&path)
            .expect("open")
            .set_modified(std::time::UNIX_EPOCH + Duration::from_secs(1_700_000_000))
            .expect("same mtime for every file");
    }

    fn entry(&self, id: &str, source_path: &str, analyzed_at: &str, facts: Option<&str>) {
        Connection::open(&self.catalog)
            .expect("catalog")
            .execute(
                "INSERT INTO entry_audio (entry_id, source_path, analyzed_at, facts) \
                 VALUES (?1, ?2, ?3, ?4)",
                rusqlite::params![id, source_path, analyzed_at, facts],
            )
            .expect("entry");
    }

    fn run(&self, dry_run: bool) -> Report {
        import(
            &self.store,
            &self.catalog,
            &self.media,
            &["/media".to_string()],
            dry_run,
            &mut |_| {},
        )
        .expect("import runs")
    }

    fn facts_rows(&self) -> i64 {
        self.conn
            .query_row("SELECT count(*) FROM audio_facts", [], |r| r.get(0))
            .expect("count")
    }
}

#[test]
fn every_count_and_the_second_run() {
    let w = World::new();
    w.file("a.mkv", b"aaaa");
    w.file("b.mkv", b"bbbbb");
    w.file("copy-of-a.mkv", b"aaaa");
    w.file("old.mkv", b"old-content");
    w.entry("1", "/media/a.mkv", "2026-01-01T00:00:00Z", Some(FACTS));
    w.entry("2", "/media/b.mkv", "2026-01-02T00:00:00Z", Some(FACTS));
    w.entry(
        "3",
        "/media/copy-of-a.mkv",
        "2026-01-03T00:00:00Z",
        Some(FACTS),
    );
    w.entry("4", "/elsewhere/c.mkv", "2026-01-04T00:00:00Z", Some(FACTS));
    w.entry(
        "5",
        "/media/missing.mkv",
        "2026-01-05T00:00:00Z",
        Some(FACTS),
    );
    w.entry("6", "/media/a.mkv", "2026-01-06T00:00:00Z", Some("[1, 2]"));
    w.entry(
        "7",
        "/media/a.mkv",
        "2026-01-07T00:00:00Z",
        Some("not json"),
    );
    w.entry("8", "/media/a.mkv", "2026-01-08T00:00:00Z", None);
    w.entry("9", "/media/old.mkv", "2026-01-09T00:00:00Z", Some(FACTS));

    // old.mkv already has facts under its hash.
    let old_hash = plexdb::frame_media::file_hash(&w.media.join("old.mkv"), 11).expect("hash");
    w.conn
        .execute(
            "INSERT INTO audio_facts (file_hash, facts, analyzed_at) VALUES (?1, '{\"bpm\":1}', 'then')",
            [&old_hash],
        )
        .expect("existing facts");

    let report = w.run(false);
    assert_eq!(
        report,
        Report {
            rows_read: 8, // the NULL-facts row is not read
            imported: 2,
            already_present: 1,
            duplicate_hash: 1,
            unmapped_path: 1,
            unreadable_file: 1,
            facts_not_object: 2,
            no_store_row: 4,
            dry_run: false,
        }
    );
    assert_eq!(w.facts_rows(), 3);

    // Stored as the station wrote it, with the row's analyzed_at; the first row of a hash wins.
    let (facts, at): (String, String) = w
        .conn
        .query_row(
            "SELECT facts, analyzed_at FROM audio_facts WHERE file_hash = ?1",
            [plexdb::frame_media::file_hash(&w.media.join("a.mkv"), 4).unwrap()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("a's facts");
    assert_eq!(facts, FACTS);
    assert_eq!(at, "2026-01-01T00:00:00Z");

    // Existing facts are untouched.
    let (facts, at): (String, String) = w
        .conn
        .query_row(
            "SELECT facts, analyzed_at FROM audio_facts WHERE file_hash = ?1",
            [&old_hash],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("old facts");
    assert_eq!((facts.as_str(), at.as_str()), ("{\"bpm\":1}", "then"));

    let again = w.run(false);
    assert_eq!(again.imported, 0);
    assert_eq!(again.already_present, 3);
    assert_eq!(w.facts_rows(), 3);
}

#[test]
fn dry_run_counts_and_writes_nothing() {
    let w = World::new();
    w.file("a.mkv", b"aaaa");
    w.entry("1", "/media/a.mkv", "2026-01-01T00:00:00Z", Some(FACTS));
    let report = w.run(true);
    assert_eq!(
        (report.rows_read, report.imported, report.dry_run),
        (1, 1, true)
    );
    assert!(report.describe().contains("1 would import"));
    assert_eq!(w.facts_rows(), 0);
}

#[test]
fn a_catalog_without_entry_audio_is_an_error() {
    let w = World::new();
    std::fs::remove_file(&w.catalog).expect("remove");
    Connection::open(&w.catalog)
        .expect("catalog")
        .execute_batch("CREATE TABLE other (x)")
        .expect("other");
    let err = import(
        &w.store,
        &w.catalog,
        &w.media,
        &["/media".to_string()],
        false,
        &mut |_| {},
    )
    .expect_err("no entry_audio");
    match err {
        Failure::Failed(message) => assert!(message.contains("no entry_audio table"), "{message}"),
        other => panic!("unexpected {other:?}"),
    }
}

/// Import a one-file catalog against a store whose `media_part` for it has `size`, then run
/// harvest-audio with an analyzer that panics if called.
fn harvest_after_import(size: Option<i64>) {
    let w = World::new();
    w.file("a.mkv", b"aaaa");
    w.entry("1", "/media/a.mkv", "2026-01-01T00:00:00Z", Some(FACTS));
    w.conn
        .execute(
            "INSERT INTO items (item_id, type, title) VALUES ('plex:1', 'movie', 'plex:1')",
            [],
        )
        .expect("item");
    w.conn
        .execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
             VALUES ('rk1', 'plex:1', '1', 'x')",
            [],
        )
        .expect("plex item");
    w.conn
        .execute(
            "INSERT INTO media_part (part_id, item_id, rating_key, file, size, duration_ms, \
             fetched_at) VALUES (1, 'plex:1', 'rk1', '/media/a.mkv', ?1, 300000, 'x')",
            [size],
        )
        .expect("part");
    let report = w.run(false);
    assert_eq!((report.imported, report.no_store_row), (1, 0));
    let settings = Settings {
        libraries: vec!["Movies".into()],
        analyzer: "unused".into(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(900),
        media_dir: w.media.clone(),
        source_roots: vec!["/media".into()],
    };
    let sections = vec![Section {
        key: "1".into(),
        kind: "movie".into(),
        title: "Movies".into(),
    }];
    harvest(
        &w.store,
        &settings,
        &sections,
        &Panics,
        &|| 1_800_000_000,
        &mut |_| {},
    )
    .expect("harvest runs without analyzing");
    let outcome: String = w
        .conn
        .query_row(
            "SELECT outcome FROM audio_harvest_state WHERE part_id = 1",
            [],
            |r| r.get(0),
        )
        .expect("state row");
    assert_eq!(outcome, "done");
    let linked: i64 = w
        .conn
        .query_row(
            "SELECT count(*) FROM audio_part WHERE part_id = 1",
            [],
            |r| r.get(0),
        )
        .expect("audio_part");
    assert_eq!(linked, 1);
}

#[test]
fn harvest_audio_then_records_the_part_done_without_analysis() {
    harvest_after_import(Some(4));
}

#[test]
fn a_part_size_that_differs_from_the_file_length_still_matches() {
    harvest_after_import(Some(999));
}

#[test]
fn a_null_part_size_hashes_as_zero() {
    harvest_after_import(None);
}

#[test]
fn an_unreadable_media_root_stops_the_run_and_writes_nothing() {
    let w = World::new();
    w.file("a.mkv", b"aaaa");
    w.entry("1", "/media/a.mkv", "2026-01-01T00:00:00Z", Some(FACTS));
    let gone = w.media.join("unmounted");
    let err = import(
        &w.store,
        &w.catalog,
        &gone,
        &["/media".to_string()],
        false,
        &mut |_| {},
    )
    .expect_err("blocked");
    match err {
        Failure::Failed(message) => assert!(message.contains("media root"), "{message}"),
        other => panic!("unexpected {other:?}"),
    }
    assert_eq!(w.facts_rows(), 0);
}
