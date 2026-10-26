//! `frames probe-encoder`, `frames quant-agreement` and `search`: the three commands that
//! moved from Python in plex-db-ex-f58.17. The encoder is a socket on port 0 answering
//! from memory; `frames.db` and `frames-float.db` are real files.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Output};

use plexdb::frame_quant::{agreement, float_path, load, open_float};
use plexdb::frames::{FRAME_DIMS, open_frames};
use rusqlite::Connection;
use serde_json::Value;

/// A unit vector along `axis`, as the little-endian float32 bytes the harvester stores.
fn unit(axis: usize, tilt: f32) -> Vec<f32> {
    let mut v = vec![0.0f32; FRAME_DIMS];
    v[axis] = 1.0;
    v[axis + 1] = tilt;
    v
}

fn blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

/// An encoder that answers every request with `FRAME_DIMS`-wide unit vectors along axis 0
/// and names `model`; it serves `requests` connections, then stops.
fn encoder(model: &'static str, requests: usize) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
    let url = format!("http://{}", listener.local_addr().expect("address"));
    std::thread::spawn(move || {
        for _ in 0..requests {
            let (mut stream, _) = listener.accept().expect("accept");
            let mut buf = vec![0u8; 1 << 20];
            let mut got = 0;
            // Read until the JSON body is whole: headers, then Content-Length bytes.
            loop {
                got += stream.read(&mut buf[got..]).expect("read");
                let text = String::from_utf8_lossy(&buf[..got]).to_string();
                if let Some((head, body)) = text.split_once("\r\n\r\n") {
                    let length = head
                        .lines()
                        .find_map(|l| {
                            l.to_lowercase()
                                .strip_prefix("content-length:")
                                .map(str::to_string)
                        })
                        .and_then(|n| n.trim().parse::<usize>().ok())
                        .unwrap_or(0);
                    if body.len() >= length {
                        let inputs =
                            serde_json::from_str::<Value>(body).expect("JSON body")["input"]
                                .as_array()
                                .map_or(1, Vec::len);
                        let data: Vec<Value> = (0..inputs)
                            .map(|index| {
                                serde_json::json!({"index": index, "embedding": unit(0, 0.0)})
                            })
                            .collect();
                        let answer = serde_json::json!({"model": model, "data": data}).to_string();
                        write!(
                            stream,
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{answer}",
                            answer.len()
                        )
                        .expect("write");
                        break;
                    }
                }
            }
        }
    });
    url
}

fn plexdb(dir: &Path, url: &str, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .args(args)
        .env("PLEXDB_PATH", dir.join("plexdb.db"))
        .env("PLEXDB_FRAMES_DIR", dir.join("frames"))
        .env("PLEXDB_FRAME_ENCODER_URL", url)
        .env("PLEXDB_FRAME_MODEL", "fake-clip")
        .output()
        .expect("run plexdb-rs")
}

#[test]
fn probe_encoder_reports_widths_and_timings_as_json() {
    let dir = tempfile::tempdir().expect("tempdir");
    let url = encoder("fake-clip", 3);
    let out = plexdb(dir.path(), &url, &["frames", "probe-encoder"]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let report: Value = serde_json::from_slice(&out.stdout).expect("JSON");
    assert_eq!(report["encoder_url"], url);
    assert_eq!(report["model"], "fake-clip");
    assert_eq!(report["served_by"], "fake-clip");
    assert_eq!(report["image"]["dims"], FRAME_DIMS as u64);
    assert_eq!(report["text"]["dims"], FRAME_DIMS as u64);
    assert_eq!(report["text"]["phrase"], "a red square");
    assert_eq!(report["frame_dims"], FRAME_DIMS as u64);
    assert!(report["cold_start_wait_ms"].is_u64());
}

#[test]
fn probe_encoder_refuses_an_encoder_answering_as_another_model() {
    let dir = tempfile::tempdir().expect("tempdir");
    let url = encoder("another-clip", 3);
    let out = plexdb(dir.path(), &url, &["frames", "probe-encoder"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&out.stderr)
            .contains("asked for fake-clip, the encoder answered as another-clip")
    );
}

#[test]
fn probe_encoder_against_a_refused_url_exits_non_zero_naming_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    let free = TcpListener::bind("127.0.0.1:0").expect("bind");
    let url = format!("http://{}", free.local_addr().expect("address"));
    drop(free);
    let out = plexdb(dir.path(), &url, &["frames", "probe-encoder"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&out.stderr)
            .contains(&format!("encoder unavailable at {url}/v1/embeddings"))
    );
}

/// Two titles, three frames: int8 and the floats they came from.
fn seed(dir: &Path) -> Connection {
    let frames_dir = dir.join("frames");
    std::fs::create_dir_all(&frames_dir).expect("frames dir");
    let frames = open_frames(&frames_dir.join("frames.db")).expect("frames.db");
    let floats = open_float(&float_path(&frames_dir.join("frames.db"))).expect("float file");
    let rows: [(&str, i64, i64, Vec<f32>); 4] = [
        ("imdb:tt1", 0, 0, unit(0, 0.1)),
        ("imdb:tt1", 1000, 1, unit(2, 0.1)),
        ("imdb:tt2", 0, 0, unit(4, 0.1)),
        // tt3 has frames but no float rows: it is reported as without floats.
        ("imdb:tt3", 0, 0, unit(6, 0.1)),
    ];
    for (item, t_ms, shot, v) in &rows {
        frames
            .execute(
                "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) \
                 VALUES (?1, ?2, ?3, vec_quantize_int8(?4, 'unit'))",
                (item, t_ms, shot, blob(v)),
            )
            .expect("frame_vec");
        if *item != "imdb:tt3" {
            floats
                .execute(
                    "INSERT INTO frame_float (item_id, t_ms, shot_ix, vec) VALUES (?1, ?2, ?3, ?4)",
                    (item, t_ms, shot, blob(v)),
                )
                .expect("frame_float");
        }
    }
    frames
}

#[test]
fn quant_agreement_counts_the_shared_top_k_and_names_titles_without_floats() {
    let dir = tempfile::tempdir().expect("tempdir");
    let frames = seed(dir.path());
    let floats =
        open_float(&float_path(&dir.path().join("frames").join("frames.db"))).expect("floats");
    let paired = load(&frames, &floats).expect("load");
    assert_eq!(paired.titles, 3);
    assert_eq!(paired.titles_without_floats, vec!["imdb:tt3".to_string()]);
    let phrase: Vec<f64> = unit(0, 0.0).iter().map(|x| f64::from(*x)).collect();
    let report = agreement(&paired, &phrase, &[1, 2]);
    let text = plexdb::pyjson::dumps_indent2(&report);
    let value: Value = serde_json::from_str(&text).expect("JSON");
    assert_eq!(value["frames"], 3);
    assert_eq!(value["shots"], 3);
    assert_eq!(value["top"]["1"], 1);
    assert_eq!(value["top"]["2"], 2);
    assert_eq!(value["same_first"], true);
    let diff = value["max_abs_sim_diff"].as_f64().expect("a number");
    assert!(
        (0.0..0.01).contains(&diff),
        "int8 and fp16 differ by {diff}"
    );
}

#[test]
fn quant_agreement_runs_end_to_end_and_asks_for_floats_when_missing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let url = encoder("fake-clip", 1);
    let out = plexdb(dir.path(), &url, &["frames", "quant-agreement", "a cat"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("run harvest-frames --keep-float first"));

    drop(seed(dir.path()));
    let out = plexdb(
        dir.path(),
        &url,
        &["frames", "quant-agreement", "a cat", "--top", "1"],
    );
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let report: Value = serde_json::from_slice(&out.stdout).expect("JSON");
    assert_eq!(report["titles"], 3);
    assert_eq!(report["phrases"][0]["phrase"], "a cat");
    assert_eq!(report["phrases"][0]["top"]["1"], 1);
    assert!(report["phrases"][0]["top"].get("10").is_none());
}

#[test]
fn search_refuses_an_empty_pool_a_seed_without_a_draw_and_an_unset_encoder() {
    let dir = tempfile::tempdir().expect("tempdir");
    let out = plexdb(dir.path(), "http://127.0.0.1:1", &["search", "a cat"]);
    assert_eq!(out.status.code(), Some(78));
    assert!(String::from_utf8_lossy(&out.stderr).contains("name the titles to search"));

    let out = plexdb(
        dir.path(),
        "http://127.0.0.1:1",
        &["search", "a cat", "--pool", "x", "--seed", "3"],
    );
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("--seed needs --draw"));

    let out = plexdb(dir.path(), "", &["search", "a cat", "--pool", "x"]);
    assert_eq!(out.status.code(), Some(78));
    assert!(String::from_utf8_lossy(&out.stderr).contains("PLEXDB_FRAME_ENCODER_URL is not set"));
}
