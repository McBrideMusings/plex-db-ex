//! `plexdb-rs explore`'s store routes against a store built by the real `plexdb migrate`:
//! each answers exactly what `Reader::answer` gives for the call `Call::parse` reads from
//! the same path, which is what `plexdb-read` prints; every list walks to its end through
//! `next`; and a request the API cannot read is refused with a status naming why. The Frame
//! index routes are tested beside the Search tab, in `explore_search.rs`.

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
             ('Heist', 'heist'), ('heists', 'heist'), ('Los Angeles', 'los angeles'),
             ('AC/DC', 'ac/dc'), ('100%', '100%'), ('What?', 'what?');
         INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at,
                                    decision, decided_at) VALUES
             ('heist', 'heists', 0.97, 'jev', '{T}', 'accepted', '{T}'),
             ('ac/dc', 'heist', 0.02, 'jev', '{T}', NULL, NULL),
             ('heist', 'los angeles', 0.10, 'jev', '{T}', 'rejected', '{T}');
         INSERT INTO keyword_roles (keyword, role, source, score, model, stated_at) VALUES
             ('heist', 'theme', 'jev', 0.9, 'jev', '{T}'), ('heist', 'tone', 'jev', 0.2, 'jev', '{T}');
         INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) VALUES
             ('heist', 'theme', 'accepted', '{T}');
         INSERT INTO keyword_role_statements (item_id, keyword, role, source, stated_at) VALUES
             ('imdb:tt1', 'los angeles', 'region', 'anilist', '{T}');
         INSERT INTO external_ids (item_id, ns, value, kind, last_seen) VALUES
             ('imdb:tt1', 'imdb', 'tt1', 'movie', '{T}'), ('imdb:tt1', 'tmdb', '949', 'movie', '{T}');
         INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) VALUES
             ('rk1', 'imdb:tt1', '1', '{T}'), ('rk2', 'imdb:tt2', '1', '{T}'),
             ('rk3', 'imdb:tt3', '2', '{T}');
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
             ('p4', 'imdb:tt2', 7, 1700000300);
         UPDATE plays SET ip = '10.0.0.9', client_identifier = 'tv-1' WHERE history_key = 'p1';"
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

/// The call `Call::parse` reads from `target`, a GET path with its query.
fn call(target: &str) -> Call {
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    Call::parse("GET", path, query, None).unwrap_or_else(|e| panic!("{target}: {e}"))
}

/// The route answers 200 with exactly the bytes `Reader::answer` gives for its call.
fn same(explorer: &Explorer, reader: &Reader, target: &str) -> Value {
    let (status, body) = explorer.get(target);
    assert_eq!(status, 200, "{target}: {body}");
    assert_eq!(
        body,
        reader.answer(&call(target)).expect("the reader answers"),
        "{target}"
    );
    serde_json::from_str(&body).expect("JSON")
}

/// Walk a list from its first page through `next` to the end, `limit` rows a page; every
/// page but the last is full and says more follow.
fn walk(explorer: &Explorer, reader: &Reader, route: &str, limit: usize) -> Vec<Value> {
    let mut rows = Vec::new();
    let mut after: Option<String> = None;
    loop {
        let sep = if route.contains('?') { '&' } else { '?' };
        let mut target = format!("{route}{sep}limit={limit}");
        if let Some(after) = &after {
            target.push_str("&after=");
            target.push_str(&urlencode(after));
        }
        let page = same(explorer, reader, &target);
        let data = page["data"].as_array().expect("data").clone();
        let has_more = page["has_more"].as_bool().expect("has_more");
        match page["next"].as_str() {
            Some(next) => {
                assert!(has_more, "{target}: next without has_more");
                assert_eq!(
                    data.len(),
                    limit,
                    "{target}: a page before the last is full"
                );
                after = Some(next.to_string());
                rows.extend(data);
            }
            None => {
                assert!(!has_more, "{target}: has_more without next");
                rows.extend(data);
                return rows;
            }
        }
    }
}

fn urlencode(text: &str) -> String {
    text.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b':' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn s(text: &str) -> String {
    text.to_string()
}

fn ids(rows: &[Value], field: &str) -> Vec<String> {
    rows.iter()
        .map(|row| row[field].as_str().expect("a text field").to_string())
        .collect()
}

#[test]
fn every_store_route_answers_what_the_reader_answers() {
    let dir = tempfile::tempdir().expect("tempdir");
    world(dir.path());
    let explorer = Explorer::start(dir.path());
    let r = reader(dir.path());

    // a title, and a title with every include
    let got = same(&explorer, &r, "/items/imdb:tt1");
    assert_eq!(got["title"], "Heat");
    assert_eq!(got["type"], "movie");
    assert!(got.get("enrichment").is_none(), "{got}");
    let got = same(
        &explorer,
        &r,
        "/items/imdb:tt1?include=enrichment,external_ids,audio_facts,plex&namespace=keywords",
    );
    assert_eq!(got["enrichment"].as_array().map(Vec::len), Some(2));
    assert_eq!(got["external_ids"][1]["value"], "949");
    assert_eq!(got["audio_facts"], json!({"bpm": 92, "key": "A minor"}));
    assert_eq!(
        got["plex"],
        json!([{"rating_key": "rk1", "section_id": "1", "last_seen": T}])
    );
    let got = same(
        &explorer,
        &r,
        "/items/imdb:tt3?include=audio_facts,enrichment",
    );
    assert_eq!(got["audio_facts"], Value::Null);
    assert_eq!(got["enrichment"], json!([]));

    // enrichment: one (key, value) per pair, newest fetched_at; every namespace without one
    let got = same(
        &explorer,
        &r,
        "/items/imdb:tt1/enrichment?namespace=keywords",
    );
    assert_eq!(
        got,
        json!([
            {"namespace": "keywords", "key": "keyword", "value": "heist", "fetched_at": "2026-02-01T00:00:00+00:00"},
            {"namespace": "keywords", "key": "keyword", "value": "los angeles", "fetched_at": T},
        ])
    );
    assert_eq!(same(&explorer, &r, "/items/imdb:tt1/enrichment"), got);
    assert_eq!(same(&explorer, &r, "/items/imdb:tt3/enrichment"), json!([]));

    // edges: out by default, in on request, every type without one
    let got = same(&explorer, &r, "/items/imdb:tt1/edges?type=tmdb_similar");
    assert_eq!(
        ids(got.as_array().expect("list"), "to_id"),
        ["imdb:tt2", "imdb:tt3"]
    );
    assert_eq!(same(&explorer, &r, "/items/imdb:tt1/edges"), got);
    let got = same(&explorer, &r, "/items/imdb:tt2/edges?direction=in");
    assert_eq!(
        ids(got.as_array().expect("list"), "from_id"),
        ["imdb:tt1", "imdb:tt3"]
    );

    // what else is filed under a title
    let got = same(&explorer, &r, "/items/imdb:tt1/collections");
    assert_eq!(got[0]["name"], "Heist films");
    assert_eq!(got[0]["mentions"], Value::Null);
    assert_eq!(
        same(&explorer, &r, "/items/imdb:tt1/audio-facts"),
        json!({"bpm": 92, "key": "A minor"})
    );
    assert_eq!(
        same(&explorer, &r, "/items/imdb:tt2/audio-facts"),
        Value::Null
    );
    let got = same(&explorer, &r, "/items/imdb:tt1/external-ids");
    assert_eq!(got.as_array().map(Vec::len), Some(2));
    assert_eq!(
        same(&explorer, &r, "/items/imdb:tt1/plex")[0]["section_id"],
        "1"
    );
    let got = same(&explorer, &r, "/items/imdb:tt1/media-parts");
    assert_eq!(got[0]["file"], "/m/heat.mkv");
    let got = same(&explorer, &r, "/items/imdb:tt1/plays");
    assert_eq!(ids(got.as_array().expect("list"), "history_key"), ["p1"]);
    assert!(
        got[0].get("ip").is_none() && got[0].get("client_identifier").is_none(),
        "{got}"
    );
    let got = same(&explorer, &r, "/items/imdb:tt1/role-statements");
    assert_eq!(
        got,
        json!([{"keyword": "los angeles", "role": "region", "source": "anilist", "stated_at": T}])
    );

    // the house and its accounts
    assert_eq!(
        same(&explorer, &r, "/accounts"),
        json!([
            {"account_id": 1, "plays": 3, "last_viewed_at": 1700000200},
            {"account_id": 7, "plays": 1, "last_viewed_at": 1700000300},
        ])
    );
    let got = same(&explorer, &r, "/accounts/1/taste");
    assert!(
        got["attributes"]
            .as_array()
            .is_some_and(|a| a.iter().any(|x| x["value"] == "police")),
        "{got}"
    );
    let got = same(&explorer, &r, "/taste");
    assert!(
        got["attributes"]
            .as_array()
            .is_some_and(|a| a.iter().any(|x| x["value"] == "car chase")),
        "{got}"
    );
    assert_eq!(
        same(&explorer, &r, "/accounts/1/watched")["data"],
        json!(["imdb:tt1", "tvdb:9"])
    );
    assert_eq!(
        same(&explorer, &r, "/watched")["data"],
        json!(["imdb:tt1", "imdb:tt2", "tvdb:9"])
    );

    // keywords: one with its spellings and name, a spelling looked up, pairs and roles
    assert_eq!(
        same(&explorer, &r, "/keywords/heist"),
        json!({"keyword": "heist", "name": "Heist films", "surfaces": ["Heist", "heists"]})
    );
    let got = same(&explorer, &r, "/keywords?surface=HEISTS");
    assert_eq!(
        ids(got["data"].as_array().expect("data"), "keyword"),
        ["heist"]
    );
    assert_eq!(
        same(&explorer, &r, "/keywords?surface=nothing+like+it")["data"],
        json!([])
    );
    let got = same(&explorer, &r, "/keywords/heist/pairs");
    assert_eq!(
        ids(got.as_array().expect("list"), "keyword_b"),
        ["heist", "heists", "los angeles"]
    );
    let got = same(&explorer, &r, "/keywords/heist/roles");
    assert_eq!(got["verdicts"].as_array().map(Vec::len), Some(2));
    assert_eq!(
        got["decisions"],
        json!([{"role": "theme", "decision": "accepted", "decided_at": T}])
    );
    // a keyword holding `/`, `%` or `?` is one encoded path segment
    assert_eq!(
        same(&explorer, &r, "/keywords/ac%2Fdc")["surfaces"],
        json!(["AC/DC"])
    );
    assert_eq!(same(&explorer, &r, "/keywords/100%25")["keyword"], "100%");
    assert_eq!(same(&explorer, &r, "/keywords/what%3F/pairs"), json!([]));
    assert_eq!(
        same(&explorer, &r, "/keywords/ac%2Fdc/pairs")[0]["keyword_b"],
        "heist"
    );
    let got = same(&explorer, &r, "/keyword-pairs?decision=undecided");
    assert_eq!(
        ids(got["data"].as_array().expect("data"), "keyword_a"),
        ["ac/dc"]
    );

    // the tag network seen from one tag
    assert_eq!(
        same(&explorer, &r, "/tags/heist/neighbours?kind=movie"),
        json!([{"tag": "car chase", "shared": 1, "df": 1}, {"tag": "los angeles", "shared": 1, "df": 1}])
    );

    // with no Frame index configured, the explorer says it cannot search
    assert_eq!(
        same(&explorer, &r, "/capabilities"),
        json!({"search": false})
    );
    let (status, body) = explorer.get("/items/imdb:tt1/cuts");
    assert_eq!(status, 503, "{body}");

    // one log line per request: method, path and query as sent, status, size or reason
    let log = explorer.stop();
    assert!(
        log.contains(
            "explore: store GET /items/imdb:tt1/enrichment?namespace=keywords status=200 ms="
        ),
        "{log}"
    );
    assert!(log.contains(" bytes="), "{log}");
    assert!(
        log.contains("explore: store GET /items/imdb:tt1/cuts status=503 ms="),
        "{log}"
    );
}

#[test]
fn every_list_walks_to_its_end_through_next() {
    let dir = tempfile::tempdir().expect("tempdir");
    world(dir.path());
    let explorer = Explorer::start(dir.path());
    let r = reader(dir.path());

    let every_item = [
        "imdb:tt1",
        "imdb:tt2",
        "imdb:tt3",
        "tvdb:9",
        "tvdb:9-1-1",
        "tvdb:9-1-2",
    ];
    for limit in [1, 2, 4, 6, 7] {
        assert_eq!(
            ids(&walk(&explorer, &r, "/items", limit), "item_id"),
            every_item
        );
    }
    // filters, and the whole-pool read with its facts
    let movies = walk(
        &explorer,
        &r,
        "/items?type=movie&include=enrichment&namespace=keywords",
        2,
    );
    assert_eq!(
        ids(&movies, "item_id"),
        ["imdb:tt1", "imdb:tt2", "imdb:tt3"]
    );
    let facts: Vec<(String, String)> = movies
        .iter()
        .flat_map(|m| {
            let id = m["item_id"].as_str().expect("id").to_string();
            m["enrichment"]
                .as_array()
                .expect("enrichment")
                .iter()
                .map(move |f| (id.clone(), f["value"].as_str().expect("value").to_string()))
                .collect::<Vec<_>>()
        })
        .collect();
    assert_eq!(
        facts,
        [
            (s("imdb:tt1"), s("heist")),
            (s("imdb:tt1"), s("los angeles")),
            (s("imdb:tt2"), s("car chase")),
            (s("imdb:tt2"), s("heist")),
        ]
    );
    assert_eq!(
        ids(&walk(&explorer, &r, "/items?library=1", 1), "item_id"),
        ["imdb:tt1", "imdb:tt2"]
    );
    assert_eq!(
        ids(&walk(&explorer, &r, "/items?show=tvdb:9", 1), "item_id"),
        ["tvdb:9-1-1", "tvdb:9-1-2"]
    );
    assert_eq!(
        walk(&explorer, &r, "/items?type=season", 3),
        Vec::<Value>::new()
    );

    assert_eq!(
        ids(&walk(&explorer, &r, "/plays", 1), "history_key"),
        ["p1", "p2", "p3", "p4"]
    );
    assert_eq!(
        ids(
            &walk(&explorer, &r, "/plays?account=1&since=1700000100", 1),
            "history_key"
        ),
        ["p2", "p3"]
    );
    assert_eq!(
        walk(&explorer, &r, "/watched", 1),
        [json!("imdb:tt1"), json!("imdb:tt2"), json!("tvdb:9")]
    );
    assert_eq!(
        walk(&explorer, &r, "/accounts/7/watched", 1),
        [json!("imdb:tt2")]
    );
    assert_eq!(
        ids(&walk(&explorer, &r, "/keywords", 2), "keyword"),
        ["100%", "ac/dc", "heist", "los angeles", "what?"]
    );
    let pairs = walk(&explorer, &r, "/keyword-pairs", 1);
    assert_eq!(ids(&pairs, "keyword_a"), ["ac/dc", "heist", "heist"]);
    assert_eq!(ids(&pairs, "keyword_b"), ["heist", "heists", "los angeles"]);
    assert_eq!(
        ids(
            &walk(&explorer, &r, "/keyword-pairs?decision=accepted", 1),
            "keyword_b"
        ),
        ["heists"]
    );
    // an `after` that names no row still starts after it: the cursor is a key, not a row
    let got = same(&explorer, &r, "/items?after=imdb:tt15");
    assert_eq!(
        ids(got["data"].as_array().expect("data"), "item_id")[0],
        "imdb:tt2"
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
    let get = |target: &str| error(explorer.get(target));

    // what the store does not hold is a 404
    assert_eq!(
        get("/items/imdb:tt9"),
        (404, s("the store holds no item imdb:tt9"))
    );
    assert_eq!(
        get("/items/imdb:tt9/edges"),
        (404, s("the store holds no item imdb:tt9"))
    );
    assert_eq!(
        get("/accounts/99/taste"),
        (404, s("the store has no plays by account 99"))
    );
    assert_eq!(get("/accounts/99/watched").0, 404);
    assert_eq!(get("/keywords/nothing").0, 404);
    assert_eq!(get("/keywords/nothing/roles").0, 404);
    assert_eq!(get("/tags/nothing/neighbours?kind=movie").0, 404);
    assert_eq!(
        get("/items/imdb:tt1/nothing"),
        (404, s("no store route answers /items/imdb:tt1/nothing"))
    );
    // the RPC routes are gone
    assert_eq!(
        get("/api/store/enrichment?item_id=imdb:tt1&namespace=keywords").0,
        404
    );
    assert_eq!(
        error(explorer.post(
            "/api/store/enrichment",
            &json!({"item_ids": ["imdb:tt1"], "namespace": "keywords"})
        ))
        .0,
        404
    );

    // a request it cannot read is a 400 naming the parameter
    assert_eq!(
        get("/items?limit=0"),
        (
            400,
            s("limit must be a whole number from 1 to 1000, got \"0\"")
        )
    );
    assert_eq!(get("/items?limit=1001").0, 400);
    assert_eq!(get("/items?limit=ten").0, 400);
    assert_eq!(
        get("/items?include=posters"),
        (
            400,
            s("include takes enrichment, external_ids, audio_facts and plex, got \"posters\"")
        )
    );
    assert_eq!(
        get("/items?namespace=keywords"),
        (
            400,
            s("namespace narrows include=enrichment, which is not asked for")
        )
    );
    assert_eq!(
        get("/items/imdb:tt1/edges?edge_type=tmdb_similar"),
        (
            400,
            s("unknown parameter edge_type: this route takes type, direction")
        )
    );
    assert_eq!(get("/items/imdb:tt1/edges?direction=sideways").0, 400);
    assert_eq!(
        get("/items?type=movie&type=show"),
        (400, s("type is given twice"))
    );
    assert_eq!(get("/accounts/owner/taste").0, 400);
    assert_eq!(get("/plays?account=owner").0, 400);
    assert_eq!(get("/tags/heist/neighbours"), (400, s("kind is required")));
    assert_eq!(get("/keyword-pairs?decision=maybe").0, 400);
    assert_eq!(get("/keyword-pairs?after=heist").0, 400);

    // a read is a GET, a search a POST
    let (status, body) = error(explorer.post("/items", &json!({})));
    assert_eq!((status, body.as_str()), (405, "/items answers GET only"));
    assert_eq!(get("/searches"), (405, s("/searches answers POST only")));

    // search without a Frame index: the body is read first, then the missing index is a 503
    assert_eq!(
        error(explorer.post("/searches", &json!({"pool": ["imdb:tt1"]}))),
        (400, s("phrase must be text of at most 500 characters"))
    );
    assert_eq!(
        error(explorer.post(
            "/searches",
            &json!({"pool": "imdb:tt1", "phrase": "a heist"})
        )),
        (400, s("pool must be a list of at most 20000 item ids"))
    );
    assert_eq!(
        error(explorer.post(
            "/searches",
            &json!({"pool": ["imdb:tt1"], "phrase": "x", "seed": 1})
        ))
        .0,
        400
    );
    assert_eq!(
        error(explorer.post(
            "/searches",
            &json!({"pool": ["imdb:tt1"], "phrase": "a heist"})
        ))
        .0,
        503
    );
    let log = explorer.stop();
    assert!(
        log.contains("explore: store GET /items?limit=0 status=400 ms="),
        "{log}"
    );
    assert!(
        log.contains("error=\"limit must be a whole number"),
        "{log}"
    );
}

#[test]
fn a_store_route_is_503_until_a_store_exists() {
    let dir = tempfile::tempdir().expect("tempdir");
    let explorer = Explorer::start(dir.path());
    let (status, body) = explorer.get("/watched");
    assert_eq!(status, 503, "{body}");
    assert!(body.contains("no store at"), "{body}");
}
