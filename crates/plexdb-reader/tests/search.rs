//! Shot search over a frames.db built by this repository's own writer, with a
//! fake encoder answering on a local port. Vectors are unit axes of the 768-wide
//! space, so every similarity below is a number that can be read off by eye.

mod common;

use std::collections::BTreeSet;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use plexdb_reader::{PhraseCache, Reader, ReaderError, Search, Url};
use rusqlite::Connection;
use serde_json::Value;

const DIMS: usize = 768;
const MODEL: &str = "test-clip";

fn approx(a: f32, b: f32) -> bool {
    (a - b).abs() < 1e-5
}

/// A unit vector along axis `axis`.
fn axis(axis: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIMS];
    v[axis] = 1.0;
    v
}

/// What the fake encoder knows how to embed.
fn embedding_of(phrase: &str) -> Vec<f32> {
    match phrase {
        "cat" => axis(0),
        "dog" => axis(1),
        "night" => axis(2),
        _ => axis(3),
    }
}

struct FakeEncoder {
    url: Url,
    calls: Arc<AtomicUsize>,
    seen: Arc<Mutex<Vec<Vec<String>>>>,
}

impl FakeEncoder {
    fn start() -> Self {
        Self::aliased(MODEL, MODEL)
    }

    /// An encoder that accepts `accepts` as the request's model and answers as `answers`.
    fn aliased(accepts: &'static str, answers: &'static str) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
        let port = listener.local_addr().expect("local addr").port();
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (c, s) = (calls.clone(), seen.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut raw = Vec::new();
                let mut buf = [0u8; 4096];
                let body = loop {
                    let n = stream.read(&mut buf).unwrap_or(0);
                    raw.extend_from_slice(&buf[..n]);
                    let text = String::from_utf8_lossy(&raw).to_string();
                    if let Some(split) = text.find("\r\n\r\n") {
                        let length = text[..split]
                            .lines()
                            .find_map(|l| {
                                l.to_ascii_lowercase()
                                    .strip_prefix("content-length:")
                                    .map(|v| v.trim().parse::<usize>().unwrap_or(0))
                            })
                            .unwrap_or(0);
                        if raw.len() >= split + 4 + length {
                            break text[split + 4..split + 4 + length].to_string();
                        }
                    }
                    if n == 0 {
                        break String::new();
                    }
                };
                let request: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
                let inputs: Vec<String> = request["input"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default();
                assert_eq!(request["modality"], "text");
                assert_eq!(request["model"], accepts);
                c.fetch_add(1, Ordering::SeqCst);
                let data: Vec<Value> = inputs
                    .iter()
                    .enumerate()
                    .map(|(i, p)| {
                        // Not unit length on purpose: the Reader scales it.
                        let scaled: Vec<f32> = embedding_of(p).iter().map(|x| x * 3.0).collect();
                        serde_json::json!({"index": i, "embedding": scaled})
                    })
                    .collect();
                s.lock().expect("lock").push(inputs);
                let payload = serde_json::json!({"data": data, "model": answers}).to_string();
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                    payload.len()
                );
            }
        });
        Self {
            url: Url::parse(&format!("http://127.0.0.1:{port}")).expect("url"),
            calls,
            seen,
        }
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

/// A port nothing listens on.
fn refused_url() -> Url {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
    let port = listener.local_addr().expect("local addr").port();
    drop(listener);
    Url::parse(&format!("http://127.0.0.1:{port}")).expect("url")
}

struct Fixture {
    dir: tempfile::TempDir,
    snapshot: PathBuf,
    frames: PathBuf,
}

impl Fixture {
    fn cache(&self) -> PathBuf {
        self.dir.path().join("phrases.db")
    }

    fn reader(&self, encoder: &Url) -> Reader {
        self.reader_as(encoder, MODEL)
    }

    fn reader_as(&self, encoder: &Url, request_model: &str) -> Reader {
        Reader::open(
            &self.snapshot,
            Some(&self.frames),
            Some(PhraseCache {
                path: self.cache(),
                encoder_url: encoder.clone(),
                request_model: request_model.to_string(),
            }),
        )
        .expect("open the reader")
    }
}

fn register_vec() {
    // The writer loads the same extension into its own connection.
    use std::sync::Once;
    static ONCE: Once = Once::new();
    ONCE.call_once(|| unsafe {
        rusqlite::ffi::sqlite3_auto_extension(Some(std::mem::transmute::<
            *const (),
            unsafe extern "C" fn(
                *mut rusqlite::ffi::sqlite3,
                *mut *mut std::os::raw::c_char,
                *const rusqlite::ffi::sqlite3_api_routines,
            ) -> std::os::raw::c_int,
        >(
            sqlite_vec::sqlite3_vec_init as *const ()
        )));
    });
}

/// An int8 vector: `(axis, value)` pairs on a zero background.
fn int8(parts: &[(usize, i8)]) -> Vec<u8> {
    let mut v = vec![0i8; DIMS];
    for (axis, value) in parts {
        v[*axis] = *value;
    }
    v.into_iter().map(|x| x as u8).collect()
}

struct Title<'a> {
    id: &'a str,
    duration_ms: Option<i64>,
    /// `(t_ms, source)`
    cuts: &'a [(i64, &'a str)],
    /// `(shot_ix, t_ms, vector)`
    frames: Vec<(i64, i64, Vec<u8>)>,
    model: &'a str,
}

fn add_title(conn: &Connection, t: &Title<'_>) {
    conn.execute(
        "INSERT INTO frame_index_state (item_id, part_id, model, model_version, dims, quant, \
         duration_ms, bytes, file_hash, shots, vectors, indexed_at, error) \
         VALUES (?1, 1, ?2, '1', 768, 'int8', ?3, 0, 'h', 0, ?4, '2026-10-16T00:00:00+00:00', NULL)",
        (t.id, t.model, t.duration_ms, t.frames.len() as i64),
    )
    .expect("state row");
    for (at, source) in t.cuts {
        conn.execute(
            "INSERT INTO cuts (item_id, t_ms, score, source) VALUES (?1, ?2, NULL, ?3)",
            (t.id, at, source),
        )
        .expect("cut row");
    }
    for (shot_ix, t_ms, vec) in &t.frames {
        conn.execute(
            "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?1, ?2, ?3, vec_int8(?4))",
            (t.id, t_ms, shot_ix, vec),
        )
        .expect("frame row");
    }
}

/// Four titles the tests search, plus one in the pool that is not indexed.
///
/// - `imdb:a` has three Shots: a cat, a dog with a half-cat frame, and night.
/// - `imdb:b` has one cat Shot, identical to `imdb:a`'s first, to force a tie.
/// - `imdb:d` has six near-identical cat Shots, for the draw.
/// - `imdb:old` was indexed by another model.
fn fixture() -> Fixture {
    register_vec();
    let dir = tempfile::tempdir().expect("tempdir");
    let snapshot = dir.path().join("plexdb.db");
    common::build_fixture(&snapshot);
    let frames_dir = dir.path().join("frames");
    let status = Command::new("uv")
        .args(["run", "plexdb", "frames", "migrate"])
        .env("PLEXDB_PATH", &snapshot)
        .env("PLEXDB_FRAMES_DIR", &frames_dir)
        .current_dir(common::repo_root())
        .status()
        .expect("run plexdb frames migrate");
    assert!(status.success(), "plexdb frames migrate failed");
    let frames = frames_dir.join("frames.db");
    let conn = Connection::open(&frames).expect("open frames.db");
    // SAFETY-free: the extension was registered process-wide above, before this open.
    add_title(
        &conn,
        &Title {
            id: "imdb:a",
            duration_ms: Some(100_000),
            cuts: &[(10_000, "scdet"), (20_000, "chapter")],
            frames: vec![
                (0, 0, int8(&[(0, 100)])),
                (1, 10_000, int8(&[(1, 100)])),
                (1, 12_000, int8(&[(0, 70), (1, 70)])),
                (2, 20_000, int8(&[(2, 100)])),
            ],
            model: MODEL,
        },
    );
    add_title(
        &conn,
        &Title {
            id: "imdb:b",
            duration_ms: Some(50_000),
            cuts: &[],
            frames: vec![(0, 0, int8(&[(0, 100)]))],
            model: MODEL,
        },
    );
    let near: Vec<(i64, i64, Vec<u8>)> = (0..6)
        .map(|i| (i, i * 5_000, int8(&[(0, 100), (10 + i as usize, 1)])))
        .collect();
    let near_cuts: Vec<(i64, &str)> = (1..6).map(|i| (i * 5_000, "scdet")).collect();
    add_title(
        &conn,
        &Title {
            id: "imdb:d",
            duration_ms: Some(30_000),
            cuts: &near_cuts,
            frames: near,
            model: MODEL,
        },
    );
    // The 4,200 ms cut is within 500 ms of the 4,000 ms one, so it makes no Shot.
    add_title(
        &conn,
        &Title {
            id: "imdb:close",
            duration_ms: Some(10_000),
            cuts: &[(4_000, "scdet"), (4_200, "scdet"), (8_000, "chapter")],
            frames: vec![
                (0, 2_000, int8(&[(0, 100)])),
                (1, 6_000, int8(&[(0, 100)])),
                (2, 9_000, int8(&[(0, 100)])),
            ],
            model: MODEL,
        },
    );
    add_title(
        &conn,
        &Title {
            id: "imdb:old",
            duration_ms: Some(1_000),
            cuts: &[],
            frames: vec![(0, 0, int8(&[(0, 100)]))],
            model: "other-clip",
        },
    );
    conn.execute(
        "INSERT INTO frame_index_state (item_id, part_id, indexed_at, error) \
         VALUES ('imdb:failed', 2, '2026-10-16T00:00:00+00:00', 'ffmpeg exited 234')",
        (),
    )
    .expect("failed row");
    drop(conn);
    Fixture {
        dir,
        snapshot,
        frames,
    }
}

fn search<'a>(pool: &'a [&'a str], phrase: &'a str) -> Search<'a> {
    Search {
        pool,
        phrase,
        negatives: &[],
        weight: None,
        temperature: None,
    }
}

const POOL: [&str; 4] = ["imdb:a", "imdb:b", "imdb:d", "imdb:missing"];

#[test]
fn shots_are_ordered_by_score_then_item_and_start_and_repeat_exactly() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let first = reader.search_shots(&search(&POOL, "Cat")).expect("search");
    let second = reader.search_shots(&search(&POOL, "cat")).expect("search");
    assert_eq!(first, second);

    let order: Vec<(&str, u64)> = first
        .shots
        .iter()
        .map(|s| (s.item_id.as_str(), s.in_ms))
        .collect();
    // Three Shots score 1.0 (a:0, b:0, and d's near-ties just under it).
    assert_eq!(order[0], ("imdb:a", 0));
    assert_eq!(order[1], ("imdb:b", 0));
    let scores: Vec<f32> = first.shots.iter().map(|s| s.score).collect();
    assert!(scores.windows(2).all(|w| w[0] >= w[1]), "{scores:?}");
    assert!(approx(scores[0], 1.0) && approx(scores[1], 1.0));
    // The half-cat frame makes a's second Shot 0.7071.
    let a1 = first
        .shots
        .iter()
        .find(|s| s.item_id == "imdb:a" && s.in_ms == 10_000)
        .expect("a's second shot");
    assert!(
        approx(a1.phrase_sim, std::f32::consts::FRAC_1_SQRT_2),
        "{}",
        a1.phrase_sim
    );
    assert_eq!(a1.out_ms, 20_000);
    assert_eq!(a1.frames.len(), 2);
}

#[test]
fn the_last_shot_ends_at_the_titles_duration() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let found = reader.search_shots(&search(&POOL, "cat")).expect("search");
    let last = found
        .shots
        .iter()
        .find(|s| s.item_id == "imdb:a" && s.in_ms == 20_000)
        .expect("a's last shot");
    assert_eq!(last.out_ms, 100_000);
    let only = found
        .shots
        .iter()
        .find(|s| s.item_id == "imdb:b")
        .expect("b's shot");
    assert_eq!((only.in_ms, only.out_ms), (0, 50_000));
}

#[test]
fn a_cut_within_half_a_second_of_the_one_before_makes_no_shot() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let found = reader
        .search_shots(&search(&["imdb:close"], "cat"))
        .expect("search");
    let spans: Vec<(u64, u64)> = found.shots.iter().map(|s| (s.in_ms, s.out_ms)).collect();
    assert_eq!(spans, vec![(0, 4_000), (4_000, 8_000), (8_000, 10_000)]);
}

#[test]
fn score_is_phrase_sim_minus_weight_times_the_strongest_negative() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let pool = ["imdb:a"];
    let none = reader.search_shots(&search(&pool, "cat")).expect("search");
    for hit in &none.shots {
        assert!(hit.negative_sims.is_empty());
        assert!(approx(hit.score, hit.phrase_sim));
    }

    let negatives = ["dog", "night"];
    let default_weight = reader
        .search_shots(&Search {
            negatives: &negatives,
            ..search(&pool, "cat")
        })
        .expect("search");
    let weighted = reader
        .search_shots(&Search {
            negatives: &negatives,
            weight: Some(0.8),
            ..search(&pool, "cat")
        })
        .expect("search");
    for (hit, w) in default_weight
        .shots
        .iter()
        .zip([0.5f32; 3])
        .chain(weighted.shots.iter().zip([0.8f32; 3]))
    {
        assert_eq!(hit.negative_sims.len(), 2);
        let worst = hit.negative_sims.iter().copied().fold(f32::MIN, f32::max);
        assert!(approx(hit.score, hit.phrase_sim - w * worst), "{hit:?}");
    }
    // a's dog Shot: phrase 0.7071, dog 1.0, so 0.7071 - 0.5.
    let dog_shot = default_weight
        .shots
        .iter()
        .find(|s| s.in_ms == 10_000)
        .expect("dog shot");
    assert!(
        approx(dog_shot.score, std::f32::consts::FRAC_1_SQRT_2 - 0.5),
        "{}",
        dog_shot.score
    );
    assert!(approx(dog_shot.negative_sims[0], 1.0));
    assert!(approx(dog_shot.negative_sims[1], 0.0));
}

#[test]
fn a_temperature_gives_probabilities_that_sum_to_one_in_score_order() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let cold = reader.search_shots(&search(&POOL, "cat")).expect("search");
    assert!(cold.shots.iter().all(|s| s.p.is_none()));

    let warm = reader
        .search_shots(&Search {
            temperature: Some(0.1),
            ..search(&POOL, "cat")
        })
        .expect("search");
    let ps: Vec<f32> = warm.shots.iter().map(|s| s.p.expect("p")).collect();
    assert!(approx(ps.iter().sum(), 1.0), "{}", ps.iter().sum::<f32>());
    assert!(ps.windows(2).all(|w| w[0] >= w[1]), "{ps:?}");
    // exp(score / T) / sum, recomputed from the scores.
    let total: f64 = warm
        .shots
        .iter()
        .map(|s| (f64::from(s.score) / 0.1).exp())
        .sum();
    let first = (f64::from(warm.shots[0].score) / 0.1).exp() / total;
    assert!((f64::from(ps[0]) - first).abs() < 1e-5);
}

#[test]
fn draw_is_repeatable_by_seed_and_varies_across_seeds() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let pool = ["imdb:d"];
    let found = reader
        .search_shots(&Search {
            temperature: Some(1.0),
            ..search(&pool, "cat")
        })
        .expect("search");
    assert_eq!(found.shots.len(), 6);

    let key =
        |hits: &[plexdb_reader::ShotHit]| -> Vec<u64> { hits.iter().map(|h| h.in_ms).collect() };
    assert_eq!(
        key(&reader.draw(&found, 3, 7)),
        key(&reader.draw(&found, 3, 7))
    );
    let picks: BTreeSet<Vec<u64>> = (0..30)
        .map(|seed| key(&reader.draw(&found, 3, seed)))
        .collect();
    assert!(
        picks.len() > 3,
        "30 seeds gave {} distinct draws",
        picks.len()
    );
    for seed in 0..30 {
        let drawn = key(&reader.draw(&found, 3, seed));
        let unique: BTreeSet<_> = drawn.iter().collect();
        assert_eq!(unique.len(), 3, "a draw never repeats a shot");
    }
    assert_eq!(reader.draw(&found, 99, 1).len(), 6);
}

#[test]
fn draw_without_a_temperature_is_the_top_n() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let found = reader.search_shots(&search(&POOL, "cat")).expect("search");
    let top: Vec<_> = found.shots.iter().take(2).cloned().collect();
    assert_eq!(reader.draw(&found, 2, 1), top);
    assert_eq!(reader.draw(&found, 2, 999), top);
}

#[test]
fn indexed_and_pool_count_titles_with_and_without_a_state_row() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let found = reader.search_shots(&search(&POOL, "cat")).expect("search");
    assert_eq!(found.pool, 4);
    assert_eq!(found.indexed, 3);
    assert_eq!(found.model, MODEL);

    let nothing = reader
        .search_shots(&search(&["imdb:missing", "imdb:missing"], "cat"))
        .expect("search");
    assert_eq!(
        (nothing.pool, nothing.indexed, nothing.shots.len()),
        (1, 0, 0)
    );
    assert_eq!(nothing.model, "");
    assert_eq!(enc.calls(), 1, "an empty index needs no embedding");
}

#[test]
fn a_title_whose_indexing_failed_is_not_indexed_and_mixes_no_model() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let found = reader
        .search_shots(&search(&["imdb:a", "imdb:failed"], "cat"))
        .expect("a failed title is skipped, not refused");
    assert_eq!((found.pool, found.indexed), (2, 1));
    assert_eq!(found.model, MODEL);
}

#[test]
fn a_pool_mixing_two_models_is_refused() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    let err = reader
        .search_shots(&search(&["imdb:a", "imdb:old"], "cat"))
        .expect_err("two models");
    assert!(matches!(err, ReaderError::MixedModels(_)), "{err}");
}

#[test]
fn a_phrase_is_embedded_once_however_it_is_spelled() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    reader.search_shots(&search(&POOL, "cat")).expect("first");
    assert_eq!(enc.calls(), 1);
    reader
        .search_shots(&search(&POOL, "  CAT "))
        .expect("second");
    assert_eq!(enc.calls(), 1, "different case and spacing is a hit");
    assert_eq!(enc.seen.lock().expect("lock")[0], vec!["cat".to_string()]);

    // Negatives are cached the same way, and only the misses are sent.
    reader
        .search_shots(&Search {
            negatives: &["Dog", "cat"],
            ..search(&POOL, "cat")
        })
        .expect("third");
    assert_eq!(enc.calls(), 2);
    assert_eq!(enc.seen.lock().expect("lock")[1], vec!["dog".to_string()]);

    let cache = Connection::open(fx.cache()).expect("open the cache");
    let n: i64 = cache
        .query_row("SELECT count(*) FROM phrase_cache", [], |r| r.get(0))
        .expect("count");
    assert_eq!(n, 2);
}

#[test]
fn the_cache_forgets_a_row_unused_for_ninety_days_and_never_uses_another_models() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    {
        // Seed the cache file through a first search, then age and re-label rows.
        let reader = fx.reader(&enc.url);
        reader.search_shots(&search(&POOL, "cat")).expect("seed");
    }
    let cache = Connection::open(fx.cache()).expect("open the cache");
    let blob = vec![0u8; DIMS * 4];
    cache
        .execute(
            "INSERT INTO phrase_cache VALUES ('stale', ?1, '1', ?2, '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')",
            (MODEL, &blob),
        )
        .expect("stale row");
    cache
        .execute(
            "INSERT INTO phrase_cache VALUES ('night', 'other-clip', '1', ?1, '2026-01-01T00:00:00Z', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (&blob,),
        )
        .expect("other-model row");
    cache
        .execute(
            "UPDATE phrase_cache SET last_used_at = '2026-01-01T00:00:00Z' WHERE phrase = 'cat'",
            [],
        )
        .expect("age the cat row");
    drop(cache);

    let reader = fx.reader(&enc.url);
    let before = enc.calls();
    reader.search_shots(&search(&POOL, "night")).expect("night");
    assert_eq!(
        enc.calls(),
        before + 1,
        "the other model's night vector is not used"
    );

    let cache = Connection::open(fx.cache()).expect("open the cache");
    let stale: i64 = cache
        .query_row(
            "SELECT count(*) FROM phrase_cache WHERE phrase = 'stale'",
            [],
            |r| r.get(0),
        )
        .expect("count");
    assert_eq!(stale, 0, "a row unused for 90 days is deleted");

    // A hit moves last_used_at forward.
    reader.search_shots(&search(&POOL, "night")).expect("hit");
    assert_eq!(enc.calls(), before + 1);
    let used: String = cache
        .query_row(
            "SELECT last_used_at FROM phrase_cache WHERE phrase = 'night' AND model = ?1",
            (MODEL,),
            |r| r.get(0),
        )
        .expect("last_used_at");
    assert!(used.as_str() > "2026-10-01", "{used}");
}

#[test]
fn a_refused_encoder_is_encoder_unavailable_and_a_cached_phrase_still_answers() {
    let fx = fixture();
    let live = FakeEncoder::start();
    fx.reader(&live.url)
        .search_shots(&search(&POOL, "cat"))
        .expect("seed the cache");

    let reader = fx.reader(&refused_url());
    let err = reader
        .search_shots(&search(&POOL, "dog"))
        .expect_err("not cached");
    assert!(matches!(err, ReaderError::EncoderUnavailable(_)), "{err}");
    reader
        .search_shots(&search(&POOL, "cat"))
        .expect("cached phrases need no encoder");
}

#[test]
fn search_needs_frames_and_a_phrase_cache() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    assert!(fx.reader(&enc.url).capabilities().search);

    let no_frames = Reader::open(
        &fx.snapshot,
        None,
        Some(PhraseCache {
            path: fx.cache(),
            encoder_url: enc.url.clone(),
            request_model: MODEL.to_string(),
        }),
    )
    .expect("open");
    assert!(!no_frames.capabilities().search);
    assert!(matches!(
        no_frames.search_shots(&search(&POOL, "cat")),
        Err(ReaderError::FramesNotAttached)
    ));

    let no_cache = Reader::open(&fx.snapshot, Some(&fx.frames), None).expect("open");
    assert!(!no_cache.capabilities().search);
    assert!(matches!(
        no_cache.search_shots(&search(&POOL, "cat")),
        Err(ReaderError::NoPhraseCache)
    ));
}

#[test]
fn cuts_and_chapters_are_the_titles_boundaries() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let reader = fx.reader(&enc.url);
    assert_eq!(reader.cuts("imdb:a").expect("cuts"), vec![10_000, 20_000]);
    assert_eq!(reader.chapters("imdb:a").expect("chapters"), vec![20_000]);
    assert!(reader.cuts("imdb:missing").expect("cuts").is_empty());
}

#[test]
fn a_frames_file_at_another_schema_version_is_refused_at_open() {
    let fx = fixture();
    let conn = Connection::open(&fx.frames).expect("open frames.db");
    conn.execute("UPDATE schema_version SET version = 3", [])
        .expect("bump");
    drop(conn);
    let err = Reader::open(&fx.snapshot, Some(&fx.frames), None).expect_err("version 3");
    assert!(
        matches!(
            err,
            ReaderError::UnsupportedFramesVersion {
                found: 3,
                supported: 2,
                ..
            }
        ),
        "{err}"
    );
}

#[test]
fn a_missing_frames_file_is_refused_at_open_and_not_created() {
    let fx = fixture();
    let nowhere = fx.dir.path().join("nowhere").join("frames.db");
    let err = Reader::open(&fx.snapshot, Some(&nowhere), None).expect_err("absent");
    assert!(matches!(err, ReaderError::FramesOpen { .. }), "{err}");
    assert!(!nowhere.exists());
}

fn read_bin(fx: &Fixture, encoder: &Url, args: &[&str]) -> (bool, String, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_plexdb-read"))
        .args(["--snapshot", path_str(&fx.snapshot)])
        .args(["--frames", path_str(&fx.frames)])
        .args(["--phrase-cache", path_str(&fx.cache())])
        .args(["--encoder-url", encoder.as_str()])
        .args(["--encoder-model", MODEL])
        .args(args)
        .output()
        .expect("run plexdb-read");
    (
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

fn path_str(p: &Path) -> &str {
    p.to_str().expect("utf-8 path")
}

#[test]
fn plexdb_read_prints_the_search_as_json_and_fails_with_a_message() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let (ok, out, err) = read_bin(
        &fx,
        &enc.url,
        &[
            "search",
            "cat",
            "--pool",
            "imdb:a",
            "--pool",
            "imdb:b",
            "--not",
            "dog",
            "--weight",
            "0.5",
            "--temperature",
            "0.2",
            "--draw",
            "2",
            "--seed",
            "4",
        ],
    );
    assert!(ok, "{err}");
    let json: Value = serde_json::from_str(&out).expect("JSON on stdout");
    assert_eq!(json["pool"], 2);
    assert_eq!(json["indexed"], 2);
    assert_eq!(json["model"], MODEL);
    assert_eq!(json["shots"].as_array().expect("shots").len(), 2);

    let reader = fx.reader(&enc.url);
    let found = reader
        .search_shots(&Search {
            negatives: &["dog"],
            weight: Some(0.5),
            temperature: Some(0.2),
            ..search(&["imdb:a", "imdb:b"], "cat")
        })
        .expect("search");
    let mut expected = found.clone();
    expected.shots = reader.draw(&found, 2, 4);
    let expected: Value =
        serde_json::from_str(&serde_json::to_string(&expected).expect("to_string")).expect("parse");
    assert_eq!(json, expected);

    let (ok, out, err) = read_bin(
        &fx,
        &enc.url,
        &[
            "search", "--pool", "imdb:a", "--pool", "imdb:b", "cat", "--not", "--dog",
        ],
    );
    assert!(ok, "{err}");
    let flags_first: Value = serde_json::from_str(&out).expect("JSON on stdout");
    assert_eq!(flags_first["pool"], 2);

    let (ok, _, err) = read_bin(
        &fx,
        &enc.url,
        &["search", "cat", "--pool", "imdb:a", "imdb:b"],
    );
    assert!(!ok);
    assert!(err.contains("unexpected argument imdb:b"), "{err}");

    let (ok, out, _) = read_bin(&fx, &enc.url, &["--help"]);
    assert!(ok);
    assert!(out.starts_with("usage: plexdb-read"), "{out}");

    let (ok, out, _) = read_bin(&fx, &enc.url, &["cuts", "imdb:a"]);
    assert!(ok);
    assert_eq!(out.trim(), "[10000,20000]");

    let (ok, out, _) = read_bin(&fx, &enc.url, &["capabilities"]);
    assert!(ok);
    assert_eq!(out.trim(), r#"{"search":true}"#);

    let (ok, out, err) = read_bin(
        &fx,
        &refused_url(),
        &["search", "unseen phrase", "--pool", "imdb:a"],
    );
    assert!(!ok);
    assert!(out.is_empty());
    assert!(err.starts_with("error: "), "{err}");
}

#[test]
fn an_encoder_that_accepts_an_alias_serves_an_index_recorded_under_the_real_name() {
    let fx = fixture();
    let enc = FakeEncoder::aliased("alias-clip", MODEL);
    let found = fx
        .reader_as(&enc.url, "alias-clip")
        .search_shots(&search(&POOL, "cat"))
        .expect("the alias is sent, the real name is answered");
    assert_eq!(found.model, MODEL);
    assert!(!found.shots.is_empty());
}

#[test]
fn an_encoder_answering_as_another_model_is_refused_and_caches_nothing() {
    let fx = fixture();
    let enc = FakeEncoder::aliased(MODEL, "other-clip");
    let err = fx
        .reader(&enc.url)
        .search_shots(&search(&POOL, "cat"))
        .expect_err("wrong model answered");
    assert!(matches!(err, ReaderError::EncoderFailed(_)), "{err}");
    let kept: i64 = Connection::open(fx.cache())
        .expect("cache")
        .query_row("SELECT count(*) FROM phrase_cache", [], |r| r.get(0))
        .expect("count");
    assert_eq!(kept, 0);
}

#[test]
fn no_configured_model_fails_only_a_phrase_that_needs_the_encoder() {
    let fx = fixture();
    let enc = FakeEncoder::start();
    let err = fx
        .reader_as(&enc.url, "")
        .search_shots(&search(&POOL, "cat"))
        .expect_err("no model to send");
    assert!(matches!(err, ReaderError::InvalidSearch(_)), "{err}");
    assert_eq!(enc.calls(), 0);
    fx.reader(&enc.url)
        .search_shots(&search(&POOL, "cat"))
        .expect("seed the cache");
    fx.reader_as(&enc.url, "")
        .search_shots(&search(&POOL, "cat"))
        .expect("a cached phrase needs no model");
}

#[test]
fn an_answer_naming_no_model_is_accepted() {
    let fx = fixture();
    let enc = FakeEncoder::aliased(MODEL, "");
    fx.reader(&enc.url)
        .search_shots(&search(&POOL, "cat"))
        .expect("an unnamed model is not a mismatch");
}
