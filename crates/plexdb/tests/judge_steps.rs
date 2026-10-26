//! The four keyword-verdict steps as a person or the sweep runs them: `plexdb-rs
//! prune-keyword-verdicts`, `film-suffix-verdicts`, `judge-keyword-roles` and
//! `judge-keyword-pairs`, against a store built by `plexdb migrate` and a local server
//! that speaks both the embedding endpoint and Jev's.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use plexdb::migrate::migrate;
use rusqlite::Connection;
use serde_json::{Value, json};

struct Fake {
    base: String,
    jev_calls: Arc<AtomicUsize>,
    embed_calls: Arc<AtomicUsize>,
}

fn read_request(stream: &mut TcpStream) -> (String, Value) {
    let mut bytes = Vec::new();
    let mut chunk = [0u8; 4096];
    let (head_end, length) = loop {
        let n = stream.read(&mut chunk).expect("read");
        bytes.extend_from_slice(&chunk[..n]);
        if let Some(at) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&bytes[..at]).to_lowercase();
            let length = head
                .lines()
                .find_map(|l| l.strip_prefix("content-length: "))
                .and_then(|v| v.trim().parse::<usize>().ok())
                .unwrap_or(0);
            break (at + 4, length);
        }
    };
    while bytes.len() < head_end + length {
        let n = stream.read(&mut chunk).expect("read body");
        bytes.extend_from_slice(&chunk[..n]);
    }
    let head = String::from_utf8_lossy(&bytes[..head_end]).into_owned();
    let path = head.split_whitespace().nth(1).unwrap_or("").to_string();
    let body = serde_json::from_slice(&bytes[head_end..head_end + length]).unwrap_or(Value::Null);
    (path, body)
}

fn reply(stream: &mut TcpStream, status: u16, body: &Value) {
    let text = body.to_string();
    let head = format!(
        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
         Connection: close\r\n\r\n",
        text.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(text.as_bytes());
}

/// Embeds `heist` and `caper` close together and `zebra` far from both; Jev scores every
/// pair 0.9 and every role 0.25, and refuses the tag `vault`.
fn serve() -> Fake {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let base = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("addr").port()
    );
    let jev_calls = Arc::new(AtomicUsize::new(0));
    let embed_calls = Arc::new(AtomicUsize::new(0));
    let (jev, embed) = (Arc::clone(&jev_calls), Arc::clone(&embed_calls));
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let (path, body) = read_request(&mut stream);
            if path == "/v1/embeddings" {
                embed.fetch_add(1, Ordering::SeqCst);
                let rows: Vec<Value> = body["input"]
                    .as_array()
                    .expect("input")
                    .iter()
                    .enumerate()
                    .map(|(index, text)| {
                        let vector = match text.as_str().unwrap_or("") {
                            "heist" => [1.0, 0.0],
                            "caper" => [0.99, 0.1],
                            _ => [0.0, 1.0],
                        };
                        json!({"index": index, "embedding": vector})
                    })
                    .collect();
                reply(&mut stream, 200, &json!({"data": rows}));
                continue;
            }
            jev.fetch_add(1, Ordering::SeqCst);
            if body["state"]["tag"] == "vault" {
                reply(&mut stream, 422, &json!({}));
            } else if body["state"].get("tag_a").is_some() {
                reply(
                    &mut stream,
                    200,
                    &json!({"answers": {"n": {"noul": 0.9}, "s": {"score": 2}}, "model": "jev-test"}),
                );
            } else {
                let answers: Value = ["tone", "era", "region", "theme", "character_trait"]
                    .iter()
                    .map(|role| (role.to_string(), json!({"noul": 0.25})))
                    .collect::<serde_json::Map<_, _>>()
                    .into();
                reply(
                    &mut stream,
                    200,
                    &json!({"answers": answers, "model": "jev-test"}),
                );
            }
        }
    });
    Fake {
        base,
        jev_calls,
        embed_calls,
    }
}

fn store(dir: &Path, keywords: &[&str]) -> PathBuf {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open");
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')",
        [],
    )
    .expect("item");
    for keyword in keywords {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, '2026-01-01T00:00:00+00:00')",
            [keyword],
        )
        .expect("keyword");
    }
    path
}

fn run(store: &Path, fake: Option<&Fake>, args: &[&str]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"));
    command
        .args(args)
        .env("PLEXDB_PATH", store)
        .env_remove("PLEXDB_HELD_STORE")
        .env_remove("LLAMA_BROKER_BASE_URL")
        .env_remove("TYPESAFE_API_KEY")
        .env_remove("JEV_URL");
    if let Some(fake) = fake {
        command
            .env("LLAMA_BROKER_BASE_URL", &fake.base)
            .env("JEV_URL", format!("{}/v1/systemone", fake.base))
            .env("TYPESAFE_API_KEY", "test-key");
    }
    command.output().expect("run plexdb-rs")
}

fn out(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn rows(store: &Path, sql: &str) -> Vec<String> {
    let conn = Connection::open(store).expect("open");
    let mut statement = conn.prepare(sql).expect("prepare");
    statement
        .query_map([], |row| {
            let columns = row.as_ref().column_count();
            Ok((0..columns)
                .map(|i| {
                    row.get::<_, rusqlite::types::Value>(i)
                        .map(|v| match v {
                            rusqlite::types::Value::Null => "NULL".to_string(),
                            rusqlite::types::Value::Integer(n) => n.to_string(),
                            rusqlite::types::Value::Real(f) => f.to_string(),
                            rusqlite::types::Value::Text(t) => t,
                            rusqlite::types::Value::Blob(_) => "blob".to_string(),
                        })
                        .expect("cell")
                })
                .collect::<Vec<_>>()
                .join("|"))
        })
        .expect("query")
        .map(|row| row.expect("row"))
        .collect()
}

#[test]
fn prune_deletes_rows_for_a_keyword_no_title_carries() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path(), &["heist"]);
    Connection::open(&path)
        .expect("open")
        .execute_batch(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) \
                 VALUES ('ghost', 'heist', 0.9, 'm', 't'), ('heist', 'heist2', 0.9, 'm', 't');
             INSERT INTO keyword_roles (keyword, role, source, stated_at) \
                 VALUES ('ghost', 'tone', 'jev', 't'), ('heist', 'tone', 'jev', 't');",
        )
        .expect("seed verdicts");
    let output = run(&path, None, &["prune-keyword-verdicts"]);
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        out(&output),
        "pruned 2 keyword value(s) no title carries (of 1 stored): 2 pair row(s), 1 role row(s), \
         0 decision row(s), 0 name row(s)\npruned keyword 'ghost'\npruned keyword 'heist2'\n"
    );
    assert_eq!(rows(&path, "SELECT keyword FROM keyword_roles"), ["heist"]);
    assert!(rows(&path, "SELECT * FROM keyword_pairs").is_empty());
}

#[test]
fn film_suffix_pairs_a_genre_with_its_film_form_except_the_kept_ones() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(
        dir.path(),
        &["horror", "horror film", "art", "art film", "lone film"],
    );
    let output = run(&path, None, &["film-suffix-verdicts"]);
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        out(&output),
        "film-suffix rule: 1 pair(s) held, 1 written now, 0 removed\n"
    );
    assert_eq!(
        rows(
            &path,
            "SELECT keyword_a, keyword_b, jev_score, jev_model FROM keyword_pairs"
        ),
        ["horror|horror film|1|rule:film-suffix"]
    );
    let again = run(&path, None, &["film-suffix-verdicts"]);
    assert_eq!(
        out(&again),
        "film-suffix rule: 1 pair(s) held, 0 written now, 0 removed\n"
    );
}

#[test]
fn judge_roles_writes_five_rows_a_keyword_and_asks_each_keyword_once() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path(), &["heist", "vault"]);
    let fake = serve();
    let output = run(&path, Some(&fake), &["judge-keyword-roles"]);
    assert!(output.status.success(), "{output:?}");
    assert!(
        out(&output).ends_with(
            "keyword roles: 2 keyword(s) stored, 0 already judged, 1 judged now, \
             1 unjudgeable, 10 row(s) written\n"
        ),
        "{}",
        out(&output)
    );
    assert_eq!(
        rows(
            &path,
            "SELECT keyword, role, score, model, error IS NOT NULL FROM keyword_roles \
             WHERE keyword = 'heist' AND role IN ('tone', 'character_trait') ORDER BY role"
        ),
        [
            "heist|character_trait|0.25|jev-test|0",
            "heist|tone|0.25|jev-test|0"
        ]
    );
    assert_eq!(
        rows(
            &path,
            "SELECT COUNT(*), COUNT(score), COUNT(model) FROM keyword_roles WHERE keyword = 'vault'"
        ),
        ["5|0|0"]
    );
    let asked = fake.jev_calls.load(Ordering::SeqCst);
    let again = run(&path, Some(&fake), &["judge-keyword-roles"]);
    assert!(again.status.success());
    assert_eq!(
        fake.jev_calls.load(Ordering::SeqCst),
        asked,
        "nothing asked twice"
    );
}

#[test]
fn judge_pairs_asks_about_near_neighbours_only_and_keeps_vectors_between_runs() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path(), &["heist", "caper", "zebra"]);
    let fake = serve();
    let output = run(&path, Some(&fake), &["judge-keyword-pairs"]);
    assert!(output.status.success(), "{output:?}");
    assert!(
        out(&output).ends_with(
            "keyword pairs: 3 keyword(s) stored, 3 examined, 1 pair(s) proposed, \
             0 already judged, 1 judged now, 0 unjudgeable\n"
        ),
        "{}",
        out(&output)
    );
    assert_eq!(
        rows(
            &path,
            "SELECT keyword_a, keyword_b, jev_score, jev_model FROM keyword_pairs"
        ),
        ["caper|heist|0.9|jev-test"]
    );
    assert!(dir.path().join("keyword-embeddings.bin").is_file());
    let (jev, embed) = (
        fake.jev_calls.load(Ordering::SeqCst),
        fake.embed_calls.load(Ordering::SeqCst),
    );
    let again = run(&path, Some(&fake), &["judge-keyword-pairs"]);
    assert!(again.status.success());
    assert_eq!(
        fake.jev_calls.load(Ordering::SeqCst),
        jev,
        "a judged pair is not re-asked"
    );
    assert_eq!(
        fake.embed_calls.load(Ordering::SeqCst),
        embed,
        "cached vectors are not re-embedded"
    );
}

#[test]
fn a_judge_step_without_its_credentials_is_skipped_and_a_bad_limit_is_a_usage_error() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = store(dir.path(), &["heist"]);
    let skipped = run(&path, None, &["judge-keyword-roles"]);
    assert_eq!(skipped.status.code(), Some(78));
    assert!(
        String::from_utf8_lossy(&skipped.stderr)
            .contains("TYPESAFE_API_KEY must be set in .env to judge keyword roles")
    );
    let usage = run(&path, None, &["judge-keyword-pairs", "--limit", "0"]);
    assert_eq!(usage.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&usage.stderr)
            .contains("argument --limit: must be at least 1, got 0")
    );
}
