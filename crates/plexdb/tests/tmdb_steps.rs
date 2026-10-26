//! `plexdb-rs enrich-tmdb-keywords` as a person or the sweep runs it: the flags, the
//! credential check and the report, against a local server that speaks TMDB.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Output};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use plexdb::migrate::migrate;
use rusqlite::Connection;

/// A TMDB-shaped server answering every keywords request with two keywords, and
/// counting what it was asked.
fn serve() -> (String, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let base = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("addr").port()
    );
    let asked = Arc::new(AtomicUsize::new(0));
    let seen = Arc::clone(&asked);
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let mut buffer = [0u8; 4096];
            let _ = stream.read(&mut buffer);
            seen.fetch_add(1, Ordering::SeqCst);
            let body = br#"{"id": 1, "keywords": [{"id": 1, "name": "Heists"}, {"id": 2, "name": "vault"}]}"#;
            let head = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(body);
        }
    });
    (base, asked)
}

fn store(dir: &Path) -> std::path::PathBuf {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open");
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat');
         INSERT INTO external_ids (item_id, ns, value, kind, last_seen)
             VALUES ('imdb:tt1', 'tmdb', '949', 'movie', '2026-01-01T00:00:00+00:00');",
    )
    .expect("seed");
    path
}

fn run(store: &Path, base: &str, key: Option<&str>, args: &[&str]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"));
    command
        .arg("enrich-tmdb-keywords")
        .args(args)
        .env("PLEXDB_PATH", store)
        .env("TMDB_BASE_URL", base)
        .env_remove("PLEXDB_HELD_STORE")
        .env_remove("TMDB_KEYWORDS_STALE_DAYS")
        .env_remove("TMDB_API_KEY");
    if let Some(key) = key {
        command.env("TMDB_API_KEY", key);
    }
    command.output().expect("run plexdb-rs")
}

fn out(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn err(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

#[test]
fn a_run_fetches_once_a_second_run_asks_nothing_and_rewipe_asks_again() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path());
    let (base, asked) = serve();

    let first = run(&path, &base, Some("k"), &[]);
    let second = run(&path, &base, Some("k"), &[]);
    let asked_after_two = asked.load(Ordering::SeqCst);
    let rewiped = run(&path, &base, Some("k"), &["--rewipe"]);

    assert_eq!(
        out(&first),
        "tmdb keywords: 1 title(s) seen, 1 fetched, 0 already cached, 0 skipped (no tmdb id), \
         0 failed, 2 keyword(s) written\n"
    );
    assert_eq!(asked_after_two, 1);
    assert!(
        out(&second).contains("0 fetched, 1 already cached"),
        "{}",
        out(&second)
    );
    assert_eq!(
        out(&rewiped),
        "wiped 3 row(s) from the keywords namespace\n\
         tmdb keywords: 1 title(s) seen, 1 fetched, 0 already cached, 0 skipped (no tmdb id), \
         0 failed, 2 keyword(s) written\n"
    );
    assert_eq!(asked.load(Ordering::SeqCst), 2);
    let conn = Connection::open(&path).expect("open");
    let stored: Vec<String> = conn
        .prepare("SELECT value FROM enrichment WHERE namespace = 'keywords' ORDER BY value")
        .expect("prepare")
        .query_map([], |r| r.get(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows");
    assert_eq!(stored, ["heist", "vault"]);
}

#[test]
fn a_missing_key_is_a_setting_that_is_absent_and_a_bad_flag_is_a_usage_error() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path());

    let no_key = run(&path, "http://127.0.0.1:1", None, &[]);
    let blank_key = run(&path, "http://127.0.0.1:1", Some("  "), &[]);
    let bad_flag = run(&path, "http://127.0.0.1:1", Some("k"), &["--nope"]);
    let bad_days = run(
        &path,
        "http://127.0.0.1:1",
        Some("k"),
        &["--stale-days", "soon"],
    );

    // 78 is EX_CONFIG: the sweep reports the step as skipped.
    for output in [&no_key, &blank_key] {
        assert_eq!(output.status.code(), Some(78));
        assert_eq!(
            err(output),
            "error: TMDB_API_KEY must be set in .env to fetch TMDB keywords\n"
        );
    }
    assert_eq!(bad_flag.status.code(), Some(2));
    assert!(
        err(&bad_flag).contains("unrecognized arguments: --nope"),
        "{}",
        err(&bad_flag)
    );
    assert_eq!(bad_days.status.code(), Some(2));
}

#[test]
fn the_staleness_window_comes_from_the_flag_then_the_environment() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path());
    let (base, asked) = serve();
    run(&path, &base, Some("k"), &[]);
    Connection::open(&path)
        .expect("open")
        .execute(
            "UPDATE enrichment_cursor SET fetched_at = '2026-01-01T00:00:00+00:00'",
            [],
        )
        .expect("age");

    // A window longer than the age keeps the title cached; the flag wins over the variable.
    let long = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .args(["enrich-tmdb-keywords", "--stale-days", "100000"])
        .env("PLEXDB_PATH", &path)
        .env("TMDB_BASE_URL", &base)
        .env("TMDB_API_KEY", "k")
        .env("TMDB_KEYWORDS_STALE_DAYS", "1")
        .env_remove("PLEXDB_HELD_STORE")
        .output()
        .expect("run");
    let asked_inside_window = asked.load(Ordering::SeqCst);
    let by_variable = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .arg("enrich-tmdb-keywords")
        .env("PLEXDB_PATH", &path)
        .env("TMDB_BASE_URL", &base)
        .env("TMDB_API_KEY", "k")
        .env("TMDB_KEYWORDS_STALE_DAYS", "1")
        .env_remove("PLEXDB_HELD_STORE")
        .output()
        .expect("run");

    assert!(
        out(&long).contains("0 fetched, 1 already cached"),
        "{}",
        out(&long)
    );
    assert_eq!(asked_inside_window, 1);
    assert!(
        out(&by_variable).contains("1 fetched, 0 already cached"),
        "{}",
        out(&by_variable)
    );
    assert_eq!(asked.load(Ordering::SeqCst), 2);
}
