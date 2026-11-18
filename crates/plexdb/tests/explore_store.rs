//! `plexdb-rs explore`'s `/api/store/*` routes against a store built by the real `plexdb
//! migrate`: each answers exactly what `Reader::answer` gives for the same call, which is what
//! `plexdb-read` prints, and refuses a request it cannot read with a 400 naming the field.
//! The Frame index routes are tested beside the Search tab, in `explore_search.rs`.

mod common;

use std::io::{BufRead, BufReader, Read};
use std::path::Path;
use std::process::{Child, ChildStderr, Command, Stdio};

use plexdb_reader::{Call, Reader};
use serde_json::{Value, json};

const T: &str = "2026-01-01T00:00:00+00:00";

/// Three films, a show with one season of two episodes, keywords, a person's display name, a
/// similar edge, a tag network, a crowd list, audio facts on one film, and plays by two
/// accounts.
fn world(dir: &Path) {
    let conn = common::store(dir);
    conn.execute_batch(&format!(
        "INSERT INTO items (item_id, type, title, year) VALUES
             ('imdb:tt1', 'movie', 'Heat', 1995),
             ('imdb:tt2', 'movie', 'Ronin', 1998),
             ('imdb:tt3', 'movie', 'Thief', 1981),
             ('tvdb:9', 'show', 'Crime Story', 1986);
         INSERT INTO items (item_id, type, title, show_item_id, season, episode) VALUES
             ('tvdb:9-1-1', 'episode', 'Pilot', 'tvdb:9', 1, 1),
             ('tvdb:9-1-2', 'episode', 'Two', 'tvdb:9', 1, 2);
         INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) VALUES
             ('imdb:tt1', 'keywords', 'tmdb', 'keyword', 'heist', '{T}'),
             ('imdb:tt1', 'keywords', 'mdblist', 'keyword', 'heist', '2026-02-01T00:00:00+00:00'),
             ('imdb:tt1', 'keywords', 'tmdb', 'keyword', 'los angeles', '{T}'),
             ('imdb:tt2', 'keywords', 'tmdb', 'keyword', 'heist', '{T}'),
             ('imdb:tt2', 'keywords', 'tmdb', 'keyword', 'car chase', '{T}'),
             ('tvdb:9', 'keywords', 'tmdb', 'keyword', 'police', '{T}');
         INSERT INTO keyword_forms (surface, keyword) VALUES
             ('Heist', 'heist'), ('heists', 'heist'), ('Los Angeles', 'los angeles');
         INSERT INTO keyword_names (keyword, name, decided_at) VALUES ('heist', 'Heist films', '{T}');
         INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) VALUES
             ('imdb:tt1', 'imdb:tt2', 'tmdb_similar', 1, '{T}'),
             ('imdb:tt1', 'imdb:tt3', 'tmdb_similar', 2, '{T}'),
             ('imdb:tt3', 'imdb:tt2', 'tmdb_similar', 1, '{T}');
         INSERT INTO tag_network (kind, value, df, x, y) VALUES
             ('movie', 'heist', 2, 0, 0), ('movie', 'car chase', 1, 0, 0),
             ('movie', 'los angeles', 1, 0, 0);
         INSERT INTO tag_network_edge (kind, a, b, shared) VALUES
             ('movie', 'car chase', 'heist', 1), ('movie', 'heist', 'los angeles', 1);
         INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) VALUES
             ('mdblist:7', 'mdblist', 'Heist films', 'https://example.test/7', 40, 12, '{T}');
         INSERT INTO collection_membership (collection_id, item_id, rank, mentions, observed_at) VALUES
             ('mdblist:7', 'imdb:tt1', 1, NULL, '{T}'), ('mdblist:7', 'imdb:tt2', 2, 3, '{T}');
         INSERT INTO media_part (part_id, item_id, rating_key, file, size, fetched_at) VALUES
             (101, 'imdb:tt1', 'rk1', '/m/heat.mkv', 10, 't');
         INSERT INTO audio_facts (file_hash, facts, analyzed_at) VALUES
             ('h-heat', '{{\"bpm\":92,\"key\":\"A minor\"}}', 't');
         INSERT INTO audio_part (part_id, item_id, file_hash) VALUES (101, 'imdb:tt1', 'h-heat');
         INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES
             ('p1', 'imdb:tt1', 1, 1700000000),
             ('p2', 'tvdb:9-1-1', 1, 1700000100),
             ('p3', 'tvdb:9-1-2', 1, 1700000200),
             ('p4', 'imdb:tt2', 7, 1700000300);"
    ))
    .expect("seed the world");
}

/// A running `plexdb-rs explore` with its stderr kept, stopped when it goes out of scope.
struct Explorer {
    child: Child,
    base: String,
    stderr: Option<ChildStderr>,
}

impl Explorer {
    fn start(dir: &Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
            .args(["explore", "--port", "0", "--store"])
            .arg(dir.join("plexdb.db"))
            .env_remove("PLEXDB_EXPLORE_SAVED_PATH")
            .env_remove("PLEXDB_FRAMES_DIR")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("start plexdb-rs explore");
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
        let stderr = child.stderr.take();
        Explorer {
            child,
            base,
            stderr,
        }
    }

    fn agent() -> ureq::Agent {
        ureq::Agent::config_builder()
            .http_status_as_error(false)
            .build()
            .into()
    }

    fn get(&self, route: &str) -> (u16, String) {
        let mut answer = Self::agent()
            .get(&format!("{}{route}", self.base))
            .call()
            .expect("the explorer answers");
        let status = answer.status().as_u16();
        (status, answer.body_mut().read_to_string().expect("a body"))
    }

    fn post(&self, route: &str, body: &Value) -> (u16, String) {
        let mut answer = Self::agent()
            .post(&format!("{}{route}", self.base))
            .header("Content-Type", "application/json")
            .send(body.to_string())
            .expect("the explorer answers");
        let status = answer.status().as_u16();
        (status, answer.body_mut().read_to_string().expect("a body"))
    }

    /// Stop the server and return everything it wrote to stderr.
    fn stop(mut self) -> String {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let mut text = String::new();
        if let Some(mut stderr) = self.stderr.take() {
            let _ = stderr.read_to_string(&mut text);
        }
        text
    }
}

impl Drop for Explorer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn reader(dir: &Path) -> Reader {
    Reader::open(dir.join("plexdb.db"), None, None).expect("open the store")
}

/// The route answers 200 with exactly the bytes `Reader::answer` gives for `call`.
fn same(reply: (u16, String), reader: &Reader, call: &Call) -> Value {
    let (status, body) = reply;
    assert_eq!(status, 200, "{call:?}: {body}");
    assert_eq!(
        body,
        reader.answer(call).expect("the reader answers"),
        "{call:?}"
    );
    serde_json::from_str(&body).expect("JSON")
}

fn s(text: &str) -> String {
    text.to_string()
}

#[test]
fn every_store_route_answers_what_the_reader_answers() {
    let dir = tempfile::tempdir().expect("tempdir");
    world(dir.path());
    let explorer = Explorer::start(dir.path());
    let r = reader(dir.path());

    // enrichment_for: one (key, value) per pair, newest fetched_at, (key, value) order
    let got = same(
        explorer.get("/api/store/enrichment?item_id=imdb:tt1&namespace=keywords"),
        &r,
        &Call::EnrichmentFor {
            item_id: s("imdb:tt1"),
            namespace: s("keywords"),
        },
    );
    assert_eq!(
        got,
        json!([
            {"namespace": "keywords", "key": "keyword", "value": "heist", "fetched_at": "2026-02-01T00:00:00+00:00"},
            {"namespace": "keywords", "key": "keyword", "value": "los angeles", "fetched_at": T},
        ])
    );
    // an id with nothing recorded is an empty list
    let got = same(
        explorer.get("/api/store/enrichment?item_id=imdb:tt9&namespace=keywords"),
        &r,
        &Call::EnrichmentFor {
            item_id: s("imdb:tt9"),
            namespace: s("keywords"),
        },
    );
    assert_eq!(got, json!([]));

    // enrichment_for_many: keyed by id, an id with nothing absent, a repeat counted once
    let got = same(
        explorer.post(
            "/api/store/enrichment",
            &json!({"item_ids": ["imdb:tt2", "imdb:tt1", "imdb:tt9", "imdb:tt2"], "namespace": "keywords"}),
        ),
        &r,
        &Call::EnrichmentForMany {
            item_ids: vec![s("imdb:tt2"), s("imdb:tt1"), s("imdb:tt9"), s("imdb:tt2")],
            namespace: s("keywords"),
        },
    );
    let keys: Vec<&String> = got.as_object().expect("object").keys().collect();
    assert_eq!(keys, ["imdb:tt1", "imdb:tt2"]);
    assert_eq!(got["imdb:tt2"].as_array().map(Vec::len), Some(2));

    // audio_facts_for: the analyzer's object as stored, null for a title never analyzed
    let got = same(
        explorer.get("/api/store/audio-facts?item_id=imdb:tt1"),
        &r,
        &Call::AudioFactsFor {
            item_id: s("imdb:tt1"),
        },
    );
    assert_eq!(got, json!({"bpm": 92, "key": "A minor"}));
    let got = same(
        explorer.get("/api/store/audio-facts?item_id=imdb:tt2"),
        &r,
        &Call::AudioFactsFor {
            item_id: s("imdb:tt2"),
        },
    );
    assert_eq!(got, Value::Null);
    let got = same(
        explorer.post(
            "/api/store/audio-facts",
            &json!({"item_ids": ["imdb:tt1", "imdb:tt2"]}),
        ),
        &r,
        &Call::AudioFactsForMany {
            item_ids: vec![s("imdb:tt1"), s("imdb:tt2")],
        },
    );
    assert_eq!(got, json!({"imdb:tt1": {"bpm": 92, "key": "A minor"}}));

    // keywords: a spelling to its stored keyword, a keyword to its spellings, a person's name
    let got = same(
        explorer.get("/api/store/keyword?surface=HEISTS"),
        &r,
        &Call::KeywordForSurface {
            surface: s("HEISTS"),
        },
    );
    assert_eq!(got, json!("heist"));
    let got = same(
        explorer.get("/api/store/keyword?surface=nothing+like+it"),
        &r,
        &Call::KeywordForSurface {
            surface: s("nothing like it"),
        },
    );
    assert_eq!(got, Value::Null);
    let got = same(
        explorer.get("/api/store/surfaces?keyword=heist"),
        &r,
        &Call::SurfacesForKeyword {
            keyword: s("heist"),
        },
    );
    assert_eq!(got, json!(["Heist", "heists"]));
    let got = same(
        explorer.get("/api/store/display-name?keyword=heist"),
        &r,
        &Call::DisplayName {
            keyword: s("heist"),
        },
    );
    assert_eq!(got, json!("Heist films"));
    let got = same(
        explorer.get("/api/store/display-name?keyword=police"),
        &r,
        &Call::DisplayName {
            keyword: s("police"),
        },
    );
    assert_eq!(got, Value::Null);

    // edges, both directions, ranked ascending
    let got = same(
        explorer.get("/api/store/edges/from?item_id=imdb:tt1&edge_type=tmdb_similar"),
        &r,
        &Call::EdgesFrom {
            item_id: s("imdb:tt1"),
            edge_type: s("tmdb_similar"),
        },
    );
    let to: Vec<&str> = got
        .as_array()
        .expect("list")
        .iter()
        .filter_map(|e| e["to_id"].as_str())
        .collect();
    assert_eq!(to, ["imdb:tt2", "imdb:tt3"]);
    let got = same(
        explorer.get("/api/store/edges/to?item_id=imdb:tt2&edge_type=tmdb_similar"),
        &r,
        &Call::EdgesTo {
            item_id: s("imdb:tt2"),
            edge_type: s("tmdb_similar"),
        },
    );
    assert_eq!(got.as_array().map(Vec::len), Some(2));

    // the tag network seen from one tag
    let got = same(
        explorer.get("/api/store/tag-neighbours?kind=movie&tag=heist"),
        &r,
        &Call::TagNeighbours {
            kind: s("movie"),
            tag: s("heist"),
        },
    );
    assert_eq!(
        got,
        json!([{"tag": "car chase", "shared": 1, "df": 1}, {"tag": "los angeles", "shared": 1, "df": 1}])
    );

    // crowd lists, a missing mentions kept as null
    let got = same(
        explorer.get("/api/store/collections?item_id=imdb:tt1"),
        &r,
        &Call::CollectionsFor {
            item_id: s("imdb:tt1"),
        },
    );
    assert_eq!(got[0]["name"], "Heist films");
    assert_eq!(got[0]["rank"], 1);
    assert_eq!(got[0]["mentions"], Value::Null);

    // watch history: one account's, and the house's
    let got = same(
        explorer.get("/api/store/watched?account=1"),
        &r,
        &Call::WatchedUnitsFor { account: 1 },
    );
    assert_eq!(got, json!(["imdb:tt1", "tvdb:9"]));
    let got = same(
        explorer.get("/api/store/watched?account=99"),
        &r,
        &Call::WatchedUnitsFor { account: 99 },
    );
    assert_eq!(got, json!([]));
    let got = same(
        explorer.get("/api/store/watched/all"),
        &r,
        &Call::WatchedUnits,
    );
    assert_eq!(got, json!(["imdb:tt1", "imdb:tt2", "tvdb:9"]));
    let got = same(
        explorer.get("/api/store/taste?account=1"),
        &r,
        &Call::TasteVectorFor { account: 1 },
    );
    assert!(
        got["attributes"]
            .as_array()
            .is_some_and(|a| a.iter().any(|x| x["value"] == "police")),
        "{got}"
    );
    let got = same(
        explorer.get("/api/store/taste/pooled"),
        &r,
        &Call::PooledTasteVector,
    );
    assert!(
        got["attributes"]
            .as_array()
            .is_some_and(|a| a.iter().any(|x| x["value"] == "car chase")),
        "{got}"
    );

    // with no Frame index configured, the explorer says it cannot search
    let got = same(
        explorer.get("/api/store/capabilities"),
        &r,
        &Call::Capabilities,
    );
    assert_eq!(got, json!({"search": false}));
    let (status, body) = explorer.get("/api/store/cuts?item_id=imdb:tt1");
    assert_eq!(status, 503, "{body}");

    // one log line per request, with raw values and the status
    let log = explorer.stop();
    assert!(
        log.contains(
            "explore: store enrichment item_id=imdb:tt1 namespace=keywords status=200 ms="
        ),
        "{log}"
    );
    assert!(
        log.contains("explore: store enrichment-many ids=4 namespace=keywords status=200"),
        "{log}"
    );
    assert!(
        log.contains("explore: store cuts item_id=imdb:tt1 status=503"),
        "{log}"
    );
}

#[test]
fn a_store_route_refuses_a_request_it_cannot_read() {
    let dir = tempfile::tempdir().expect("tempdir");
    world(dir.path());
    let explorer = Explorer::start(dir.path());

    let error = |(status, body): (u16, String)| -> (u16, String) {
        let body: Value = serde_json::from_str(&body).expect("JSON");
        (status, body["error"].as_str().unwrap_or("").to_string())
    };
    assert_eq!(
        error(explorer.get("/api/store/enrichment?item_id=imdb:tt1")),
        (400, s("namespace is required"))
    );
    assert_eq!(
        error(explorer.get("/api/store/edges/from?edge_type=tmdb_similar")),
        (400, s("item_id is required"))
    );
    assert_eq!(
        error(explorer.get("/api/store/taste?account=owner")),
        (400, s("account must be a Plex account id, a whole number"))
    );
    assert_eq!(
        error(explorer.post(
            "/api/store/enrichment",
            &json!({"item_ids": "imdb:tt1", "namespace": "keywords"})
        )),
        (400, s("item_ids must be a list of at most 20000 item ids"))
    );
    assert_eq!(
        error(explorer.post("/api/store/audio-facts", &json!({}))),
        (400, s("item_ids must be a list of at most 20000 item ids"))
    );
    let too_many: Vec<String> = (0..20_001).map(|i| format!("fs:{i}")).collect();
    assert_eq!(
        error(explorer.post("/api/store/audio-facts", &json!({"item_ids": too_many}))).0,
        400
    );
    // search without a Frame index: the body is read first, then the missing index is a 503
    assert_eq!(
        error(explorer.post("/api/store/search", &json!({"pool": ["imdb:tt1"]}))),
        (400, s("phrase must be text of at most 500 characters"))
    );
    assert_eq!(
        error(explorer.post(
            "/api/store/search",
            &json!({"pool": ["imdb:tt1"], "phrase": "a heist"})
        ))
        .0,
        503
    );
}

#[test]
fn a_store_route_is_503_until_a_store_exists() {
    let dir = tempfile::tempdir().expect("tempdir");
    let explorer = Explorer::start(dir.path());
    let (status, body) = explorer.get("/api/store/watched/all");
    assert_eq!(status, 503, "{body}");
    assert!(body.contains("no store at"), "{body}");
}
