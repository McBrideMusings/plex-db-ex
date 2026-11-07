//! `harvest-audio` against a store built by the real migrations, small files in a temp dir
//! and a fake analyzer: each outcome, the retry clock, the blocked media root, the
//! unchanged-hash skip, the run lock and `audio status`'s counts.

mod common;

use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
use std::time::Duration;

use plexdb::audio_cmd;
use plexdb::harvest_audio::{
    AnalyzeError, Analyzer, MAX_DURATION_MS, ProcessAnalyzer, Report, RunLock, Settings,
    backoff_secs, harvest, harvest_running, parse_sections, status,
};
use plexdb::pyjson::dumps_indent2;
use plexdb::steps::Failure;
use rusqlite::Connection;
use serde_json::{Value, json};

const T0: i64 = 1_800_000_000;
const HOUR: i64 = 3600;

struct Fake {
    calls: RefCell<Vec<PathBuf>>,
    fail: Cell<bool>,
}

impl Fake {
    fn new() -> Fake {
        Fake {
            calls: RefCell::new(Vec::new()),
            fail: Cell::new(false),
        }
    }
    fn calls(&self) -> usize {
        self.calls.borrow().len()
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
}

struct World {
    _dir: tempfile::TempDir,
    store: PathBuf,
    media: PathBuf,
    conn: Connection,
    settings: Settings,
    clock: Cell<i64>,
}

impl World {
    fn new() -> World {
        let dir = tempfile::tempdir().expect("tempdir");
        let conn = common::store(dir.path());
        let media = dir.path().join("media");
        std::fs::create_dir(&media).expect("media dir");
        let settings = Settings {
            sections: vec!["1".into()],
            analyzer: "unused".into(),
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

    fn run(&self, analyzer: &dyn Analyzer) -> Result<Report, Failure> {
        harvest(
            &self.store,
            &self.settings,
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
fn sections_parse_as_a_deduplicated_comma_list_and_empty_is_off() {
    assert_eq!(parse_sections(" 1, 2 ,,1 "), ["1", "2"]);
    assert!(parse_sections("").is_empty());
}

#[test]
fn without_sections_the_command_exits_not_configured_and_analyzes_nothing() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .arg("harvest-audio")
        .env("PLEXDB_PATH", &w.store)
        .env_remove("PLEXDB_AUDIO_SECTIONS")
        .output()
        .expect("run plexdb-rs");
    assert_eq!(out.status.code(), Some(78));
    assert!(String::from_utf8_lossy(&out.stderr).contains("PLEXDB_AUDIO_SECTIONS is not set"));
    assert!(!w.has_state(1));

    let status = std::process::Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .args(["audio", "status"])
        .env("PLEXDB_PATH", &w.store)
        .env_remove("PLEXDB_AUDIO_SECTIONS")
        .output()
        .expect("run plexdb-rs");
    assert_eq!(status.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&status.stdout).contains("off: PLEXDB_AUDIO_SECTIONS"));
}

#[test]
fn status_counts_each_state_per_section() {
    let mut w = World::new();
    w.part(1, "1", "done.wav", b"aaaa", MIN_20);
    w.part(2, "1", "bad.wav", b"bbbbb", MIN_20);
    w.part(3, "1", "long.wav", b"cccccc", Some(MAX_DURATION_MS + 1));
    w.row(4, "1", "missing.wav", 4, MIN_20);
    w.part(5, "2", "other.wav", b"ddddddd", MIN_20);
    let fake = Fake::new();
    // Part 2 fails; the rest run normally.
    let mut sections = vec!["1".to_string(), "2".to_string()];
    w.settings.sections.clone_from(&sections);
    struct FailBad(Fake);
    impl Analyzer for FailBad {
        fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError> {
            if path.ends_with("bad.wav") {
                return Err(AnalyzeError::Failed("bad".into()));
            }
            self.0.analyze(path)
        }
    }
    w.run(&FailBad(fake)).expect("run");
    // A part Plex has added since: no state yet, so it is pending.
    w.part(6, "1", "new.wav", b"eeeeeeee", MIN_20);

    let counts = status(&w.store, &sections, T0).expect("status");
    let line = |id: &str| {
        let (_, c) = counts.iter().find(|(s, _)| s == id).unwrap();
        (c.done, c.pending, c.failed, c.unreachable, c.excluded)
    };
    assert_eq!(line("1"), (1, 1, 1, 1, 1));
    assert_eq!(line("2"), (1, 0, 0, 0, 0));

    // Once the retry is due, the unreachable part is pending again.
    let later = status(&w.store, &sections, T0 + HOUR).expect("status");
    let (_, c) = later.iter().find(|(s, _)| s == "1").unwrap();
    assert_eq!((c.pending, c.unreachable), (2, 0));

    let json = audio_cmd::report(&w.store, &sections, T0).expect("report");
    let text = dumps_indent2(&json);
    assert!(text.contains("\"enabled\": true") && text.contains("\"running\": false"));
    sections.clear();
    let off = dumps_indent2(&audio_cmd::report(&w.store, &sections, T0).expect("off"));
    assert!(off.contains("\"enabled\": false") && off.contains("off: PLEXDB_AUDIO_SECTIONS"));
}

#[test]
fn status_on_a_store_with_nothing_harvested_counts_every_part_pending() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let counts = status(&w.store, &["1".to_string()], T0).expect("status");
    assert_eq!(counts[0].1.pending, 1);
    assert_eq!(counts[0].1.done, 0);
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
    w.settings.sections = vec!["1".into(), "7".into()];

    let report = w.run(&Fake::new()).expect("run");

    assert_eq!(report.empty_sections, ["7"]);
    assert!(
        report
            .describe()
            .contains("section 7: no media_part rows — only movie and show sections are walked")
    );
    let counts = status(&w.store, &w.settings.sections, T0).expect("status");
    assert_eq!((counts[0].1.parts, counts[1].1.parts), (1, 0));
    let text = dumps_indent2(&audio_cmd::report(&w.store, &w.settings.sections, T0).unwrap());
    assert!(text.contains("\"parts\": 0"));
    assert_eq!(
        text.matches("only movie and show sections are walked")
            .count(),
        1
    );
}
