//! Extras scrapers (ADR-0030): real executables run against a store the real migration built.

mod common;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use plexdb::extras_decisions::{self as decisions, Decision, Entry};
use plexdb::extras_declaration::fold_file;
use plexdb::extras_scrapers as scrapers;
use plexdb::extras_walk::walk_all;
use rusqlite::Connection;

const T1: &str = "2026-10-27T00:00:00+00:00";
const T2: &str = "2026-10-27T00:10:00+00:00";
const T3: &str = "2026-10-27T00:20:00+00:00";
const T4: &str = "2026-10-27T00:30:00+00:00";
const URL: &str = "https://example.com/watch/1";

struct World {
    dir: tempfile::TempDir,
    root: String,
    roots: Vec<String>,
    decisions: PathBuf,
    conn: Connection,
}

fn world() -> World {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = fs::canonicalize(dir.path())
        .expect("canonical")
        .to_string_lossy()
        .into_owned();
    fs::create_dir_all(format!("{root}/musicvideos")).expect("library root");
    let conn = common::store(dir.path());
    let json = dir.path().join("extras.json");
    fs::write(
        &json,
        format!(
            r#"[{{"name": "Music Videos", "root": "{root}/musicvideos", "type": "music-video",
                "template": "{{artist}}/{{title}}",
                "fields": [{{"name": "artist", "kind": "text", "required": true}},
                           {{"name": "featured", "kind": "list"}},
                           {{"name": "title", "kind": "text", "required": true}},
                           {{"name": "year", "kind": "year"}}]}}]"#
        ),
    )
    .expect("extras.json");
    let roots = vec![root.clone()];
    let mut w = World {
        decisions: dir.path().join("extra_decisions.json"),
        root,
        roots,
        conn,
        dir,
    };
    fold_file(&mut w.conn, &json, &w.roots, T1).expect("declare");
    w
}

/// A queued item whose `source_url` is `url`.
fn queued(w: &mut World, url: &str) -> String {
    walk_all(&mut w.conn, &w.roots, None, T1).expect("first walk");
    let path = format!("{}/musicvideos/drop.mkv", w.root);
    fs::write(&path, "a").expect("write");
    walk_all(&mut w.conn, &w.roots, None, T2).expect("walk");
    let id: String = w
        .conn
        .query_row(
            "SELECT item_id FROM extra_file WHERE path = ?",
            [&path],
            |row| row.get(0),
        )
        .expect("item");
    w.conn
        .execute(
            "UPDATE extra_item SET organized = 0, source_url = ? WHERE item_id = ?",
            [url, &id],
        )
        .expect("queue");
    id
}

/// A scraper folder `name` whose executable is the shell `body`.
fn scraper(w: &World, name: &str, config: &str, body: &str) -> PathBuf {
    let dir = w.dir.path().join(scrapers::DIR);
    let folder = dir.join(name);
    fs::create_dir_all(&folder).expect("scraper folder");
    fs::write(folder.join("scraper.toml"), config).expect("scraper.toml");
    let run = folder.join("run.sh");
    fs::write(&run, format!("#!/bin/sh\n{body}\n")).expect("script");
    fs::set_permissions(&run, fs::Permissions::from_mode(0o755)).expect("chmod");
    dir
}

const CONFIG: &str =
    "match = ['^https://example\\.com/']\nrun = \"run.sh\"\nauto = true\nretries = 0\n";
const SAYS: &str = r#"printf '{"title": "Song", "artist": "Band", "featured": "A; B", "year": 1999, "mood": "sad", "tags": ["synth"]}'"#;

fn fetch(w: &World, id: &str, at: &str) {
    let entry = Entry {
        decision: Decision::Fetch { item_id: id.into() },
        decided_at: at.into(),
    };
    decisions::record(&w.decisions, &entry).expect("record");
}

fn entries(w: &World) -> Vec<Entry> {
    decisions::load(&w.decisions)
        .expect("load")
        .unwrap_or_default()
}

fn run_all(w: &mut World, dir: &Path, at: &str) -> scrapers::Stats {
    let (found, broken) = scrapers::discover(dir);
    assert!(broken.is_empty(), "{broken:?}");
    let jobs = scrapers::due(&w.conn, &found, &entries(w)).expect("due");
    let done = scrapers::execute(&found, &jobs);
    scrapers::apply(&mut w.conn, &done, at).expect("apply")
}

fn values(conn: &Connection, id: &str, source: &str, key: &str) -> Vec<String> {
    let mut stmt = conn
        .prepare(
            "SELECT value FROM enrichment WHERE item_id = ? AND namespace = 'extra' \
             AND source = ? AND key = ? ORDER BY rank",
        )
        .expect("prepare");
    stmt.query_map([id, source, key], |row| row.get(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

#[test]
fn an_auto_scraper_proposes_each_declared_key_and_drops_the_rest() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let dir = scraper(&w, "fake", CONFIG, SAYS);
    let stats = run_all(&mut w, &dir, T3);
    assert_eq!(stats.succeeded, 1, "{stats:?}");
    assert_eq!(values(&w.conn, &id, "fake", "title"), ["Song"]);
    assert_eq!(values(&w.conn, &id, "fake", "artist"), ["Band"]);
    assert_eq!(values(&w.conn, &id, "fake", "featured"), ["A", "B"]);
    assert_eq!(values(&w.conn, &id, "fake", "year"), ["1999"]);
    assert_eq!(stats.ignored, ["fake: mood"]);
    assert!(values(&w.conn, &id, "fake", "mood").is_empty());
    let tags: i64 = w
        .conn
        .query_row(
            "SELECT COUNT(*) FROM enrichment WHERE item_id = ? AND namespace = 'keywords' \
             AND source = 'fake'",
            [&id],
            |row| row.get(0),
        )
        .expect("count");
    assert!(tags > 0, "the tag was stored");
    let ok: i64 = w
        .conn
        .query_row(
            "SELECT ok FROM extra_scrape WHERE item_id = ? AND scraper = 'fake'",
            [&id],
            |row| row.get(0),
        )
        .expect("last result");
    assert_eq!(ok, 1);
    let again = run_all(&mut w, &dir, T4);
    assert_eq!(
        again.succeeded + again.failed,
        0,
        "an auto run is owed once"
    );
}

#[test]
fn a_confirmed_value_survives_a_later_scrape() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let dir = scraper(&w, "fake", CONFIG, SAYS);
    let entry = Entry {
        decision: Decision::Set {
            item_id: id.clone(),
            field: "title".into(),
            value: "My Title".into(),
        },
        decided_at: T3.into(),
    };
    decisions::record(&w.decisions, &entry).expect("record");
    decisions::fold(&mut w.conn, &w.decisions, &w.roots, T3).expect("fold");
    run_all(&mut w, &dir, T3);
    assert_eq!(values(&w.conn, &id, "person", "title"), ["My Title"]);
    assert_eq!(values(&w.conn, &id, "fake", "title"), ["Song"]);
    let title: String = w
        .conn
        .query_row("SELECT title FROM items WHERE item_id = ?", [&id], |row| {
            row.get(0)
        })
        .expect("title");
    assert_eq!(title, "My Title");
    fetch(&w, &id, T4);
    run_all(&mut w, &dir, T4);
    assert_eq!(values(&w.conn, &id, "person", "title"), ["My Title"]);
}

#[test]
fn an_auto_false_scraper_runs_only_after_a_fetch() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let manual = CONFIG.replace("auto = true", "auto = false");
    let dir = scraper(&w, "fake", &manual, SAYS);
    let idle = run_all(&mut w, &dir, T3);
    assert_eq!(idle.succeeded + idle.failed, 0);
    assert!(values(&w.conn, &id, "fake", "title").is_empty());
    fetch(&w, &id, T3);
    assert_eq!(run_all(&mut w, &dir, T3).succeeded, 1);
    assert_eq!(values(&w.conn, &id, "fake", "title"), ["Song"]);
    let same = run_all(&mut w, &dir, T4);
    assert_eq!(same.succeeded, 0, "the fetch was answered");
    fetch(&w, &id, "2026-10-27T01:00:00+00:00");
    assert_eq!(
        run_all(&mut w, &dir, "2026-10-27T01:01:00+00:00").succeeded,
        1
    );
}

#[test]
fn a_scraper_that_does_not_match_the_url_never_runs() {
    let mut w = world();
    let id = queued(&mut w, "https://other.org/x");
    let dir = scraper(&w, "fake", CONFIG, SAYS);
    fetch(&w, &id, T3);
    let stats = run_all(&mut w, &dir, T3);
    assert_eq!(stats.succeeded + stats.failed, 0);
}

#[test]
fn a_failure_is_recorded_with_why_and_changes_no_proposal() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let dir = scraper(&w, "fake", CONFIG, SAYS);
    // The auto run and the fetch carry the same second: the fetch still runs.
    run_all(&mut w, &dir, T4);
    fs::write(
        dir.join("fake/run.sh"),
        "#!/bin/sh\necho 'network is down' >&2\nexit 3\n",
    )
    .expect("break it");
    fetch(&w, &id, T4);
    let stats = run_all(&mut w, &dir, T4);
    assert_eq!(stats.failed, 1, "{stats:?}");
    let (ok, attempts, error): (i64, i64, String) = w
        .conn
        .query_row(
            "SELECT ok, attempts, error FROM extra_scrape WHERE item_id = ?",
            [&id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .expect("result");
    assert_eq!((ok, attempts), (0, 1));
    assert!(error.contains("network is down"), "{error}");
    assert_eq!(values(&w.conn, &id, "fake", "title"), ["Song"]);
}

#[test]
fn a_retry_limit_and_a_timeout_bound_every_run() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let config = CONFIG.replace("retries = 0", "retries = 1\ntimeout = 1");
    let dir = scraper(&w, "slow", &config, "sleep 5");
    let stats = run_all(&mut w, &dir, T3);
    assert_eq!(stats.failed, 1, "{stats:?}");
    let (attempts, error): (i64, String) = w
        .conn
        .query_row(
            "SELECT attempts, error FROM extra_scrape WHERE item_id = ?",
            [&id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .expect("result");
    assert_eq!(attempts, 2);
    assert!(error.contains("timed out"), "{error}");
}

#[test]
fn the_shipped_youtube_scraper_is_seeded_once_and_never_again() {
    let w = world();
    let dir = w.dir.path().join(scrapers::DIR);
    assert!(scrapers::seed(&dir).expect("seed"));
    let (found, broken) = scrapers::discover(&dir);
    assert!(broken.is_empty(), "{broken:?}");
    let youtube = found.iter().find(|s| s.name == "youtube").expect("youtube");
    assert!(youtube.auto);
    assert!(youtube.handles("https://www.youtube.com/watch?v=3YxaaGgTQYM"));
    assert!(youtube.handles("https://youtu.be/3YxaaGgTQYM"));
    assert!(!youtube.handles("https://vimeo.com/1"));
    let mode = fs::metadata(dir.join("youtube/youtube.py"))
        .expect("script")
        .permissions()
        .mode();
    assert_ne!(mode & 0o111, 0, "the script is executable");
    fs::remove_dir_all(dir.join("youtube")).expect("a person deletes it");
    assert!(!scrapers::seed(&dir).expect("seed again"));
    assert!(!dir.join("youtube").exists());
}

#[test]
fn a_folder_that_cannot_run_is_listed_with_why() {
    let w = world();
    let dir = scraper(&w, "bad", "match = [\"(\"]\nrun = \"run.sh\"\n", "true");
    let (found, broken) = scrapers::discover(&dir);
    assert!(found.is_empty());
    assert_eq!(broken.len(), 1);
    assert!(broken[0].why.contains("match"), "{}", broken[0].why);
}

#[test]
fn a_folder_named_like_a_person_or_a_parser_is_refused() {
    let w = world();
    let dir = scraper(&w, "person", CONFIG, SAYS);
    scraper(&w, "path", CONFIG, SAYS);
    let (found, broken) = scrapers::discover(&dir);
    assert!(found.is_empty());
    assert_eq!(broken.len(), 2);
}

#[test]
fn a_changed_source_url_is_scraped_again_but_a_failed_run_is_not_retried_alone() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let dir = scraper(&w, "fake", CONFIG, SAYS);
    assert_eq!(run_all(&mut w, &dir, T3).succeeded, 1);
    w.conn
        .execute(
            "UPDATE extra_item SET source_url = 'https://example.com/watch/2' WHERE item_id = ?",
            [&id],
        )
        .expect("new url");
    assert_eq!(run_all(&mut w, &dir, T4).succeeded, 1);
    fs::write(dir.join("fake/run.sh"), "#!/bin/sh\nexit 1\n").expect("break it");
    fetch(&w, &id, T4);
    assert_eq!(run_all(&mut w, &dir, T4).failed, 1);
    let idle = run_all(&mut w, &dir, T4);
    assert_eq!(idle.succeeded + idle.failed, 0);
}

#[test]
fn an_empty_tags_list_keeps_the_tags_already_proposed() {
    let mut w = world();
    let id = queued(&mut w, URL);
    let dir = scraper(&w, "fake", CONFIG, SAYS);
    run_all(&mut w, &dir, T3);
    fs::write(
        dir.join("fake/run.sh"),
        "#!/bin/sh\nprintf '{\"tags\": []}'\n",
    )
    .expect("rewrite");
    fetch(&w, &id, T4);
    run_all(&mut w, &dir, T4);
    let tags: i64 = w
        .conn
        .query_row(
            "SELECT COUNT(*) FROM enrichment WHERE item_id = ? AND namespace = 'keywords' \
             AND source = 'fake'",
            [&id],
            |row| row.get(0),
        )
        .expect("count");
    assert!(tags > 0);
}
