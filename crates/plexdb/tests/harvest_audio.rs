//! `harvest-audio` against a store built by the real migrations, small files in a temp dir
//! and a fake analyzer: each outcome, the retry clock, the blocked media root, the
//! unchanged-hash skip, the run lock, `audio status`'s counts, library order, extras and
//! the duration probe.

mod common;

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use plexdb::audio_cmd;
use plexdb::harvest_audio::{
    AnalyzeError, Analyzer, MAX_DURATION_MS, ProcessAnalyzer, Report, RunLock, Settings,
    backoff_secs, harvest, harvest_running, harvest_with, parse_probe_duration, resolve_libraries,
    status,
};
use plexdb::plex::Section;
use plexdb::pyjson::dumps_indent2;
use plexdb::steps::Failure;
use rusqlite::Connection;
use serde_json::{Value, json};

const T0: i64 = 1_800_000_000;
const HOUR: i64 = 3600;

struct Fake {
    calls: RefCell<Vec<PathBuf>>,
    fail: Cell<bool>,
    /// What the duration probe answers per file name; a file not named plays 5 minutes.
    probe: RefCell<HashMap<String, Result<Option<i64>, AnalyzeError>>>,
    probes: RefCell<Vec<PathBuf>>,
}

impl Fake {
    fn new() -> Fake {
        Fake {
            calls: RefCell::new(Vec::new()),
            fail: Cell::new(false),
            probe: RefCell::new(HashMap::new()),
            probes: RefCell::new(Vec::new()),
        }
    }
    fn calls(&self) -> usize {
        self.calls.borrow().len()
    }
    fn analyzed_names(&self) -> Vec<String> {
        self.calls
            .borrow()
            .iter()
            .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
            .collect()
    }
}

impl Analyzer for Fake {
    fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError> {
        self.calls.borrow_mut().push(path.to_path_buf());
        if self.fail.get() {
            return Err(AnalyzeError::Failed("could not decode".into()));
        }
        Ok(json!({"bpm": 120.0, "key": "C", "scale": "major"}))
    }

    fn duration_ms(&self, path: &Path) -> Result<Option<i64>, AnalyzeError> {
        self.probes.borrow_mut().push(path.to_path_buf());
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        self.probe
            .borrow()
            .get(&name)
            .cloned()
            .unwrap_or(Ok(MIN_20))
    }
}

struct World {
    _dir: tempfile::TempDir,
    store: PathBuf,
    media: PathBuf,
    conn: Connection,
    settings: Settings,
    sections: Vec<Section>,
    clock: Cell<i64>,
}

impl World {
    fn new() -> World {
        let dir = tempfile::tempdir().expect("tempdir");
        let conn = common::store(dir.path());
        let media = dir.path().join("media");
        std::fs::create_dir(&media).expect("media dir");
        let section = |key: &str, title: &str| Section {
            key: key.into(),
            kind: "movie".into(),
            title: title.into(),
        };
        let settings = Settings {
            libraries: vec!["Movies".into()],
            analyzer: "unused".into(),
            ffprobe: "unused".into(),
            timeout: Duration::from_secs(900),
            media_dir: media.clone(),
            source_roots: vec!["/plexroot".into()],
        };
        World {
            store: dir.path().join("plexdb.db"),
            _dir: dir,
            media,
            conn,
            settings,
            sections: vec![
                section("1", "Movies"),
                section("2", "TV Shows"),
                section("7", "Photos"),
            ],
            clock: Cell::new(T0),
        }
    }

    /// A media part in `section`: its file under the media dir holds `content`.
    fn part(&self, id: i64, section: &str, name: &str, content: &[u8], duration_ms: Option<i64>) {
        if !content.is_empty() {
            let path = self.media.join(name);
            std::fs::write(&path, content).expect("write media");
            let file = std::fs::File::options()
                .write(true)
                .open(&path)
                .expect("open");
            file.set_modified(
                std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000),
            )
            .expect("same mtime for every file");
        }
        self.row(id, section, name, content.len() as i64, duration_ms);
    }

    fn row(&self, id: i64, section: &str, name: &str, size: i64, duration_ms: Option<i64>) {
        let item = format!("plex:{id}");
        let key = format!("rk{id}");
        self.conn
            .execute(
                "INSERT INTO items (item_id, type, title) VALUES (?1, 'movie', ?1)",
                [&item],
            )
            .expect("item");
        self.conn
            .execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
                 VALUES (?1, ?2, ?3, 'x')",
                [&key, &item, section],
            )
            .expect("plex item");
        self.conn
            .execute(
                "INSERT INTO media_part (part_id, item_id, rating_key, file, size, duration_ms, \
                 fetched_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'x')",
                rusqlite::params![
                    id,
                    item,
                    key,
                    format!("/plexroot/{name}"),
                    size,
                    duration_ms
                ],
            )
            .expect("part");
    }

    /// An extra in `library` whose primary file `name` under the media dir holds `content`.
    /// Its `extra_file.path` is `/plexroot/<name>`, as Plex-style roots map it.
    fn extra(
        &self,
        library: &str,
        item: &str,
        first_seen: &str,
        name: &str,
        content: &[u8],
        duration_ms: Option<i64>,
    ) {
        let path = self.media.join(name);
        std::fs::write(&path, content).expect("write media");
        let file = std::fs::File::options()
            .write(true)
            .open(&path)
            .expect("open");
        file.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000))
            .expect("same mtime for every file");
        self.conn
            .execute(
                "INSERT OR IGNORE INTO extra_library (name, root, type, declared_at) \
                 VALUES (?1, '/plexroot', 'video', 't')",
                [library],
            )
            .expect("extra library");
        self.conn
            .execute(
                "INSERT INTO items (item_id, type, title) VALUES (?1, 'extra', ?1)",
                [item],
            )
            .expect("extra item row");
        self.conn
            .execute(
                "INSERT INTO extra_item (item_id, library, organized, first_seen) \
                 VALUES (?1, ?2, 1, ?3)",
                [item, library, first_seen],
            )
            .expect("extra_item");
        self.conn
            .execute(
                "INSERT INTO extra_file (path, item_id, primary_file, size, duration_ms, \
                 first_seen, last_seen) VALUES (?1, ?2, 1, ?3, ?4, 't', 't')",
                rusqlite::params![
                    format!("/plexroot/{name}"),
                    item,
                    content.len() as i64,
                    duration_ms
                ],
            )
            .expect("extra_file");
    }

    fn extra_state(&self, path: &str) -> (String, Option<String>) {
        self.conn
            .query_row(
                "SELECT outcome, error FROM audio_extra_state WHERE path = ?1",
                [path],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("an extra state row")
    }

    fn run(&self, analyzer: &dyn Analyzer) -> Result<Report, Failure> {
        harvest(
            &self.store,
            &self.settings,
            &self.sections,
            analyzer,
            &|| self.clock.get(),
            &mut |_| {},
        )
    }

    fn state(&self, id: i64) -> (String, i64, Option<String>, Option<String>) {
        self.conn
            .query_row(
                "SELECT outcome, attempts, next_try_at, error FROM audio_harvest_state \
                 WHERE part_id = ?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .expect("a state row")
    }

    fn count(&self, table: &str) -> i64 {
        self.conn
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .expect("count")
    }

    fn has_state(&self, id: i64) -> bool {
        self.conn
            .query_row(
                "SELECT count(*) FROM audio_harvest_state WHERE part_id = ?1",
                [id],
                |row| row.get::<_, i64>(0),
            )
            .expect("count")
            > 0
    }
}

const MIN_20: Option<i64> = Some(20 * 60_000);

#[test]
fn a_part_is_analyzed_once_and_its_facts_are_stored_verbatim() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!((report.candidates, report.analyzed), (1, 1));
    assert_eq!(fake.calls(), 1);
    assert_eq!(w.state(1).0, "done");
    let (hash, facts): (String, String) = w
        .conn
        .query_row("SELECT file_hash, facts FROM audio_facts", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .expect("facts");
    assert_eq!(
        serde_json::from_str::<Value>(&facts).unwrap(),
        json!({"bpm": 120.0, "key": "C", "scale": "major"})
    );
    let linked: (String, String) = w
        .conn
        .query_row(
            "SELECT item_id, file_hash FROM audio_part WHERE part_id = 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("audio_part");
    assert_eq!(linked, ("plex:1".to_string(), hash));

    // A second run settles it without hashing or analyzing.
    let again = w.run(&fake).expect("rerun");
    assert_eq!((again.settled, again.analyzed), (1, 0));
    assert_eq!(fake.calls(), 1);
}

#[test]
fn only_the_configured_sections_are_analyzed() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.part(2, "2", "b.wav", b"bbbbb", MIN_20);
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!(report.candidates, 1);
    assert!(!w.has_state(2));
}

#[test]
fn a_failed_part_keeps_its_error_and_is_not_retried_while_its_size_holds() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let fake = Fake::new();
    fake.fail.set(true);

    let report = w.run(&fake).expect("run");
    assert_eq!(report.failed, 1);
    let (outcome, attempts, _, error) = w.state(1);
    assert_eq!((outcome.as_str(), attempts), ("failed", 1));
    assert_eq!(error.as_deref(), Some("could not decode"));
    assert_eq!(w.count("audio_facts"), 0);
    assert_eq!(w.count("audio_part"), 0);
    let hash: Option<String> = w
        .conn
        .query_row("SELECT file_hash FROM audio_harvest_state", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert!(
        hash.is_some(),
        "a failed part records the hash it failed on"
    );

    fake.fail.set(false);
    let again = w.run(&fake).expect("rerun");

    assert_eq!((again.settled, again.analyzed, again.failed), (1, 0, 0));
    assert_eq!(fake.calls(), 1);
    assert_eq!(w.state(1).0, "failed");
}

#[test]
fn an_unreachable_part_waits_for_its_retry_time_and_the_wait_doubles_to_a_day() {
    let w = World::new();
    w.row(1, "1", "missing.wav", 4, MIN_20);
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");
    assert_eq!(report.unreachable, 1);
    assert_eq!(fake.calls(), 0);
    let (outcome, attempts, next, error) = w.state(1);
    assert_eq!((outcome.as_str(), attempts), ("unreachable", 1));
    assert_eq!(next.as_deref(), Some("2027-01-15T09:00:00+00:00"));
    assert!(error.unwrap().contains("missing.wav"));

    // Before next_try_at: nothing happens.
    w.clock.set(T0 + HOUR - 1);
    let early = w.run(&fake).expect("early");
    assert_eq!((early.settled, early.unreachable), (1, 0));
    assert_eq!(w.state(1).1, 1);

    // After: tried again, fails again, and the wait doubles.
    w.clock.set(T0 + HOUR);
    let retry = w.run(&fake).expect("retry");
    assert_eq!(retry.unreachable, 1);
    let (_, attempts, next, _) = w.state(1);
    assert_eq!(attempts, 2);
    assert_eq!(next.as_deref(), Some("2027-01-15T11:00:00+00:00"));

    // The file appears; the next due retry analyzes it.
    std::fs::write(w.media.join("missing.wav"), b"aaaa").unwrap();
    w.clock.set(T0 + HOUR + 2 * HOUR);
    let found = w.run(&fake).expect("found");
    assert_eq!(found.analyzed, 1);
    assert_eq!(w.state(1).0, "done");
}

#[test]
fn the_backoff_doubles_from_an_hour_and_stops_at_a_day() {
    let waits: Vec<i64> = (1..=7).map(backoff_secs).collect();
    assert_eq!(waits, [3600, 7200, 14_400, 28_800, 57_600, 86_400, 86_400]);
    assert_eq!(backoff_secs(500), 86_400);
}

#[test]
fn a_media_root_that_cannot_be_read_stops_the_run_and_marks_no_file() {
    let mut w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.settings.media_dir = w.media.join("gone");
    let fake = Fake::new();

    let report = w.run(&fake).expect("a blocked run is a report");

    let reason = report.blocked.expect("blocked");
    assert!(
        reason.starts_with("blocked: media root ") && reason.contains("unreadable"),
        "{reason}"
    );
    assert_eq!(fake.calls(), 0);
    assert!(!w.has_state(1), "the file is not marked unreachable");
}

#[test]
fn a_part_with_no_duration_or_over_thirty_minutes_is_excluded_without_a_decode() {
    let w = World::new();
    w.part(1, "1", "long.wav", b"aaaa", Some(MAX_DURATION_MS + 1));
    w.part(2, "1", "nodur.wav", b"bbbbb", None);
    w.part(3, "1", "edge.wav", b"cccccc", Some(MAX_DURATION_MS));
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!((report.excluded, report.analyzed), (2, 1));
    assert_eq!(fake.calls(), 1);
    assert_eq!(w.state(1).0, "excluded");
    assert!(w.state(1).3.unwrap().contains("limit"));
    assert_eq!(w.state(2).0, "excluded");
    assert!(w.state(2).3.unwrap().contains("unknown"));
    assert_eq!(w.state(3).0, "done");
    // Excluded until the size changes.
    let again = w.run(&fake).expect("rerun");
    assert_eq!((again.settled, again.excluded), (3, 0));
}

#[test]
fn a_second_part_with_the_same_content_gets_the_facts_without_an_analysis() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"same-bytes", MIN_20);
    w.part(2, "1", "copy.wav", b"same-bytes", MIN_20);
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!((report.analyzed, report.unchanged), (1, 1));
    assert_eq!(fake.calls(), 1);
    assert_eq!(w.count("audio_facts"), 1);
    assert_eq!(w.count("audio_part"), 2);
    assert_eq!(w.state(2).0, "done");
}

#[test]
fn a_changed_size_is_analyzed_again() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let fake = Fake::new();
    w.run(&fake).expect("run");
    std::fs::write(w.media.join("a.wav"), b"aaaaaaaa").unwrap();
    w.conn
        .execute("UPDATE media_part SET size = 8", [])
        .unwrap();

    let report = w.run(&fake).expect("rerun");

    assert_eq!(report.analyzed, 1);
    assert_eq!(fake.calls(), 2);
    assert_eq!(w.count("audio_facts"), 2);
}

#[test]
fn a_missing_analyzer_stops_the_run_and_records_nothing() {
    struct Gone;
    impl Analyzer for Gone {
        fn analyze(&self, _: &Path) -> Result<Value, AnalyzeError> {
            Err(AnalyzeError::Unavailable("cannot run x".into()))
        }
        fn duration_ms(&self, _: &Path) -> Result<Option<i64>, AnalyzeError> {
            Ok(Some(1))
        }
    }
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);

    assert!(matches!(w.run(&Gone), Err(Failure::Failed(_))));
    assert!(!w.has_state(1));
}

#[test]
fn a_second_run_while_one_holds_the_lock_is_busy() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    assert!(!harvest_running(&w.store));
    let held = RunLock::acquire(&w.store).expect("first run");
    assert!(harvest_running(&w.store));

    match w.run(&Fake::new()) {
        Err(Failure::Busy(message)) => assert_eq!(message, "another audio harvest is running"),
        other => panic!("expected busy, got {other:?}"),
    }
    assert!(!w.has_state(1));

    drop(held);
    assert!(!harvest_running(&w.store));
    assert!(w.run(&Fake::new()).is_ok());
}

#[test]
fn without_libraries_the_command_exits_not_configured_and_analyzes_nothing() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .arg("harvest-audio")
        .env("PLEXDB_PATH", &w.store)
        .env_remove("PLEXDB_AUDIO_LIBRARIES")
        .output()
        .expect("run plexdb-rs");
    assert_eq!(out.status.code(), Some(78));
    assert!(String::from_utf8_lossy(&out.stderr).contains("PLEXDB_AUDIO_LIBRARIES is not set"));
    assert!(!w.has_state(1));

    let status = std::process::Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .args(["audio", "status"])
        .env("PLEXDB_PATH", &w.store)
        .env_remove("PLEXDB_AUDIO_LIBRARIES")
        .output()
        .expect("run plexdb-rs");
    assert_eq!(status.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&status.stdout).contains("off: PLEXDB_AUDIO_LIBRARIES"));
}

/// `status` over the `names`, resolved against the world's sections and extras libraries.
fn status_of(w: &World, names: &[&str], now: i64) -> Vec<(String, plexdb::harvest_audio::Counts)> {
    let names: Vec<String> = names.iter().map(|name| name.to_string()).collect();
    let resolved = resolve_libraries(&w.conn, &names, &w.sections).expect("resolve");
    status(&w.store, &resolved, now)
        .expect("status")
        .into_iter()
        .map(|(library, counts)| (library.name, counts))
        .collect()
}

fn report_json(w: &World, names: &[&str]) -> String {
    let names: Vec<String> = names.iter().map(|name| name.to_string()).collect();
    dumps_indent2(&audio_cmd::report(&w.store, &names, &w.sections, None, T0).expect("report"))
}

#[test]
fn status_counts_each_state_per_library() {
    let mut w = World::new();
    w.part(1, "1", "done.wav", b"aaaa", MIN_20);
    w.part(2, "1", "bad.wav", b"bbbbb", MIN_20);
    w.part(3, "1", "long.wav", b"cccccc", Some(MAX_DURATION_MS + 1));
    w.row(4, "1", "missing.wav", 4, MIN_20);
    w.part(5, "2", "other.wav", b"ddddddd", MIN_20);
    let fake = Fake::new();
    // Part 2 fails; the rest run normally.
    w.settings.libraries = vec!["Movies".into(), "TV Shows".into()];
    struct FailBad(Fake);
    impl Analyzer for FailBad {
        fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError> {
            if path.ends_with("bad.wav") {
                return Err(AnalyzeError::Failed("bad".into()));
            }
            self.0.analyze(path)
        }
        fn duration_ms(&self, path: &Path) -> Result<Option<i64>, AnalyzeError> {
            self.0.duration_ms(path)
        }
    }
    w.run(&FailBad(fake)).expect("run");
    // A part Plex has added since: no state yet, so it is pending.
    w.part(6, "1", "new.wav", b"eeeeeeee", MIN_20);

    let counts = status_of(&w, &["Movies", "TV Shows"], T0);
    let line = |name: &str| {
        let (_, c) = counts.iter().find(|(s, _)| s == name).unwrap();
        (c.done, c.pending, c.failed, c.unreachable, c.excluded)
    };
    assert_eq!(line("Movies"), (1, 1, 1, 1, 1));
    assert_eq!(line("TV Shows"), (1, 0, 0, 0, 0));

    // Once the retry is due, the unreachable part is pending again.
    let later = status_of(&w, &["Movies", "TV Shows"], T0 + HOUR);
    let (_, c) = later.iter().find(|(s, _)| s == "Movies").unwrap();
    assert_eq!((c.pending, c.unreachable), (2, 0));

    let text = report_json(&w, &["Movies", "TV Shows"]);
    assert!(text.contains("\"enabled\": true") && text.contains("\"running\": false"));
    let off = report_json(&w, &[]);
    assert!(off.contains("\"enabled\": false") && off.contains("off: PLEXDB_AUDIO_LIBRARIES"));
}

#[test]
fn status_on_a_store_with_nothing_harvested_counts_every_part_pending() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let counts = status_of(&w, &["Movies"], T0);
    assert_eq!(counts[0].1.pending, 1);
    assert_eq!(counts[0].1.done, 0);
}

#[test]
fn an_extras_library_listed_first_is_analyzed_before_any_plex_section_and_sections_follow_list_order()
 {
    let mut w = World::new();
    w.part(1, "1", "movie.wav", b"mmmm", MIN_20);
    w.part(2, "2", "show.wav", b"sssss", MIN_20);
    // Two extras, the later first sighting inserted first: first_seen decides.
    w.extra(
        "Music Videos",
        "fs:late",
        "2026-02-01",
        "late.wav",
        b"lllllll",
        MIN_20,
    );
    w.extra(
        "Music Videos",
        "fs:early",
        "2026-01-01",
        "early.wav",
        b"eeeeee",
        MIN_20,
    );
    w.settings.libraries = vec!["Music Videos".into(), "TV Shows".into(), "Movies".into()];
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!((report.candidates, report.analyzed), (4, 4));
    assert_eq!(
        fake.analyzed_names(),
        ["early.wav", "late.wav", "show.wav", "movie.wav"]
    );
    assert!(
        fake.probes.borrow().is_empty(),
        "a recorded duration is never probed"
    );
    let text = report_json(&w, &["Music Videos", "TV Shows", "Movies"]);
    assert!(
        text.find("Music Videos") < text.find("TV Shows")
            && text.find("TV Shows") < text.find("Movies"),
        "{text}"
    );
}

#[test]
fn within_a_library_never_tried_parts_come_before_due_retries() {
    let w = World::new();
    w.row(1, "1", "missing.wav", 4, MIN_20);
    let fake = Fake::new();
    w.run(&fake).expect("first run records it unreachable");
    w.part(2, "1", "new.wav", b"nnnnn", MIN_20);
    w.clock.set(T0 + HOUR);
    std::fs::write(w.media.join("missing.wav"), b"aaaa").unwrap();

    w.run(&fake).expect("rerun");

    assert_eq!(fake.analyzed_names(), ["new.wav", "missing.wav"]);
}

#[test]
fn an_extras_facts_are_stored_by_path_and_a_moved_file_keeps_them() {
    let mut w = World::new();
    w.extra(
        "Music Videos",
        "fs:a",
        "2026-01-01",
        "a.wav",
        b"aaaa",
        MIN_20,
    );
    w.settings.libraries = vec!["Music Videos".into()];
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!((report.candidates, report.analyzed), (1, 1));
    assert_eq!(w.extra_state("/plexroot/a.wav").0, "done");
    let (item, hash): (String, String) = w
        .conn
        .query_row(
            "SELECT item_id, file_hash FROM audio_extra WHERE path = '/plexroot/a.wav'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .expect("audio_extra");
    assert_eq!(item, "fs:a");
    assert_eq!(w.count("audio_facts"), 1);
    assert_eq!(w.count("audio_part") + w.count("audio_harvest_state"), 0);

    // extras-organize renames the path in place; the foreign key carries the rows along.
    std::fs::rename(w.media.join("a.wav"), w.media.join("moved.wav")).unwrap();
    w.conn
        .execute(
            "UPDATE extra_file SET path = '/plexroot/moved.wav' WHERE path = '/plexroot/a.wav'",
            [],
        )
        .unwrap();
    let moved: String = w
        .conn
        .query_row("SELECT file_hash FROM audio_extra", [], |row| row.get(0))
        .expect("the row followed the file");
    assert_eq!(moved, hash);
    assert_eq!(w.extra_state("/plexroot/moved.wav").0, "done");

    let again = w.run(&fake).expect("rerun");
    assert_eq!((again.settled, again.analyzed), (1, 0));
    assert_eq!(fake.calls(), 1, "the move cost no analysis");

    // A file that is missing is no candidate, and deleting its row deletes the harvest rows.
    w.conn
        .execute("UPDATE extra_file SET missing_since = 't'", [])
        .unwrap();
    assert_eq!(w.run(&fake).expect("rerun").candidates, 0);
    w.conn.execute("DELETE FROM extra_file", []).unwrap();
    assert_eq!(w.count("audio_extra") + w.count("audio_extra_state"), 0);
}

#[test]
fn an_extra_with_no_recorded_duration_is_probed_and_excluded_or_failed_by_the_answer() {
    let mut w = World::new();
    w.extra(
        "Music Videos",
        "fs:ok",
        "2026-01-01",
        "ok.wav",
        b"1111",
        None,
    );
    w.extra(
        "Music Videos",
        "fs:long",
        "2026-01-02",
        "long.wav",
        b"22222",
        None,
    );
    w.extra(
        "Music Videos",
        "fs:none",
        "2026-01-03",
        "none.wav",
        b"333333",
        None,
    );
    w.extra(
        "Music Videos",
        "fs:bad",
        "2026-01-04",
        "bad.wav",
        b"4444444",
        None,
    );
    w.extra(
        "Music Videos",
        "fs:known-long",
        "2026-01-05",
        "known-long.wav",
        b"88888888",
        Some(MAX_DURATION_MS + 1),
    );
    w.settings.libraries = vec!["Music Videos".into()];
    let fake = Fake::new();
    fake.probe
        .borrow_mut()
        .insert("long.wav".into(), Ok(Some(MAX_DURATION_MS + 1)));
    fake.probe.borrow_mut().insert("none.wav".into(), Ok(None));
    fake.probe.borrow_mut().insert(
        "bad.wav".into(),
        Err(AnalyzeError::Failed(
            "ffprobe exited 1: moov atom not found".into(),
        )),
    );

    let report = w.run(&fake).expect("run");

    assert_eq!((report.analyzed, report.excluded, report.failed), (1, 3, 1));
    assert_eq!(fake.analyzed_names(), ["ok.wav"]);
    assert_eq!(
        fake.probes.borrow().len(),
        4,
        "the known duration was not probed"
    );
    assert_eq!(w.extra_state("/plexroot/ok.wav").0, "done");
    let (outcome, error) = w.extra_state("/plexroot/long.wav");
    assert_eq!(outcome, "excluded");
    assert!(error.unwrap().contains("limit"));
    let (outcome, error) = w.extra_state("/plexroot/none.wav");
    assert_eq!(outcome, "excluded");
    assert!(error.unwrap().contains("unknown"));
    let (outcome, error) = w.extra_state("/plexroot/bad.wav");
    assert_eq!(outcome, "failed");
    assert!(error.unwrap().contains("duration probe: ffprobe exited 1"));
    assert_eq!(w.extra_state("/plexroot/known-long.wav").0, "excluded");
    let column: Option<i64> = w
        .conn
        .query_row(
            "SELECT duration_ms FROM extra_file WHERE path = '/plexroot/ok.wav'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        column, None,
        "the probe never writes extra_file.duration_ms"
    );

    // All settled: a rerun probes and analyzes nothing.
    let again = w.run(&fake).expect("rerun");
    assert_eq!((again.settled, fake.probes.borrow().len()), (5, 4));
}

#[test]
fn a_listed_name_that_matches_nothing_is_reported_and_skipped_while_the_rest_run() {
    let mut w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.settings.libraries = vec!["Concerts".into(), "Movies".into()];
    let mut log = Vec::new();

    let report = harvest(
        &w.store,
        &w.settings,
        &w.sections,
        &Fake::new(),
        &|| T0,
        &mut |line| log.push(line.to_string()),
    )
    .expect("run");

    assert_eq!(report.analyzed, 1);
    assert_eq!(report.unknown_libraries, ["Concerts"]);
    assert!(
        report
            .describe()
            .contains("unknown library Concerts: not a Plex section title or an extras library"),
        "{}",
        report.describe()
    );
    assert!(
        log.iter()
            .any(|line| line.contains("unknown library Concerts"))
    );
    let text = report_json(&w, &["Concerts", "Movies"]);
    assert!(
        text.contains("\"unknown\": [\n    \"Concerts\"\n  ]"),
        "{text}"
    );
    assert!(text.contains("\"kind\": \"section\"") && text.contains("\"section_id\": \"1\""));
}

#[test]
fn status_names_each_library_with_its_kind_and_counts_extras() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.extra("Music Videos", "fs:a", "2026-01-01", "x.wav", b"xx", MIN_20);
    w.extra(
        "Empty Extras",
        "fs:gone",
        "2026-01-02",
        "g.wav",
        b"gg",
        MIN_20,
    );
    w.conn
        .execute(
            "UPDATE extra_file SET missing_since = 't' WHERE item_id = 'fs:gone'",
            [],
        )
        .unwrap();

    let text = report_json(&w, &["Music Videos", "Movies", "Empty Extras", "Nope"]);
    let json: Value = serde_json::from_str(&text).expect("JSON");

    assert_eq!(json["libraries"][0]["library"], "Music Videos");
    assert_eq!(json["libraries"][0]["kind"], "extras");
    assert_eq!(json["libraries"][0]["parts"], 1);
    assert_eq!(json["libraries"][0]["pending"], 1);
    assert_eq!(json["libraries"][1]["library"], "Movies");
    assert_eq!(json["libraries"][1]["kind"], "section");
    assert_eq!(json["libraries"][1]["section_id"], "1");
    assert_eq!(json["libraries"][2]["library"], "Empty Extras");
    assert_eq!(json["libraries"][2]["parts"], 0);
    assert!(
        json["libraries"][2]["note"]
            .as_str()
            .unwrap()
            .contains("extra_file")
    );
    assert_eq!(json["unknown"], json!(["Nope"]));
}

#[test]
fn ffprobe_durations_parse_to_milliseconds() {
    assert_eq!(parse_probe_duration("212.345000\n"), Ok(Some(212_345)));
    assert_eq!(parse_probe_duration("N/A\n"), Ok(None));
    assert_eq!(parse_probe_duration(""), Ok(None));
    assert_eq!(parse_probe_duration("0.000000"), Ok(None));
    assert!(parse_probe_duration("garbage").is_err());
}

#[cfg(unix)]
#[test]
fn the_analyzer_process_runs_niced_single_threaded_and_reports_a_failure_by_its_stderr() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("analyze.sh");
    std::fs::write(
        &script,
        "#!/bin/sh\n\
         case \"$1\" in *bad*) echo 'no audio stream' >&2; exit 3;; esac\n\
         printf '{\"file\": \"%s\", \"omp\": \"%s\", \"openblas\": \"%s\", \"nice\": %s}\\n' \
         \"$1\" \"$OMP_NUM_THREADS\" \"$OPENBLAS_NUM_THREADS\" \"$(ps -o ni= -p $$)\"\n",
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let analyzer = ProcessAnalyzer {
        program: script.to_string_lossy().into_owned(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(60),
    };

    let facts = analyzer.analyze(Path::new("/x/good.wav")).expect("facts");
    assert_eq!(facts["omp"], "1");
    assert_eq!(facts["openblas"], "1");
    assert_eq!(facts["nice"], 19);

    match analyzer.analyze(Path::new("/x/bad.wav")) {
        Err(AnalyzeError::Failed(message)) => {
            assert!(
                message.contains("exited 3") && message.contains("no audio stream"),
                "{message}"
            )
        }
        other => panic!("expected failed, got {other:?}"),
    }
    let missing = ProcessAnalyzer {
        program: "/nonexistent/analyzer".into(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(60),
    };
    assert!(matches!(
        missing.analyze(Path::new("/x/a.wav")),
        Err(AnalyzeError::Unavailable(_))
    ));
}

#[cfg(unix)]
#[test]
fn an_analyzer_past_its_timeout_is_killed_with_its_children_and_recorded_failed() {
    use std::os::unix::fs::PermissionsExt;
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let script = w._dir.path().join("slow.sh");
    // `sleep` is a child of the shell: killing only the shell would leave it holding the pipes.
    std::fs::write(&script, "#!/bin/sh\nsleep 30\necho '{}'\n").unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let analyzer = ProcessAnalyzer {
        program: script.to_string_lossy().into_owned(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(1),
    };

    let started = std::time::Instant::now();
    let report = w.run(&analyzer).expect("run");

    assert!(
        started.elapsed() < Duration::from_secs(10),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(report.failed, 1);
    let (outcome, _, _, error) = w.state(1);
    assert_eq!(outcome, "failed");
    assert_eq!(error.as_deref(), Some("analyzer timed out after 1s"));
    assert!(!harvest_running(&w.store), "the run lock is released");
}

#[test]
fn a_part_that_moves_off_done_loses_its_audio_part_row() {
    let w = World::new();
    w.part(1, "1", "a.wav", &[b'a'; 100], MIN_20);
    let fake = Fake::new();
    w.run(&fake).expect("run");
    assert_eq!(w.count("audio_part"), 1);

    // Plex re-lists the part at a new size and the analyzer now fails.
    std::fs::write(w.media.join("a.wav"), [b'a'; 120]).unwrap();
    w.conn
        .execute("UPDATE media_part SET size = 120", [])
        .unwrap();
    fake.fail.set(true);
    let report = w.run(&fake).expect("rerun");

    assert_eq!(report.failed, 1);
    assert_eq!(w.state(1).0, "failed");
    assert_eq!(
        w.count("audio_part"),
        0,
        "no row points at the old file's facts"
    );
    assert_eq!(
        w.count("audio_facts"),
        1,
        "the old content's facts stay under their hash"
    );

    // Excluded and unreachable clear it too.
    fake.fail.set(false);
    std::fs::write(w.media.join("a.wav"), [b'a'; 130]).unwrap();
    w.conn
        .execute("UPDATE media_part SET size = 130", [])
        .unwrap();
    w.run(&fake).expect("done again");
    assert_eq!(w.count("audio_part"), 1);
    w.conn
        .execute("UPDATE media_part SET size = 140, duration_ms = NULL", [])
        .unwrap();
    w.run(&fake).expect("excluded");
    assert_eq!(
        (w.state(1).0.as_str(), w.count("audio_part")),
        ("excluded", 0)
    );
}

#[test]
fn a_path_under_no_source_root_blocks_the_run_and_records_nothing() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.conn
        .execute("UPDATE media_part SET file = '/elsewhere/a.wav'", [])
        .unwrap();
    let fake = Fake::new();

    let report = w.run(&fake).expect("a blocked run is a report");

    let reason = report.blocked.expect("blocked");
    assert!(
        reason.starts_with("blocked: /elsewhere/a.wav is under none of PLEX_SOURCE_ROOTS"),
        "{reason}"
    );
    assert_eq!(fake.calls(), 0);
    assert!(!w.has_state(1));
}

#[test]
fn a_section_with_no_media_part_rows_is_flagged_in_the_report_and_in_status() {
    let mut w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.settings.libraries = vec!["Movies".into(), "Photos".into()];

    let report = w.run(&Fake::new()).expect("run");

    assert_eq!(report.empty_libraries.len(), 1);
    assert_eq!(report.empty_libraries[0].0, "Photos");
    assert!(
        report.describe().contains(
            "library Photos: no media_part rows — only movie and show sections are walked"
        )
    );
    let counts = status_of(&w, &["Movies", "Photos"], T0);
    assert_eq!((counts[0].1.parts, counts[1].1.parts), (1, 0));
    let text = report_json(&w, &["Movies", "Photos"]);
    assert!(text.contains("\"parts\": 0"));
    assert_eq!(
        text.matches("only movie and show sections are walked")
            .count(),
        1
    );
}

#[cfg(unix)]
#[test]
fn the_duration_probe_runs_ffprobe_and_reports_its_failure() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("ffprobe.sh");
    std::fs::write(
        &script,
        "#!/bin/sh\n\
         for last; do :; done\n\
         case \"$last\" in *bad*) echo 'moov atom not found' >&2; exit 1;; *none*) echo N/A; exit 0;; esac\n\
         echo 12.5\n",
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let analyzer = ProcessAnalyzer {
        program: "unused".into(),
        ffprobe: script.to_string_lossy().into_owned(),
        timeout: Duration::from_secs(60),
    };

    assert_eq!(
        analyzer.duration_ms(Path::new("/x/a.mp4")),
        Ok(Some(12_500))
    );
    assert_eq!(analyzer.duration_ms(Path::new("/x/none.mp4")), Ok(None));
    match analyzer.duration_ms(Path::new("/x/bad.mp4")) {
        Err(AnalyzeError::Failed(message)) => {
            assert!(
                message.contains("exited 1") && message.contains("moov atom"),
                "{message}"
            )
        }
        other => panic!("expected failed, got {other:?}"),
    }
    let missing = ProcessAnalyzer {
        program: "unused".into(),
        ffprobe: "/nonexistent/ffprobe".into(),
        timeout: Duration::from_secs(60),
    };
    assert!(matches!(
        missing.duration_ms(Path::new("/x/a.mp4")),
        Err(AnalyzeError::Unavailable(_))
    ));
}

#[test]
fn an_extra_whose_path_cannot_be_mapped_fails_alone_and_later_libraries_still_run() {
    let mut w = World::new();
    w.extra(
        "Music Videos",
        "fs:odd",
        "2026-01-01",
        "odd.wav",
        b"oooo",
        MIN_20,
    );
    w.conn
        .execute("UPDATE extra_file SET path = '/elsewhere/odd.wav'", [])
        .unwrap();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    w.settings.libraries = vec!["Music Videos".into(), "Movies".into()];
    let fake = Fake::new();

    let report = w.run(&fake).expect("run");

    assert_eq!(report.blocked, None);
    assert_eq!((report.failed, report.analyzed), (1, 1));
    let (outcome, error) = w.extra_state("/elsewhere/odd.wav");
    assert_eq!(outcome, "failed");
    assert!(
        error
            .unwrap()
            .contains("is under none of PLEX_SOURCE_ROOTS")
    );
    let again = w.run(&fake).expect("rerun");
    assert_eq!(
        (again.settled, again.failed),
        (2, 0),
        "kept while the size holds"
    );
}

#[test]
fn without_plex_sections_extras_still_run_and_plex_names_are_skipped_not_unknown() {
    let mut w = World::new();
    w.extra(
        "Music Videos",
        "fs:a",
        "2026-01-01",
        "a.wav",
        b"aaaa",
        MIN_20,
    );
    w.part(1, "1", "m.wav", b"mmmm", MIN_20);
    w.settings.libraries = vec!["Movies".into(), "Music Videos".into(), "Nope".into()];
    let fake = Fake::new();

    let report = harvest_with(
        &w.store,
        &w.settings,
        &[],
        Some("connection refused"),
        &fake,
        &|| T0,
        &mut |_| {},
    )
    .expect("run");

    assert_eq!(fake.analyzed_names(), ["a.wav"]);
    assert!(report.unknown_libraries.is_empty());
    assert_eq!(report.plex_skipped, ["Movies", "Nope"]);
    assert!(
        report
            .describe()
            .contains("; plex sections unavailable: connection refused, skipped: Movies, Nope"),
        "{}",
        report.describe()
    );
}
