//! `plexdb-rs explore` against a store built by the real `plexdb migrate`: the page and the
//! read-only keyword routes answer what `plexdb/explore.py` answers, a route not yet moved goes
//! to the Python explorer named by `--fallback`, and the network streams its stages.

mod common;

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Child, Command, Stdio};

use plexdb::refresh_map;
use plexdb::refresh_tagnetwork;
use plexdb::staleness::Now;
use rusqlite::Connection;
use serde_json::{Value, json};

fn add_title(conn: &Connection, id: &str, kind: &str, keywords: &[&str]) {
    conn.execute(
        "INSERT INTO items (item_id, type, title, year, studio) VALUES (?, ?, ?, 1999, 'Acme')",
        (id, kind, format!("Title {id}")),
    )
    .expect("item");
    for keyword in keywords {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES (?, 'keywords', 'tmdb', 'keyword', ?, '2026-01-01T00:00:00+00:00')",
            (id, keyword),
        )
        .expect("keyword");
    }
}

/// Sixty movies in three themes of twenty, and eight shows; the maps are drawn.
fn library(dir: &Path) {
    let mut conn = common::store(dir);
    for theme in 0..3 {
        for n in 0..20 {
            let own = format!("theme{theme}");
            let sub = format!("theme{theme}-sub{}", n % 4);
            let odd = format!("odd{}", n % 7);
            add_title(
                &conn,
                &format!("fs:{theme}-{n:02}"),
                "movie",
                &[&own, &sub, &odd, "common"],
            );
        }
    }
    for n in 0..8 {
        add_title(&conn, &format!("fs:show-{n}"), "show", &["drama", "serial"]);
    }
    refresh_map::refresh(&mut conn, &Now::read()).expect("map");
    refresh_tagnetwork::refresh(&mut conn, &Now::read()).expect("network");
}

/// A running `plexdb-rs explore`, stopped when it goes out of scope.
struct Explorer {
    child: Child,
    base: String,
}

impl Explorer {
    fn start(dir: &Path, fallback: Option<&str>) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"));
        command
            .args(["explore", "--port", "0", "--store"])
            .arg(dir.join("plexdb.db"))
            .env_remove("PLEXDB_EXPLORE_SAVED_PATH")
            .env_remove("PLEXDB_EXPLORE_FALLBACK")
            .stdout(Stdio::piped());
        if let Some(url) = fallback {
            command.args(["--fallback", url]);
        }
        let mut child = command.spawn().expect("start plexdb-rs explore");
        let mut line = String::new();
        BufReader::new(child.stdout.as_mut().expect("stdout"))
            .read_line(&mut line)
            .expect("the address line");
        let base = line
            .split("on ")
            .nth(1)
            .and_then(|rest| rest.split("/ reading").next())
            .unwrap_or_else(|| panic!("no address in {line:?}"))
            .to_string();
        Explorer { child, base }
    }

    fn get(&self, route: &str) -> (u16, String, String) {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .build()
            .into();
        let mut answer = agent
            .get(&format!("{}{route}", self.base))
            .call()
            .expect("the explorer answers");
        let status = answer.status().as_u16();
        let kind = answer
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        let body = answer.body_mut().read_to_string().expect("a body");
        (status, kind, body)
    }

    fn json(&self, route: &str) -> Value {
        let (status, _, body) = self.get(route);
        assert_eq!(status, 200, "{route}: {body}");
        serde_json::from_str(&body).expect("JSON")
    }
}

impl Drop for Explorer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn the_page_and_the_tag_table_answer_as_the_python_explorer_does() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let (status, kind, page) = explorer.get("/");
    assert_eq!((status, kind.as_str()), (200, "text/html; charset=utf-8"));
    assert!(page.contains("Plex DB-EX"));

    let tags = explorer.json("/api/tags?kind=movie");
    assert_eq!(tags["kind"], "movie");
    assert_eq!(tags["titles"], 60);
    let first = &tags["tags"][0];
    assert_eq!(
        (first["value"].as_str(), first["df"].as_i64()),
        (Some("common"), Some(60))
    );
    // 1 + ln(60 / 60), rounded to four places.
    assert_eq!(first["idf"].as_f64(), Some(1.0));
    // A tag on every title is no evidence of anything: "common" never ranks as related.
    let theme = tags["tags"]
        .as_array()
        .expect("tags")
        .iter()
        .find(|t| t["value"] == "theme0")
        .expect("theme0");
    assert_eq!(theme["df"], 20);
    let related = theme["related"].as_array().expect("related");
    assert!(!related.is_empty() && related.len() <= 5);
    assert!(related.iter().all(|r| r[0] != "common"));
    assert!(
        related[0][0]
            .as_str()
            .expect("tag")
            .starts_with("theme0-sub")
    );

    let (status, _, body) = explorer.get("/api/tags?kind=bogus");
    assert_eq!(status, 400);
    assert_eq!(
        body,
        r#"{"error":"kind must be one of movie, show, not 'bogus'"}"#
    );
}

#[test]
fn a_tags_titles_and_co_tags_come_from_the_folded_rows() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let titles = explorer.json("/api/titles?kind=movie&tag=theme1-sub2");
    let found = titles["titles"].as_array().expect("titles");
    assert_eq!(found.len(), 5);
    // Each title carries four keywords: its theme, sub-theme, odd tag and "common".
    assert!(found.iter().all(|t| t["keywords"] == 4));

    let co = explorer.json("/api/cotags?kind=movie&tag=theme1-sub2");
    assert_eq!(
        (co["df"].as_i64(), co["titles"].as_i64()),
        (Some(5), Some(60))
    );
    let names: Vec<&str> = co["co"]
        .as_array()
        .expect("co")
        .iter()
        .map(|c| c[0].as_str().expect("tag"))
        .collect();
    assert!(names.contains(&"theme1") && names.contains(&"common"));
    // [tag, shared titles, the tag's own df]
    let common = co["co"]
        .as_array()
        .expect("co")
        .iter()
        .find(|c| c[0] == "common")
        .expect("common");
    assert_eq!(
        (common[1].as_i64(), common[2].as_i64()),
        (Some(5), Some(60))
    );

    let (status, _, body) = explorer.get("/api/cotags?kind=movie");
    assert_eq!(
        (status, body.as_str()),
        (400, r#"{"error":"tag is required"}"#)
    );
}

#[test]
fn a_title_card_lists_its_keywords_and_nearest_titles() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let card = explorer.json("/api/title?item_id=fs:0-00");
    assert_eq!(card["title"], "Title fs:0-00");
    assert_eq!(
        (card["year"].as_i64(), card["studio"].as_str()),
        (Some(1999), Some("Acme"))
    );
    let keywords: Vec<&str> = card["keywords"]
        .as_array()
        .expect("keywords")
        .iter()
        .map(|k| k["value"].as_str().expect("value"))
        .collect();
    assert_eq!(keywords, ["common", "odd0", "theme0", "theme0-sub0"]);
    let similar = card["similar"].as_array().expect("similar");
    assert_eq!(similar.len(), 12);
    // The four other titles sharing its sub-theme come first, ahead of titles that share only
    // "common" and an odd tag.
    let scores: Vec<f64> = similar
        .iter()
        .map(|s| s["score"].as_f64().expect("score"))
        .collect();
    assert!(scores.windows(2).all(|w| w[0] >= w[1]), "{scores:?}");
    let nearest: Vec<&str> = similar[..4]
        .iter()
        .map(|s| s["item_id"].as_str().expect("id"))
        .collect();
    assert_eq!(nearest, ["fs:0-04", "fs:0-08", "fs:0-12", "fs:0-16"]);

    let (status, _, body) = explorer.get("/api/title?item_id=fs:nobody");
    assert_eq!(status, 404);
    assert_eq!(body, r#"{"error":"no title with item_id 'fs:nobody'"}"#);
}

#[test]
fn the_stored_map_is_served_and_an_excluded_one_is_drawn_live() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let stored = explorer.json("/api/map?kind=movie");
    assert_eq!(stored["stale"], false);
    assert_eq!(stored["points"].as_array().expect("points").len(), 60);
    let region = &stored["labels"][0];
    // [node, x, y, width, height, titles, text shown, keyword]
    assert_eq!(region.as_array().expect("label").len(), 8);
    assert_eq!(region[6], region[7]);

    let live = explorer.json("/api/map?kind=movie&exclude=common&exclude=theme0");
    assert_eq!(live["stale"], false);
    let ids = |map: &Value| -> Vec<String> {
        let mut ids: Vec<String> = map["points"]
            .as_array()
            .expect("points")
            .iter()
            .map(|p| p[0].as_str().expect("id").to_string())
            .collect();
        ids.sort();
        ids
    };
    assert_eq!(ids(&live), ids(&stored));
    assert!(
        live["labels"]
            .as_array()
            .expect("labels")
            .iter()
            .all(|l| l[7] != "common" && l[7] != "theme0"),
        "an excluded tag names no region"
    );

    // Excluding every tag of a title leaves it unplaced but still counted.
    let drained = explorer.json("/api/map?kind=show&exclude=drama&exclude=serial");
    assert_eq!(drained["unplaced"], 8);
    assert_eq!(drained["points"], serde_json::json!([]));
}

#[test]
fn the_network_streams_its_stages_and_the_stored_one_is_a_single_line() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);
    let lines = |route: &str| -> Vec<Value> {
        let (status, kind, body) = explorer.get(route);
        assert_eq!(
            (status, kind.as_str()),
            (200, "application/x-ndjson"),
            "{body}"
        );
        body.lines()
            .map(|l| serde_json::from_str(l).expect("a JSON line"))
            .collect()
    };

    let stored = lines("/api/tagnetwork?kind=movie");
    assert_eq!(stored.len(), 1);
    assert_eq!(stored[0]["stage"], "done");
    // Every movie tag is on at least three titles.
    assert_eq!(
        stored[0]["nodes"].as_array().expect("nodes").len(),
        3 + 12 + 7 + 1
    );
    assert!(stored[0]["stamp"].as_str().expect("stamp").contains(':'));

    let live = lines("/api/tagnetwork?kind=movie&exclude=common");
    let stages: Vec<&str> = live
        .iter()
        .map(|l| l["stage"].as_str().expect("stage"))
        .collect();
    assert_eq!(stages, ["vocab", "edges", "done"]);
    assert_eq!(
        live[0]["nodes"].as_array().expect("vocab").len(),
        3 + 12 + 7
    );
    assert_eq!(
        live[2]["nodes"].as_array().expect("nodes").len(),
        3 + 12 + 7
    );
    // [value, df]
    assert_eq!(live[0]["nodes"][0].as_array().expect("node").len(), 2);

    let (status, _, body) = explorer.get("/api/tagnetwork?kind=bogus");
    assert_eq!(status, 400, "{body}");
}

#[test]
fn a_route_not_yet_moved_goes_to_the_python_explorer() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let python = TcpListener::bind("127.0.0.1:0").expect("a fake Python explorer");
    let url = format!("http://{}", python.local_addr().expect("address"));
    let fake = std::thread::spawn(move || {
        let (mut stream, _) = python.accept().expect("a forwarded request");
        let mut seen = Vec::new();
        let mut buffer = [0u8; 4096];
        while !seen.windows(4).any(|w| w == b"\r\n\r\n") {
            let n = stream.read(&mut buffer).expect("read");
            seen.extend_from_slice(&buffer[..n]);
        }
        let body = r#"{"queries":[]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
             Cache-Control: no-cache\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .expect("answer");
        String::from_utf8_lossy(&seen).into_owned()
    });
    let explorer = Explorer::start(dir.path(), Some(&url));

    let (status, kind, body) = explorer.get("/api/saved?x=1");
    assert_eq!(
        (status, kind.as_str(), body.as_str()),
        (200, "application/json", r#"{"queries":[]}"#)
    );
    let request = fake.join().expect("the fake saw the request");
    assert!(
        request.starts_with("GET /api/saved?x=1 HTTP/1.1"),
        "{request}"
    );

    let alone = Explorer::start(dir.path(), None);
    let (status, _, body) = alone.get("/api/saved");
    assert_eq!(
        (status, body.as_str()),
        (404, r#"{"error":"no route /api/saved"}"#)
    );
}

impl Explorer {
    /// POST `body` as `content_type`; the status and the body text.
    fn post(&self, route: &str, content_type: &str, body: &str) -> (u16, String) {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .build()
            .into();
        let mut answer = agent
            .post(&format!("{}{route}", self.base))
            .header("Content-Type", content_type)
            .send(body.as_bytes())
            .expect("the explorer answers");
        let status = answer.status().as_u16();
        (status, answer.body_mut().read_to_string().expect("a body"))
    }

    fn post_json(&self, route: &str, body: &Value) -> (u16, Value) {
        let (status, text) = self.post(route, "application/json", &body.to_string());
        (status, serde_json::from_str(&text).expect("JSON"))
    }
}

fn read_file(dir: &Path, name: &str) -> Value {
    let text = std::fs::read_to_string(dir.join(name)).expect("the decisions file");
    assert!(text.ends_with("\n"), "json.dumps(indent=2) plus a newline");
    serde_json::from_str(&text).expect("JSON")
}

#[test]
fn a_name_shows_at_once_and_is_written_to_the_decisions_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let (status, reply) = explorer.post_json(
        "/api/names",
        &json!({"keyword": "theme0", "name": "  Theme   Zero "}),
    );
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["label"], "Theme Zero");
    assert_eq!(reply["name"]["name"], "Theme Zero");
    assert_eq!(reply["name"]["pending"], true);
    assert_eq!(reply["entry"]["name"], "Theme Zero");
    let file = read_file(dir.path(), "name_decisions.json");
    assert_eq!(file.as_array().map(Vec::len), Some(1));
    assert_eq!(file[0]["surface"], "theme0");
    assert_eq!(file[0]["decision"], "named");

    let card = explorer.json("/api/title?item_id=fs:0-00");
    let label = card["keywords"]
        .as_array()
        .expect("keywords")
        .iter()
        .find(|k| k["value"] == "theme0")
        .expect("theme0");
    assert_eq!(label["label"], "Theme Zero");

    // A blank name clears it, and a second decision replaces the first entry.
    let (status, reply) =
        explorer.post_json("/api/names", &json!({"keyword": "theme0", "name": "   "}));
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["label"], "theme0");
    let file = read_file(dir.path(), "name_decisions.json");
    assert_eq!(file.as_array().map(Vec::len), Some(1));
    assert_eq!(file[0]["decision"], "cleared");
    assert!(file[0].get("name").is_none());
}

#[test]
fn a_decision_post_refuses_what_it_cannot_record() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let (status, text) = explorer.post("/api/names", "text/plain", "{}");
    assert_eq!(status, 400);
    assert_eq!(text, r#"{"error":"Content-Type must be application/json"}"#);
    let (status, reply) = explorer.post_json("/api/names", &json!({"keyword": "theme0"}));
    assert_eq!(
        (status, reply["error"].as_str()),
        (
            400,
            Some("name is required; a null or blank name clears it")
        )
    );
    let (status, reply) =
        explorer.post_json("/api/names", &json!({"keyword": "no such", "name": "x"}));
    assert_eq!(
        (status, reply["error"].as_str()),
        (404, Some("no title carries the keyword 'no such'"))
    );
    let (status, reply) = explorer.post_json(
        "/api/merges",
        &json!({"keyword_a": "b", "keyword_b": "a", "decision": "accepted"}),
    );
    assert_eq!(
        (status, reply["error"].as_str()),
        (400, Some("keyword_a must sort before keyword_b"))
    );
    assert!(!dir.path().join("name_decisions.json").exists());
    assert!(!dir.path().join("merge_decisions.json").exists());
}

#[test]
fn a_merge_decision_lays_over_the_pair_until_the_sweep_folds_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    Connection::open(dir.path().join("plexdb.db"))
        .expect("store")
        .execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) \
             VALUES ('theme0', 'theme1', 0.7, 'm', '2026-01-01T00:00:00+00:00')",
            [],
        )
        .expect("pair");
    let explorer = Explorer::start(dir.path(), None);

    let before = explorer.json("/api/merges");
    assert_eq!(before["proposed"][0]["keyword_a"], "theme0");
    assert_eq!(before["proposed"][0]["bucket"], "proposed");

    let (status, reply) = explorer.post_json(
        "/api/merges",
        &json!({"keyword_a": "theme0", "keyword_b": "theme1", "decision": "accepted"}),
    );
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["pair"]["bucket"], "merged");
    assert_eq!(reply["pair"]["pending"], true);
    let after = explorer.json("/api/merges");
    assert_eq!(after["proposed"].as_array().map(Vec::len), Some(0));
    assert_eq!(after["decided"][0]["decision"], "accepted");
    let file = read_file(dir.path(), "merge_decisions.json");
    assert_eq!(file[0]["surface_a"], "theme0");
    assert_eq!(file[0]["surface_b"], "theme1");

    let (status, reply) = explorer.post_json(
        "/api/merges",
        &json!({"keyword_a": "nope1", "keyword_b": "nope2", "decision": "accepted"}),
    );
    assert_eq!(
        (status, reply["error"].as_str()),
        (404, Some("no keyword pair ('nope1', 'nope2') in the store"))
    );
}

#[test]
fn a_role_decision_and_a_list_decision_wait_for_the_sweep() {
    let dir = tempfile::tempdir().expect("tempdir");
    library(dir.path());
    Connection::open(dir.path().join("plexdb.db"))
        .expect("store")
        .execute(
            "INSERT INTO keyword_roles (keyword, role, source, score, model, stated_at) \
             VALUES ('theme0', 'tone', 'jev', 0.8, 'm', '2026-01-01T00:00:00+00:00')",
            [],
        )
        .expect("role");
    let explorer = Explorer::start(dir.path(), None);

    let listing = explorer.json("/api/roles");
    assert_eq!(listing["keywords"][0]["keyword"], "theme0");
    assert_eq!(
        listing["keywords"][0]["cells"]["tone"]["decision"],
        Value::Null
    );
    let (status, reply) = explorer.post_json(
        "/api/roles",
        &json!({"keyword": "theme0", "role": "tone", "decision": "rejected"}),
    );
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["cell"]["decision"], "rejected");
    assert_eq!(reply["cell"]["pending"], true);
    assert_eq!(reply["entry"]["surface"], "theme0");
    let listing = explorer.json("/api/roles");
    assert_eq!(listing["decided"][0]["role"], "tone");

    let url = "https://mdblist.com/lists/someone/new-list";
    let (status, reply) = explorer.post_json(
        "/api/list-decisions",
        &json!({"url": url, "decision": "added"}),
    );
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["entry"]["url"], url);
    let lists = explorer.json("/api/lists");
    let row = &lists["lists"][0];
    assert_eq!(
        (row["url"].as_str(), row["pending"].as_str()),
        (Some(url), Some("Waiting for sweep"))
    );
    let (status, reply) = explorer.post_json(
        "/api/list-decisions",
        &json!({"url": url, "decision": "refetch"}),
    );
    assert_eq!(status, 400, "{reply}");
    let (status, reply) = explorer.post_json(
        "/api/list-decisions",
        &json!({"url": "https://mdblist.com/lists/no/body", "decision": "retry"}),
    );
    assert_eq!(
        (status, reply["error"].as_str()),
        (
            404,
            Some("no stored list has the URL https://mdblist.com/lists/no/body")
        )
    );
}

fn extras_world(dir: &Path) {
    let conn = common::store(dir);
    conn.execute_batch(
        "INSERT INTO extra_library (name, root, type, template, declared_at) VALUES ('Music Videos', '/m', 'music-video', NULL, '2026-10-01T00:00:00+00:00');
         INSERT INTO extra_field VALUES ('Music Videos', 'artist', 'text', 1), ('Music Videos', 'year', 'year', 0);",
    )
    .expect("library");
    let add = |id: &str, organized: i64, seen: &str, missing: Option<&str>| {
        conn.execute(
            "INSERT INTO items (item_id, type, title, year) VALUES (?, 'extra', ?, 1999)",
            (id, format!("T {id}")),
        )
        .expect("item");
        conn.execute(
            "INSERT INTO extra_item (item_id, library, organized, source_url, first_seen, organized_at) \
             VALUES (?, 'Music Videos', ?, 'https://example.test/v', ?, ?)",
            (id, organized, seen, seen),
        )
        .expect("extra");
        conn.execute(
            "INSERT INTO extra_file (path, item_id, primary_file, duration_ms, first_seen, last_seen, missing_since) \
             VALUES (?, ?, 1, 180000, ?, ?, ?)",
            (format!("/m/{id}.mkv"), id, seen, seen, missing),
        )
        .expect("file");
    };
    add("fs:a", 1, "2026-10-02T00:00:00+00:00", None);
    add(
        "fs:b",
        1,
        "2026-10-03T00:00:00+00:00",
        Some("2026-10-04T00:00:00+00:00"),
    );
    add("fs:queued", 0, "2026-10-05T00:00:00+00:00", None);
    for (source, value) in [
        ("path", "Path Name"),
        ("scraper", "Scraped"),
        ("person", "Person"),
    ] {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES ('fs:a', 'extra', ?, 'artist', ?, '2026-10-02T00:00:00+00:00')",
            (source, value),
        )
        .expect("field");
    }
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
         VALUES ('fs:a', 'keywords', 'person', 'keyword', 'live', '2026-10-06T00:00:00+00:00')",
        [],
    )
    .expect("tag");
}

#[test]
fn extras_items_list_organized_extras_and_since_returns_only_later_changes() {
    let dir = tempfile::tempdir().expect("tempdir");
    extras_world(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let all = explorer.json("/api/extras/items");
    let ids: Vec<&str> = all["items"]
        .as_array()
        .expect("items")
        .iter()
        .map(|item| item["item_id"].as_str().expect("id"))
        .collect();
    assert_eq!(ids, ["fs:b", "fs:a"], "queued fs:queued never appears");
    assert_eq!(all["cursor"], "2026-10-06T00:00:00+00:00");
    let b = &all["items"][0];
    assert_eq!(b["missing"], true);
    assert_eq!(b["path"], "/m/fs:b.mkv");
    assert_eq!(b["duration_ms"], 180000);
    assert_eq!(b["year"], 1999);
    assert_eq!(b["library"], "Music Videos");
    assert_eq!(b["type"], "music-video");
    assert_eq!(b["source_url"], "https://example.test/v");

    let later = explorer.json("/api/extras/items?since=2026-10-04T00:00:00%2B00:00");
    let ids: Vec<&str> = later["items"]
        .as_array()
        .expect("items")
        .iter()
        .map(|item| item["item_id"].as_str().expect("id"))
        .collect();
    assert_eq!(ids, ["fs:a"], "fs:b changed at the cursor, not after it");
    let none = explorer.json("/api/extras/items?since=2026-10-06T00:00:00%2B00:00");
    assert_eq!(none["items"], json!([]));
    assert_eq!(none["cursor"], "2026-10-06T00:00:00+00:00");
    // A stamp in another offset is ordered by instant: 10:00-04:00 is 14:00Z, after 12:00Z.
    let conn = rusqlite::Connection::open(dir.path().join("plexdb.db")).expect("open");
    conn.execute(
        "UPDATE extra_item SET organized_at = '2026-10-07T10:00:00-04:00' WHERE item_id = 'fs:b'",
        [],
    )
    .expect("restamp");
    let mixed = explorer.json("/api/extras/items?since=2026-10-07T12%3A00%3A00%2B00%3A00");
    assert_eq!(mixed["items"][0]["item_id"], "fs:b");
    assert_eq!(mixed["cursor"], "2026-10-07T10:00:00-04:00");
    let (status, _, _) = explorer.get("/api/extras/items?since=yesterday");
    assert_eq!(status, 400);
}

#[test]
fn extras_fields_put_person_values_ahead_of_scraper_and_path_values() {
    let dir = tempfile::tempdir().expect("tempdir");
    extras_world(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let (status, reply) = explorer.post_json(
        "/api/extras/fields",
        &json!({"ids": ["fs:a", "fs:a", "fs:queued", "fs:nope"]}),
    );
    assert_eq!(status, 200, "{reply}");
    let items = reply["items"].as_object().expect("items");
    assert_eq!(items.keys().collect::<Vec<_>>(), ["fs:a"]);
    let fields = items["fs:a"]["fields"].as_array().expect("fields");
    assert_eq!(fields.len(), 2, "both declared fields, valued or not");
    let artist = fields
        .iter()
        .find(|f| f["name"] == "artist")
        .expect("artist");
    let sources: Vec<&str> = artist["values"]
        .as_array()
        .expect("values")
        .iter()
        .map(|v| v["source"].as_str().expect("source"))
        .collect();
    assert_eq!(sources, ["person", "scraper", "path"]);
    assert_eq!(artist["values"][0]["value"], "Person");
    assert_eq!(artist["required"], true);
    let year = fields.iter().find(|f| f["name"] == "year").expect("year");
    assert_eq!(year["values"], json!([]));
    assert_eq!(
        items["fs:a"]["tags"],
        json!([{"value": "live", "source": "person"}])
    );

    let (status, reply) = explorer.post_json("/api/extras/fields", &json!({"ids": "fs:a"}));
    assert_eq!(
        (status, reply["error"].as_str()),
        (400, Some("ids must be a list of item ids"))
    );
}

/// A library of real files so the writer's plan can say where a save would move them: one
/// queued extra holding a path-parsed title, another with nothing, and an organized extra with
/// a second file that was folded into it.
fn extras_ui_world(dir: &Path) -> std::path::PathBuf {
    let root = dir.join("lib");
    std::fs::create_dir_all(root.join("new")).expect("dirs");
    for name in ["new/queued.mp4", "new/blank.mp4", "kept.mkv", "copy.mp4"] {
        std::fs::write(root.join(name), name).expect("file");
    }
    let conn = common::store(dir);
    conn.execute(
        "INSERT INTO extra_library (name, root, type, template, declared_at) \
         VALUES ('Clips', ?, 'clip', '{artist}/{title}', '2026-10-01T00:00:00+00:00')",
        [root.to_str().expect("utf-8")],
    )
    .expect("library");
    conn.execute_batch(
        "INSERT INTO extra_field VALUES ('Clips', 'artist', 'text', 1), ('Clips', 'title', 'text', 1), \
         ('Clips', 'year', 'year', 0);",
    )
    .expect("fields");
    let add = |id: &str, organized: i64, file: &str, primary: i64, seen: &str| {
        let path = root.join(file).to_str().expect("utf-8").to_string();
        let held: i64 = conn
            .query_row("SELECT COUNT(*) FROM items WHERE item_id = ?", [id], |r| {
                r.get(0)
            })
            .expect("count");
        if held == 0 {
            conn.execute(
                "INSERT INTO items (item_id, type, title) VALUES (?, 'extra', ?)",
                (id, id),
            )
            .expect("item");
            conn.execute(
                "INSERT INTO extra_item (item_id, library, organized, first_seen) VALUES (?, 'Clips', ?, ?)",
                (id, organized, seen),
            )
            .expect("extra");
        }
        conn.execute(
            "INSERT INTO extra_file (path, item_id, primary_file, size, height, duration_ms, first_seen, last_seen) \
             VALUES (?, ?, ?, 10, 480, 60000, ?, ?)",
            (path, id, primary, seen, seen),
        )
        .expect("file");
    };
    add(
        "fs:queued",
        0,
        "new/queued.mp4",
        1,
        "2026-10-05T00:00:00+00:00",
    );
    add(
        "fs:blank",
        0,
        "new/blank.mp4",
        1,
        "2026-10-06T00:00:00+00:00",
    );
    add("fs:kept", 1, "kept.mkv", 1, "2026-10-02T00:00:00+00:00");
    add("fs:kept", 1, "copy.mp4", 0, "2026-10-03T00:00:00+00:00");
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
         VALUES ('fs:queued', 'extra', 'path', 'title', 'Proposed Title', '2026-10-05T00:00:00+00:00')",
        [],
    )
    .expect("proposal");
    root
}

#[test]
fn extras_queue_lists_unsaved_extras_with_proposals_and_what_saving_would_do() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = extras_ui_world(dir.path());
    let explorer = Explorer::start(dir.path(), None);

    let status = explorer.json("/api/extras/status");
    let clips = &status["libraries"][0];
    assert_eq!(
        (
            clips["name"].as_str(),
            clips["organized"].as_i64(),
            clips["queue"].as_i64(),
            clips["files"].as_i64()
        ),
        (Some("Clips"), Some(1), Some(2), Some(4))
    );
    assert_eq!(clips["last_scan"], "2026-10-06T00:00:00+00:00");

    let queue = explorer.json("/api/extras/queue?library=Clips");
    assert_eq!(queue["total"], 2);
    let ids: Vec<&str> = queue["items"]
        .as_array()
        .expect("items")
        .iter()
        .map(|i| i["item_id"].as_str().expect("id"))
        .collect();
    assert_eq!(ids, ["fs:queued", "fs:blank"], "oldest first");
    let queued = &queue["items"][0];
    let title = queued["fields"]
        .as_array()
        .expect("fields")
        .iter()
        .find(|f| f["name"] == "title")
        .expect("title field");
    assert_eq!(title["proposed"], json!(["Proposed Title"]));
    assert_eq!(title["proposed_by"], "path");
    assert_eq!(
        queued["plan"],
        json!({"status": "refused", "why": "required field artist is empty"})
    );
    assert_eq!(queued["saving"], Value::Null);
    assert_eq!(
        queued["file"]["path"],
        root.join("new/queued.mp4").to_str().expect("utf-8")
    );
    let (status, _, _) = explorer.get("/api/extras/queue?library=Nope");
    assert_eq!(status, 404);
}

#[test]
fn extras_save_records_decisions_only_for_a_complete_card_and_dry_run_records_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    extras_ui_world(dir.path());
    let explorer = Explorer::start(dir.path(), None);
    let decisions = dir.path().join("extra_decisions.json");

    let complete =
        json!({"item_id": "fs:queued", "fields": {"artist": "Daft Punk"}, "tags": ["electronic"]});
    let (status, reply) = explorer.post_json(
        "/api/extras/save",
        &json!({"saves": [complete], "dry_run": true}),
    );
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["saved"][0]["plan"]["status"], "move");
    assert_eq!(
        reply["saved"][0]["plan"]["to"],
        "Daft Punk/Proposed Title.mp4"
    );
    assert!(!decisions.exists(), "a dry run writes nothing");

    let blank = json!({"item_id": "fs:blank", "fields": {"artist": "Nobody"}});
    let (status, reply) =
        explorer.post_json("/api/extras/save", &json!({"saves": [complete, blank]}));
    assert_eq!(status, 400, "{reply}");
    assert_eq!(
        reply["error"],
        "fs:blank: required field title is empty; nothing was recorded"
    );
    assert!(!decisions.exists(), "one refused card records none of them");

    let (status, reply) = explorer.post_json("/api/extras/save", &json!({"saves": [complete]}));
    assert_eq!(status, 200, "{reply}");
    let written: Value =
        serde_json::from_str(&std::fs::read_to_string(&decisions).expect("file")).expect("json");
    let kinds: Vec<&str> = written
        .as_array()
        .expect("list")
        .iter()
        .map(|e| e["decision"].as_str().expect("decision"))
        .collect();
    assert_eq!(kinds, ["set", "tag", "organize"]);

    let queue = explorer.json("/api/extras/queue?library=Clips");
    let saving = &queue["items"][0]["saving"];
    assert_eq!(saving["to"]["to"], "Daft Punk/Proposed Title.mp4");

    let (status, reply) = explorer.post_json(
        "/api/extras/save",
        &json!({"saves": [{"item_id": "fs:kept", "fields": {"artist": "X"}}]}),
    );
    assert_eq!(
        (status, reply["error"].as_str()),
        (400, Some("fs:kept is already organized"))
    );
    let (status, reply) = explorer.post_json(
        "/api/extras/save",
        &json!({"saves": [{"item_id": "fs:blank", "fields": {"colour": "red"}}]}),
    );
    assert_eq!(
        (status, reply["error"].as_str()),
        (400, Some("Clips declares no field colour"))
    );
}

#[test]
fn extras_doctor_lists_files_and_delete_and_split_are_refused_on_the_primary() {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = extras_ui_world(dir.path());
    let explorer = Explorer::start(dir.path(), None);
    let decisions = dir.path().join("extra_decisions.json");

    let item = explorer.json("/api/extras/item?item_id=fs:kept");
    let files = item["files"].as_array().expect("files");
    assert_eq!(files.len(), 2);
    assert_eq!(
        (files[0]["primary"].as_bool(), files[1]["primary"].as_bool()),
        (Some(true), Some(false))
    );
    assert_eq!(files[0]["splittable"], false);
    let (status, _, _) = explorer.get("/api/extras/item?item_id=fs:nothing");
    assert_eq!(status, 404);

    let primary = root.join("kept.mkv");
    let copy = root.join("copy.mp4");
    for action in ["delete", "split"] {
        let (status, reply) = explorer.post_json(
            "/api/extras/file",
            &json!({"action": action, "item_id": "fs:kept", "path": primary.to_str().expect("utf-8")}),
        );
        assert_eq!(
            (status, reply["error"].as_str()),
            (400, Some("the primary file is neither deleted nor split")),
            "{action}"
        );
    }
    let (status, reply) = explorer.post_json(
        "/api/extras/file",
        &json!({"action": "split", "item_id": "fs:kept", "path": copy.to_str().expect("utf-8")}),
    );
    assert_eq!(
        status, 400,
        "an exact copy was not folded by the Frame index: {reply}"
    );
    assert!(!decisions.exists());

    let (status, reply) = explorer.post_json(
        "/api/extras/file",
        &json!({"action": "delete", "item_id": "fs:kept", "path": copy.to_str().expect("utf-8")}),
    );
    assert_eq!(status, 200, "{reply}");
    let written: Value =
        serde_json::from_str(&std::fs::read_to_string(&decisions).expect("file")).expect("json");
    assert_eq!(written[0]["decision"], "delete");
    let item = explorer.json("/api/extras/item?item_id=fs:kept");
    assert_eq!(item["files"][1]["pending"], "delete");
}

#[test]
fn extras_scan_touches_the_decisions_file_the_tick_watches() {
    let dir = tempfile::tempdir().expect("tempdir");
    extras_ui_world(dir.path());
    let explorer = Explorer::start(dir.path(), None);
    let decisions = dir.path().join("extra_decisions.json");
    assert!(!decisions.exists());
    let (status, reply) = explorer.post_json("/api/extras/scan", &json!({}));
    assert_eq!(status, 200, "{reply}");
    assert_eq!(
        std::fs::read_to_string(&decisions).expect("created"),
        "[]\n"
    );
}
