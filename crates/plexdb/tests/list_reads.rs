//! What a person reads back about crowd lists (`plexdb lists`, `plexdb list`) and writes
//! about them (`plexdb list-decide`), against a store built by the real `plexdb migrate`.
//! Python's explorer reads and writes the same shapes until it moves, so the JSON and the
//! file are held to what it produces.

use std::path::Path;

use plexdb::list_file::{DecideError, ListFile, decide};
use plexdb::lists::{list_detail, list_table};
use plexdb::migrate::migrate;
use rusqlite::Connection;
use serde_json::{Value, json};

const URL: &str = "https://mdblist.com/lists/lina/top-watched";

fn store(dir: &Path) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    Connection::open(&path).expect("open the fixture store")
}

fn run(conn: &Connection, sql: &str) {
    conn.execute_batch(sql).expect("fixture sql");
}

/// Twenty movies; the list holds four of them (one only through its membership, as a
/// harvest tries every id an entry carries while the entry row keeps the strongest), and a
/// fifth entry the library does not own. Three of the four carry the keyword `heist`, which
/// three library titles carry in all.
fn fixture(conn: &Connection) {
    for n in 1..=20 {
        run(
            conn,
            &format!(
                "INSERT INTO items (item_id, type, title, year) VALUES ('m{n}', 'movie', 'Movie {n}', {});
                 INSERT INTO external_ids (item_id, ns, value, kind, last_seen)
                   VALUES ('m{n}', 'imdb', 'tt{n}', 'movie', '2026-01-01T00:00:00+00:00');",
                1990 + n
            ),
        );
    }
    for n in 1..=3 {
        run(
            conn,
            &format!(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at)
                 VALUES ('m{n}', 'keywords', 'tmdb', 'keyword', 'heist', '2026-01-01T00:00:00+00:00')"
            ),
        );
    }
    run(
        conn,
        &format!(
            "INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at, provenance)
               VALUES ('mdblist:14', 'mdblist', 'Top Watched', '{URL}', 48, 961, '2026-10-01T00:00:00+00:00', 'feed');
             INSERT INTO collection_entry (collection_id, rank, ns, value, kind, title, year) VALUES
               ('mdblist:14', 1, 'imdb', 'tt1', 'movie', 'One', 1991),
               ('mdblist:14', 2, 'imdb', 'tt2', 'movie', 'Two', 1992),
               ('mdblist:14', 3, 'tmdb', '303', 'movie', 'Three', 1993),
               ('mdblist:14', 4, 'imdb', 'tt4', 'movie', 'Four', 1994),
               ('mdblist:14', 5, 'imdb', 'tt99', 'movie', 'Unowned', 2001);
             INSERT INTO collection_membership (collection_id, item_id, rank, observed_at)
               VALUES ('mdblist:14', 'm3', 3, '2026-10-01T00:00:00+00:00');
             INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES ('h', 'm2', 1, 1);
             INSERT INTO list_decision (url, decision, decided_at, added)
               VALUES ('https://letterboxd.com/ann/list/best', 'added', '2026-10-02T00:00:00+00:00', 1);
             INSERT INTO list_attempt (list_key, attempts, attempted_at, reason)
               VALUES ('https://mdblist.com/lists/lina/gone', 3, '2026-10-03T00:00:00+00:00', 'MDBList returned 404');"
        ),
    );
}

#[test]
fn the_list_table_counts_what_is_owned_and_watched_and_lists_the_unfetched_last() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    fixture(&conn);
    let rows = list_table(&conn, None).expect("the table");
    let stored = Value::Object(rows[0].clone());
    assert_eq!(
        stored,
        json!({
            "collection_id": "mdblist:14", "source": "mdblist", "author": "lina",
            "name": "Top Watched", "url": URL, "provenance": "feed", "seed_count": null,
            "entry_count": 5, "owned": 4, "size": 48, "watched": 1, "likes": 961,
            "fetched_at": "2026-10-01T00:00:00+00:00",
            "decision": null, "failure": null, "state": "ok"
        })
    );
    // The two lists nothing is stored of: the failing one and the one a person added,
    // by URL in order, each with no collection_id.
    let rest: Vec<(&str, &str, &str)> = rows[1..]
        .iter()
        .map(|r| {
            (
                r["url"].as_str().expect("url"),
                r["source"].as_str().expect("source"),
                r["state"].as_str().expect("state"),
            )
        })
        .collect();
    assert_eq!(
        rest,
        [
            (
                "https://letterboxd.com/ann/list/best",
                "letterboxd",
                "waiting"
            ),
            ("https://mdblist.com/lists/lina/gone", "mdblist", "failed"),
        ]
    );
    assert_eq!(rows[2]["failure"]["stopped"], json!(true));
    assert_eq!(
        list_table(&conn, Some("letterboxd")).expect("table").len(),
        1
    );
}

#[test]
fn a_list_detail_names_every_entry_and_the_keywords_the_list_over_represents() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    fixture(&conn);
    let detail = list_detail(&conn, &format!("{URL}/"))
        .expect("detail")
        .expect("a list");
    let entries = detail["entries"].as_array().expect("entries");
    let shown: Vec<(i64, bool, &str)> = entries
        .iter()
        .map(|e| {
            (
                e["rank"].as_i64().expect("rank"),
                e["owned"].as_bool().expect("owned"),
                e["title"].as_str().expect("title"),
            )
        })
        .collect();
    // An owned entry carries the library's title; the third owns only through its membership.
    assert_eq!(
        shown,
        [
            (1, true, "Movie 1"),
            (2, true, "Movie 2"),
            (3, true, "Movie 3"),
            (4, true, "Movie 4"),
            (5, false, "Unowned")
        ]
    );
    assert_eq!(entries[1]["watched"], json!(true));
    assert_eq!(detail["keyword_kind"], json!("movie"));
    assert_eq!(detail["keyword_titles_in_list"], json!(4));
    assert_eq!(detail["keyword_titles_in_library"], json!(20));
    // 3 of the list's 4 titles carry it against 3 of the library's 20: x5.
    assert_eq!(
        detail["keywords"],
        json!([{"keyword": "heist", "in_list": 3, "in_library": 3, "lift": 5.0}])
    );
    assert!(
        list_detail(&conn, "mdblist:nope")
            .expect("detail")
            .is_none()
    );
}

fn decisions_file(dir: &Path) -> ListFile {
    ListFile::new(dir.join("list_decisions.json"))
}

#[test]
fn a_decision_replaces_the_lists_earlier_entry_and_is_written_as_python_writes_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    fixture(&conn);
    let file = decisions_file(dir.path());
    decide(
        &conn,
        &file,
        "https://www.mdblist.com/lists/Lina/Top-Watched/",
        "excluded",
    )
    .expect("exclude");
    let entry = decide(&conn, &file, URL, "refetch").expect("refetch");
    assert_eq!(entry["url"], json!(URL));
    let written: Value =
        serde_json::from_str(&std::fs::read_to_string(file.path()).expect("the file"))
            .expect("json");
    let entries = written.as_array().expect("a list");
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0]["decision"], json!("refetch"));
    let keys: Vec<&String> = entries[0].as_object().expect("an object").keys().collect();
    assert_eq!(keys, ["url", "decision", "decided_at"]);
    let text = std::fs::read_to_string(file.path()).expect("the file");
    assert!(
        text.starts_with("[\n  {\n    \"url\": "),
        "indented like json.dumps(indent=2): {text}"
    );
    assert!(text.ends_with("}\n]\n"));
}

#[test]
fn a_list_nothing_knows_can_be_added_or_excluded_but_not_retried_removed_or_refetched() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    let file = decisions_file(dir.path());
    let other = "https://mdblist.com/lists/someone/else";
    for verdict in ["refetch", "retry", "removed"] {
        assert!(matches!(
            decide(&conn, &file, other, verdict),
            Err(DecideError::Unknown(_))
        ));
    }
    decide(&conn, &file, other, "excluded").expect("exclude");
    decide(&conn, &file, other, "refetch").expect("the file now knows it");
    assert!(matches!(
        decide(&conn, &file, "https://example.com/x", "added"),
        Err(DecideError::Invalid(_))
    ));
    assert!(matches!(
        decide(&conn, &file, other, "deleted"),
        Err(DecideError::Invalid(_))
    ));
}

#[test]
fn a_refetch_cannot_replace_an_add_the_sweep_has_not_folded() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    let file = decisions_file(dir.path());
    let new = "https://mdblist.com/lists/pierce/new";
    decide(&conn, &file, new, "added").expect("add");
    let err = decide(&conn, &file, new, "refetch").expect_err("refused");
    assert!(
        err.to_string().contains("no sweep has folded the add"),
        "{err}"
    );
    let latest = file.latest().expect("latest");
    assert_eq!(latest[new].0, "added");
}
