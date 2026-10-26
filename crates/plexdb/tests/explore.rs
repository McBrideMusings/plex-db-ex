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
use serde_json::Value;

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
