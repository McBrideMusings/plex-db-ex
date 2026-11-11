//! `plexdb-rs explore`'s Search tab (`/api/search/*`) against a store, a seeded `frames.db`,
//! a fake encoder, a fake Plex and a three-second `testsrc` video cut by the real ffmpeg:
//! what `plexdb/explore_search.py` answers (tests/test_explore_search.py).

mod common;

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use plexdb::frames::{migrate, open_frames};
use plexdb_schema::frames::FRAME_DIMS;
use serde_json::{Value, json};

const MODEL: &str = "test/clip";
const NOW: &str = "2026-01-02T03:04:05+00:00";

fn tokenizer_fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/search_tokenizer.json")
}

fn vec_json(degrees: f64) -> String {
    let angle = degrees.to_radians();
    let mut v = vec![0i64; FRAME_DIMS];
    v[0] = (127.0 * angle.cos()).round() as i64;
    v[1] = (127.0 * angle.sin()).round() as i64;
    format!(
        "[{}]",
        v.iter().map(i64::to_string).collect::<Vec<_>>().join(",")
    )
}

/// (item_id, shot in_ms, t_ms, angle from the phrase vector)
const FRAMES: [(&str, i64, i64, f64); 5] = [
    ("imdb:tt1", 0, 0, 0.0),
    ("imdb:tt1", 0, 2000, 30.0),
    ("imdb:tt1", 5000, 5000, 60.0),
    ("imdb:tt2", 0, 0, 90.0),
    ("imdb:tt2", 0, 2000, 10.0),
];

/// Heat, La Haine and Ronin in the store; Heat and La Haine in the Frame index with a Plex
/// part each; a "Heist films" collection.
fn world(dir: &Path) -> PathBuf {
    let conn = common::store(dir);
    for (id, title, year, key) in [
        ("imdb:tt1", "Heat", 1995, "101"),
        ("imdb:tt2", "La Haine", 1995, "102"),
        ("imdb:tt3", "Ronin", 1998, "103"),
    ] {
        conn.execute(
            "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, ?)",
            (id, title, year),
        )
        .expect("item");
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) VALUES (?, ?, '2', ?)",
            (key, id, NOW),
        )
        .expect("plex item");
    }
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, observed_at) \
         VALUES ('mdblist:1', 'mdblist', 'Heist films', ?)",
        [NOW],
    )
    .expect("collection");
    for (id, rank) in [("imdb:tt2", 1), ("imdb:tt1", 2)] {
        conn.execute(
            "INSERT INTO collection_membership (collection_id, item_id, rank, observed_at) \
             VALUES ('mdblist:1', ?, ?, ?)",
            (id, rank, NOW),
        )
        .expect("member");
    }
    let frames_dir = dir.join("frames");
    let frames_path = frames_dir.join("frames.db");
    migrate(&frames_path).expect("frames.db");
    let frames = open_frames(&frames_path).expect("open frames");
    for (id, part) in [("imdb:tt1", 11), ("imdb:tt2", 12)] {
        frames
            .execute(
                "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, \
                 quant, duration_ms, vectors, indexed_at) VALUES (?, ?, ?, 'v1', ?, 'int8', 10000, 3, ?)",
                (id, part, MODEL, FRAME_DIMS as i64, NOW),
            )
            .expect("state");
    }
    let mut starts: Vec<(String, Vec<i64>)> = Vec::new();
    for (id, shot_in, t_ms, degrees) in FRAMES {
        let at = match starts.iter().position(|(i, _)| i == id) {
            Some(at) => at,
            None => {
                starts.push((id.to_string(), Vec::new()));
                starts.len() - 1
            }
        };
        if !starts[at].1.contains(&shot_in) {
            starts[at].1.push(shot_in);
            frames
                .execute(
                    "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?, ?, ?, ?)",
                    (id, starts[at].1.len() as i64 - 1, shot_in, shot_in + 5000),
                )
                .expect("shot");
        }
        let shot_ix = starts[at].1.iter().position(|s| *s == shot_in).unwrap() as i64;
        frames
            .execute(
                "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?, ?, ?, vec_int8(?))",
                (id, t_ms, shot_ix, vec_json(degrees)),
            )
            .expect("vector");
    }
    frames_dir
}

/// A three-second 64×48 test pattern, the file Plex says part 11 is.
fn video(dir: &Path) -> PathBuf {
    let media = dir.join("media");
    std::fs::create_dir_all(&media).expect("media dir");
    let file = media.join("heat.mp4");
    let done = Command::new("ffmpeg")
        .args([
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
        ])
        .arg("testsrc=duration=3:size=64x48:rate=10")
        .args(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-y"])
        .arg(&file)
        .status()
        .expect("ffmpeg on PATH");
    assert!(done.success(), "ffmpeg made the test video");
    media
}

fn read_request(stream: &mut TcpStream) -> (String, Value) {
    let mut bytes = Vec::new();
    let mut chunk = [0u8; 4096];
    let (head_end, length) = loop {
        let n = stream.read(&mut chunk).expect("read");
        if n == 0 {
            return (String::new(), Value::Null);
        }
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

/// The encoder: every phrase embeds as the unit vector along axis 0, so a frame's phrase
/// similarity is the cosine of its angle. While `refuse` is set it answers 500.
fn encoder(refuse: Arc<AtomicBool>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let base = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let (_, body) = read_request(&mut stream);
            if refuse.load(Ordering::SeqCst) {
                reply(&mut stream, 500, &json!({"error": "no model loaded"}));
                continue;
            }
            let mut unit = vec![0.0f64; FRAME_DIMS];
            unit[0] = 1.0;
            let rows: Vec<Value> = body["input"]
                .as_array()
                .map(|texts| {
                    (0..texts.len())
                        .map(|index| json!({"index": index, "embedding": unit}))
                        .collect()
                })
                .unwrap_or_default();
            reply(&mut stream, 200, &json!({"data": rows}));
        }
    });
    base
}

/// Plex: one Movies section, and rating key 101 (Heat) whose part 11 is `/library/heat.mp4`.
fn plex() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let base = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let (path, _) = read_request(&mut stream);
            let body = if path.starts_with("/library/sections") {
                json!({"MediaContainer": {"Directory": [
                    {"key": "2", "type": "movie", "title": "Movies"}
                ]}})
            } else if path.starts_with("/library/metadata/101") {
                json!({"MediaContainer": {"Metadata": [{
                    "ratingKey": "101",
                    "Media": [{"Part": [{"id": 11, "file": "/library/heat.mp4"}]}]
                }]}})
            } else {
                json!({"MediaContainer": {"Metadata": []}})
            };
            reply(&mut stream, 200, &body);
        }
    });
    base
}

struct Explorer {
    child: Child,
    base: String,
    data: PathBuf,
}

impl Explorer {
    fn start(dir: &Path, frames_dir: &Path, media: &Path, encoder: &str, plex: &str) -> Self {
        let data = dir.join("explore-data");
        std::fs::create_dir_all(&data).expect("data dir");
        let mut child = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
            .args(["explore", "--port", "0", "--store"])
            .arg(dir.join("plexdb.db"))
            .env_remove("PLEXDB_EXPLORE_FALLBACK")
            .env_remove("PLEXDB_PHRASE_CACHE")
            .env(
                "PLEXDB_EXPLORE_SAVED_PATH",
                data.join("explore-queries.json"),
            )
            .env("PLEXDB_FRAMES_DIR", frames_dir)
            .env("PLEXDB_FRAME_ENCODER_URL", encoder)
            .env("PLEXDB_FRAME_MODEL", MODEL)
            .env("PLEXDB_FRAME_TOKENIZER", tokenizer_fixture())
            .env("PLEX_URL", plex)
            .env("PLEX_TOKEN", "t")
            .env("PLEXDB_MEDIA_DIR", media)
            .env("PLEX_SOURCE_ROOTS", "/library")
            .stdout(Stdio::piped())
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
        Explorer { child, base, data }
    }

    fn agent() -> ureq::Agent {
        ureq::Agent::config_builder()
            .http_status_as_error(false)
            .build()
            .into()
    }

    /// GET with optional `Range`: status, the headers asked for, the bytes.
    fn get_bytes(&self, route: &str, range: Option<&str>) -> (u16, Vec<(String, String)>, Vec<u8>) {
        let mut request = Self::agent().get(&format!("{}{route}", self.base));
        if let Some(range) = range {
            request = request.header("Range", range);
        }
        let mut answer = request.call().expect("the explorer answers");
        let status = answer.status().as_u16();
        let headers = [
            "content-type",
            "content-length",
            "accept-ranges",
            "content-range",
            "cache-control",
        ]
        .iter()
        .filter_map(|name| {
            answer
                .headers()
                .get(*name)
                .and_then(|v| v.to_str().ok())
                .map(|v| (name.to_string(), v.to_string()))
        })
        .collect();
        let mut body = Vec::new();
        answer
            .body_mut()
            .as_reader()
            .read_to_end(&mut body)
            .expect("a body");
        (status, headers, body)
    }

    fn get(&self, route: &str) -> (u16, Value) {
        let (status, _, body) = self.get_bytes(route, None);
        let text = String::from_utf8_lossy(&body).into_owned();
        (
            status,
            serde_json::from_str(&text).unwrap_or(Value::String(text)),
        )
    }

    fn post(&self, route: &str, body: &Value) -> (u16, Value) {
        let mut answer = Self::agent()
            .post(&format!("{}{route}", self.base))
            .header("Content-Type", "application/json")
            .send(body.to_string().as_bytes())
            .expect("the explorer answers");
        let status = answer.status().as_u16();
        let text = answer.body_mut().read_to_string().expect("a body");
        (
            status,
            serde_json::from_str(&text).unwrap_or(Value::String(text)),
        )
    }
}

impl Drop for Explorer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn header<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(n, _)| n == name)
        .map(|(_, v)| v.as_str())
}

#[test]
fn the_search_tab_answers_as_the_python_tab_does() {
    let dir = tempfile::tempdir().expect("tempdir");
    let frames_dir = world(dir.path());
    let media = video(dir.path());
    let refuse = Arc::new(AtomicBool::new(false));
    let encoder = encoder(refuse.clone());
    let plex = plex();
    let explorer = Explorer::start(dir.path(), &frames_dir, &media, &encoder, &plex);

    // status: everything in place
    let (status, out) = explorer.get("/api/search/status");
    assert_eq!(status, 200, "{out}");
    assert_eq!(out["ready"], true, "{out}");
    assert_eq!(out["problems"], json!({}));
    assert_eq!(
        (
            out["movies"].as_i64(),
            out["indexed"].as_i64(),
            out["limit"].as_i64()
        ),
        (Some(3), Some(2), Some(75))
    );
    assert_eq!(out["model"], MODEL);

    // tokenize: offsets are characters, a split word is two boxes, the 76th token is over
    let (status, out) = explorer.post(
        "/api/search/tokenize",
        &json!({"texts": ["sunny firefight é", "a ".repeat(76)]}),
    );
    assert_eq!(status, 200, "{out}");
    let first = &out["fields"][0];
    assert_eq!(first["count"], 4);
    let boxes: Vec<(String, i64, i64)> = first["tokens"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            (
                t["text"].as_str().unwrap().to_string(),
                t["start"].as_i64().unwrap(),
                t["end"].as_i64().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        boxes,
        vec![
            ("sunny".into(), 0, 5),
            ("fire".into(), 6, 10),
            ("fight".into(), 10, 15),
            ("é".into(), 16, 17)
        ]
    );
    let second = &out["fields"][1];
    assert_eq!(second["count"], 76);
    assert_eq!(second["tokens"][75]["over"], true);
    assert_eq!(second["tokens"][74]["over"], false);
    let (status, out) = explorer.post("/api/search/tokenize", &json!({"texts": "x"}));
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("texts must be a list of at most 21 strings"))
    );

    // the pool: states, unknown ids, a collection's members in rank order, suggestions
    let (status, out) = explorer.post(
        "/api/search/pool",
        &json!({"item_ids": ["imdb:tt1", "imdb:tt3", "imdb:nope", "imdb:tt1"]}),
    );
    assert_eq!(status, 200, "{out}");
    assert_eq!(out["indexed"], 1);
    assert_eq!(out["unknown"], json!(["imdb:nope"]));
    assert_eq!(
        out["titles"][0],
        json!({"item_id": "imdb:tt1", "title": "Heat", "year": 1995, "kind": "movie", "state": "indexed"})
    );
    assert_eq!(out["titles"][1]["state"], "none");
    let (status, out) = explorer.get("/api/search/collection?id=mdblist%3A1");
    assert_eq!(
        (status, out["item_ids"].clone()),
        (200, json!(["imdb:tt2", "imdb:tt1"]))
    );
    let (status, out) = explorer.get("/api/search/collection?id=nope");
    assert_eq!(
        (status, out["error"].as_str()),
        (404, Some("no collection 'nope'"))
    );
    let (status, out) = explorer.get("/api/search/suggest?q=heist");
    assert_eq!(status, 200, "{out}");
    assert_eq!(
        out["collections"][0],
        json!({"collection_id": "mdblist:1", "name": "Heist films", "members": 2})
    );
    let (status, out) = explorer.get("/api/search/suggest?q=h");
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("q must be at least 2 characters"))
    );
    let (status, out) = explorer.get("/api/search/indexed");
    assert_eq!(status, 200, "{out}");
    let names: Vec<&str> = out["titles"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["title"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["Heat", "La Haine"]);
    let (status, out) = explorer.post(
        "/api/search/queue",
        &json!({"item_ids": ["imdb:tt1", "imdb:tt3"]}),
    );
    assert_eq!(
        (status, out["queued"].as_i64(), out["waiting"].as_i64()),
        (200, Some(1), Some(1)),
        "{out}"
    );
    let (_, out) = explorer.post("/api/search/pool", &json!({"item_ids": ["imdb:tt3"]}));
    assert_eq!(out["titles"][0]["state"], "queued");

    // the Frame index panel names the section through Plex
    let (status, out) = explorer.get("/api/search/frames");
    assert_eq!(status, 200, "{out}");
    assert_eq!(
        out["sections"],
        json!([{"section_id": "2", "indexed": 2, "total": 3, "name": "Movies"}])
    );
    assert_eq!(out["names_error"], Value::Null);
    assert_eq!(out["queued"], 1);

    // a search: ranked by the cosine of each frame's angle, drawn with a seed
    let body = json!({"phrase": "sunny firefight", "pool": ["imdb:tt1", "imdb:tt2", "imdb:tt3"], "negatives": ["dog"], "weight": 0, "temperature": 0.5, "draw": {"n": 1, "seed": 3}});
    let (status, out) = explorer.post("/api/search/run", &body);
    assert_eq!(status, 200, "{out}");
    assert_eq!(
        (
            out["pool"].as_i64(),
            out["indexed"].as_i64(),
            out["total"].as_i64()
        ),
        (Some(3), Some(2), Some(3))
    );
    assert_eq!(
        out["searched"],
        json!({"phrase": "sunny firefight", "negatives": ["dog"]})
    );
    assert_eq!(
        (out["weight"].as_f64(), out["temperature"].as_f64()),
        (Some(0.0), Some(0.5))
    );
    assert_eq!(out["draw"], json!({"n": 1, "seed": 3, "picks": 1}));
    let top = &out["shots"][0];
    assert_eq!(
        (
            top["rank"].as_i64(),
            top["item_id"].as_str(),
            top["part_id"].as_i64()
        ),
        (Some(1), Some("imdb:tt1"), Some(11))
    );
    assert_eq!(
        (
            top["in_ms"].as_i64(),
            top["out_ms"].as_i64(),
            top["in"].as_str(),
            top["seconds"].as_f64()
        ),
        (Some(0), Some(5000), Some("0:00:00.0"), Some(5.0))
    );
    assert_eq!(top["thumbs"], json!([0, 2000]));
    assert_eq!(top["title"], "Heat");
    assert!(
        (top["phrase_sim"].as_f64().unwrap() - 1.0).abs() < 1e-6,
        "{top}"
    );
    assert_eq!(top["strongest_negative"]["phrase"], "dog");
    let drawn: Vec<i64> = out["shots"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|s| s["drawn"].as_i64())
        .collect();
    assert_eq!(drawn, vec![1]);
    assert_eq!(
        out["formula"],
        "score = phrase similarity − weight × strongest negative similarity"
    );
    // the refusals
    let (status, out) = explorer.post(
        "/api/search/run",
        &json!({"phrase": "--help", "pool": ["imdb:tt1"]}),
    );
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("a phrase or negative cannot start with --"))
    );
    let (status, out) = explorer.post("/api/search/run", &json!({"phrase": "x", "temperature": 0}));
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("temperature must be above 0"))
    );
    let (status, out) = explorer.post("/api/search/run", &json!({"phrase": "x", "draw": {"n": 0}}));
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("draw.n must be between 1 and 100"))
    );
    let (status, out) = explorer.post(
        "/api/search/run",
        &json!({"phrase": "x", "weight": "heavy"}),
    );
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("weight must be a number"))
    );

    // why: the best frame of Heat's first Shot is the library's highest
    let (status, out) = explorer.post(
        "/api/search/why",
        &json!({"phrase": "sunny firefight", "item_id": "imdb:tt1", "in_ms": 0}),
    );
    assert_eq!(status, 200, "{out}");
    assert_eq!(
        (out["frames"].as_i64(), out["titles"].as_i64()),
        (Some(5), Some(2))
    );
    assert_eq!(out["is_highest"], true);
    assert_eq!(out["best"]["t_ms"], 0);
    assert!((out["best"]["sim"].as_f64().unwrap() - 1.0).abs() < 1e-6);
    assert_eq!(out["series"].as_array().unwrap().len(), 3);
    assert_eq!(
        out["title"],
        json!({"item_id": "imdb:tt1", "title": "Heat", "year": 1995, "kind": "movie"})
    );
    assert_eq!(out["part_id"], 11);
    assert_eq!(out["same"].as_array().unwrap().len(), 1, "{out}");
    assert_eq!(out["same"][0]["t_ms"], 5000);
    assert_eq!(out["other"].as_array().unwrap().len(), 2);
    assert_eq!(out["other"][0]["item_id"], "imdb:tt2");
    assert_eq!(out["other"][0]["part_id"], 12);
    let (status, out) = explorer.post(
        "/api/search/why",
        &json!({"phrase": "sunny firefight", "item_id": "imdb:tt1", "in_ms": 7}),
    );
    assert_eq!(
        (status, out["error"].as_str()),
        (
            404,
            Some("imdb:tt1 has no Shot starting at 7 ms in the Frame index")
        )
    );
    let (status, out) = explorer.post(
        "/api/search/why",
        &json!({"phrase": "nobody searched this", "item_id": "imdb:tt1", "in_ms": 0}),
    );
    assert_eq!(
        (status, out["error"].as_str()),
        (
            503,
            Some("this phrase is not in the Phrase cache; search again")
        )
    );

    // a frame and a clip cut from the file Plex named, served with ranges, found in the cache
    let (status, headers, body) = explorer.get_bytes(
        "/api/search/frame?item_id=imdb%3Att1&part=11&t_ms=1000",
        None,
    );
    assert_eq!(status, 200, "{}", String::from_utf8_lossy(&body));
    assert_eq!(header(&headers, "content-type"), Some("image/jpeg"));
    assert_eq!(&body[..2], &[0xff, 0xd8]);
    assert_eq!(
        header(&headers, "cache-control"),
        Some("public, max-age=604800, immutable")
    );
    let frame_file = explorer.data.join("clips/frames/imdb_tt1-11-1000-224.jpg");
    assert!(frame_file.exists(), "the cut landed in the cache");
    let (status, headers, clip) = explorer.get_bytes(
        "/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=0&out_ms=2000",
        None,
    );
    assert_eq!(status, 200, "{}", String::from_utf8_lossy(&clip));
    assert_eq!(header(&headers, "content-type"), Some("video/mp4"));
    assert_eq!(header(&headers, "accept-ranges"), Some("bytes"));
    assert_eq!(
        header(&headers, "content-length"),
        Some(clip.len().to_string().as_str())
    );
    assert!(clip.len() > 100 && &clip[4..8] == b"ftyp", "an MP4");
    let clip_file = explorer.data.join("clips/imdb_tt1-11-0-2000-640.mp4");
    assert_eq!(std::fs::read(&clip_file).unwrap(), clip);
    let (status, headers, part) = explorer.get_bytes(
        "/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=0&out_ms=2000",
        Some("bytes=4-7"),
    );
    assert_eq!((status, part.as_slice()), (206, &b"ftyp"[..]));
    assert_eq!(
        header(&headers, "content-range"),
        Some(format!("bytes 4-7/{}", clip.len()).as_str())
    );
    let (status, _, tail) = explorer.get_bytes(
        "/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=0&out_ms=2000",
        Some("bytes=-10"),
    );
    assert_eq!((status, tail.as_slice()), (206, &clip[clip.len() - 10..]));
    let (status, headers, empty) = explorer.get_bytes(
        "/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=0&out_ms=2000",
        Some("bytes=-0"),
    );
    assert_eq!((status, empty.len()), (416, 0));
    assert_eq!(
        header(&headers, "content-range"),
        Some(format!("bytes */{}", clip.len()).as_str())
    );
    let (status, _, whole) = explorer.get_bytes(
        "/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=0&out_ms=2000",
        Some("bytes=-"),
    );
    assert_eq!((status, whole.len()), (200, clip.len()));
    // the refusals: a stale part, a title outside the index, a bad window, a bad number
    let (status, out) = explorer.get("/api/search/frame?item_id=imdb%3Att1&part=10&t_ms=1000");
    assert_eq!(
        (status, out["error"].as_str()),
        (
            404,
            Some("imdb:tt1 was re-indexed from another file; search again")
        )
    );
    let (status, out) = explorer.get("/api/search/frame?item_id=imdb%3Att3&part=0&t_ms=1000");
    assert_eq!(
        (status, out["error"].as_str()),
        (404, Some("imdb:tt3 is not in the Frame index"))
    );
    let (status, out) = explorer.get("/api/search/frame?item_id=imdb%3Att1&part=11&t_ms=20000");
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("t_ms is past the end of the title (10000 ms)"))
    );
    let (status, out) =
        explorer.get("/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=0&out_ms=200000");
    assert_eq!(
        (status, out["error"].as_str()),
        (
            400,
            Some("the clip window must lie inside the title and be at most 120000 ms")
        )
    );
    let (status, out) =
        explorer.get("/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=5&out_ms=5");
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("the clip window is empty"))
    );
    let (status, out) =
        explorer.get("/api/search/clip?item_id=imdb%3Att1&part=11&in_ms=x&out_ms=5");
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("in_ms must be a whole number of milliseconds"))
    );
    let (status, out) = explorer.get("/api/search/frame?part=11&t_ms=5");
    assert_eq!(
        (status, out["error"].as_str()),
        (400, Some("item_id is required"))
    );
    // La Haine's part is not one Plex lists
    let (status, out) = explorer.get("/api/search/frame?item_id=imdb%3Att2&part=12&t_ms=0");
    assert_eq!(
        (status, out["error"].as_str()),
        (404, Some("Plex no longer lists part 12 for imdb:tt2"))
    );

    // the encoder refusing a new phrase names the URL and the phrases
    refuse.store(true, Ordering::SeqCst);
    let (status, out) = explorer.post(
        "/api/search/run",
        &json!({"phrase": "a cat in los angeles", "pool": ["imdb:tt1"]}),
    );
    assert_eq!(status, 503, "{out}");
    assert_eq!(out["encoder_down"]["url"], encoder);
    assert_eq!(
        out["encoder_down"]["phrases"],
        json!(["a cat in los angeles"])
    );
    assert!(
        out["error"]
            .as_str()
            .unwrap()
            .starts_with(&format!("the encoder at {encoder} refused 1 phrase(s): ")),
        "{out}"
    );
}

#[test]
fn a_missing_frame_index_and_tokenizer_are_reported_not_guessed() {
    let dir = tempfile::tempdir().expect("tempdir");
    let _ = common::store(dir.path());
    let mut child = Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .args(["explore", "--port", "0", "--store"])
        .arg(dir.path().join("plexdb.db"))
        .env_remove("PLEXDB_EXPLORE_FALLBACK")
        .env_remove("PLEXDB_FRAME_TOKENIZER")
        .env_remove("PLEXDB_FRAME_ENCODER_URL")
        .env_remove("PLEXDB_MEDIA_DIR")
        .env_remove("PLEX_URL")
        .env(
            "PLEXDB_EXPLORE_SAVED_PATH",
            dir.path().join("explore-data/explore-queries.json"),
        )
        .env("PLEXDB_FRAMES_DIR", dir.path().join("frames"))
        .env("PLEXDB_FRAME_MODEL", MODEL)
        .stdout(Stdio::piped())
        .spawn()
        .expect("start");
    let mut line = String::new();
    BufReader::new(child.stdout.as_mut().unwrap())
        .read_line(&mut line)
        .unwrap();
    let base = line
        .split("on ")
        .nth(1)
        .and_then(|r| r.split("/ reading").next())
        .unwrap()
        .to_string();
    let explorer = Explorer {
        child,
        base,
        data: dir.path().join("explore-data"),
    };
    let (status, out) = explorer.get("/api/search/status");
    assert_eq!(status, 200, "{out}");
    assert_eq!(out["ready"], false);
    let problems = out["problems"].as_object().unwrap();
    assert_eq!(problems["encoder"], "PLEXDB_FRAME_ENCODER_URL is not set");
    let tokenizer = problems["tokenizer"].as_str().unwrap();
    assert!(
        tokenizer.starts_with("no tokenizer for test/clip: ")
            && tokenizer.contains("tokenizer-test--clip.json"),
        "{tokenizer}"
    );
    assert!(
        problems["frames"]
            .as_str()
            .unwrap()
            .starts_with("no Frame index at "),
        "{out}"
    );
    let media = problems["media"].as_str().unwrap();
    assert!(
        media.contains("PLEX_URL") || media.contains("not found"),
        "{media}"
    );
    assert_eq!(out["indexed"], 0);
    let (status, out) = explorer.post("/api/search/run", &json!({"phrase": "x"}));
    assert_eq!(status, 503, "{out}");
    assert!(
        out["error"]
            .as_str()
            .unwrap()
            .starts_with("no Frame index at "),
        "{out}"
    );
    let (status, out) = explorer.get("/api/search/frame?item_id=x&part=1&t_ms=0");
    assert_eq!(status, 404, "{out}");
}
