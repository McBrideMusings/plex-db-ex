//! `harvest-frames` over a real `frames.db` and a real migrated store, with a Plex, a
//! video reader and an encoder that answer from memory: what it indexes and in which
//! order, what a failed file and a refusing encoder leave behind, and how a run stops.

mod common;

use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};

use common::{FakePlex, store};
use plexdb::frame_encoder::{Embedded, Encoder, EncoderError};
use plexdb::frame_media::{Cut, Jpegs, MediaError, MediaReader, Scan};
use plexdb::frames::{FRAME_DIMS, open_frames};
use plexdb::harvest_frames::{
    Inputs, Options, STOP_EMPTY, STOP_ENCODER, STOP_LIMIT, STOP_REJECTED, STOP_WINDOW, harvest,
};
use plexdb::localtime::Local;
use plexdb::plex::{Section, TYPE_EPISODE, TYPE_MOVIE};
use rusqlite::Connection;
use serde_json::json;

/// A video reader whose files are a name: `broken` cannot be read, and the rest scan to
/// two Shots with one frame each in the first.
struct FakeMedia {
    scanned: RefCell<Vec<String>>,
}

impl MediaReader for FakeMedia {
    fn file_hash(&self, path: &Path, size: i64) -> Result<String, MediaError> {
        Ok(format!("hash:{}:{size}", path.display()))
    }

    fn scan(&self, path: &Path) -> Result<Scan, MediaError> {
        self.scanned.borrow_mut().push(path.display().to_string());
        if path.ends_with("broken.mkv") {
            return Err(MediaError::new(
                format!("{}: source file damaged", path.display()),
                "source file damaged (1 decode errors)",
            ));
        }
        Ok(Scan {
            cuts: vec![Cut {
                t_ms: 3000,
                score: Some(14.5),
                source: "scdet",
            }],
            last_frame_ms: 5000,
            warnings: 2,
        })
    }

    fn frames(&self, _path: &Path, times_ms: &[i64]) -> Result<Jpegs, MediaError> {
        Ok(times_ms
            .iter()
            .map(|t| Some(t.to_string().into_bytes()))
            .collect())
    }
}

enum Answer {
    Vectors,
    Unavailable,
    Rejected,
    NarrowVectors,
}

struct FakeEncoder {
    answer: Answer,
    requests: Cell<usize>,
}

impl Encoder for FakeEncoder {
    fn model(&self) -> &str {
        "fake-clip"
    }

    fn timed_images(&self, images: &[Vec<u8>]) -> Result<Embedded, EncoderError> {
        self.requests.set(self.requests.get() + 1);
        match self.answer {
            Answer::Unavailable => Err(EncoderError::Unavailable("connection refused".into())),
            Answer::Rejected => Err(EncoderError::Rejected("HTTP 400: bad image".into())),
            Answer::NarrowVectors => Ok(Embedded {
                vectors: images.iter().map(|_| vec![1.0; 3]).collect(),
                model: String::new(),
                first_byte_s: 0.0,
                total_s: 0.0,
            }),
            Answer::Vectors => Ok(Embedded {
                vectors: images
                    .iter()
                    .map(|_| {
                        let mut vector = vec![0.0; FRAME_DIMS];
                        vector[0] = 1.0;
                        vector
                    })
                    .collect(),
                model: "fake-clip-v2".into(),
                first_byte_s: 0.0,
                total_s: 0.5,
            }),
        }
    }
}

struct World {
    dir: tempfile::TempDir,
    plex: FakePlex,
}

fn movie_record(rating_key: &str, added_at: i64, part_id: i64, file: &str) -> serde_json::Value {
    json!({
        "ratingKey": rating_key,
        "addedAt": added_at,
        "Media": [{"Part": [{"id": part_id, "size": 1000 + part_id, "file": file}]}]
    })
}

/// A store with the given `(item_id, rating_key, type)` titles, all in section `1`, and a
/// Plex that lists the movies of section `1`.
fn world(titles: &[(&str, &str, &str)], records: Vec<serde_json::Value>) -> World {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    for (item_id, rating_key, kind) in titles {
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES (?1, ?2, ?1)",
            (item_id, kind),
        )
        .expect("item");
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
             VALUES (?1, ?2, '1', '2026-01-01')",
            (rating_key, item_id),
        )
        .expect("plex item");
    }
    let mut plex = FakePlex {
        sections: vec![Section {
            key: "1".into(),
            kind: "movie".into(),
            title: "Movies".into(),
        }],
        ..FakePlex::default()
    };
    plex.set("1", TYPE_MOVIE, records);
    plex.set("1", TYPE_EPISODE, vec![]);
    World { dir, plex }
}

fn at(hour: u32) -> Local {
    Local {
        year: 2026,
        month: 10,
        day: 26,
        hour,
        minute: 0,
        second: 0,
        micro: 0,
        offset_secs: 0,
    }
}

struct Run {
    stopped: &'static str,
    indexed: usize,
    failed: usize,
    titles: Vec<String>,
    log: Vec<String>,
    frames: PathBuf,
    media: FakeMedia,
    requests: usize,
}

fn run(
    world: &World,
    answer: Answer,
    options: &Options,
    hours: Option<(u64, u64)>,
    clock: u32,
) -> Run {
    let frames = world.dir.path().join("frames/frames.db");
    let media = FakeMedia {
        scanned: RefCell::new(Vec::new()),
    };
    let encoder = FakeEncoder {
        answer,
        requests: Cell::new(0),
    };
    let locate = |file: &str| -> Result<PathBuf, MediaError> {
        Ok(PathBuf::from("/library").join(file.trim_start_matches('/')))
    };
    let mut log = Vec::new();
    let result = harvest(
        &frames,
        &Inputs {
            plex: &world.plex,
            media: &media,
            encoder: &encoder,
            locate: &locate,
            store_path: &world.dir.path().join("plexdb.db"),
            hours,
            now: &|| at(clock),
        },
        options,
        &mut |line| log.push(line.to_string()),
    )
    .expect("the harvest runs");
    Run {
        stopped: result.stopped,
        indexed: result.indexed,
        failed: result.failed,
        titles: result.titles,
        log,
        frames,
        media,
        requests: encoder.requests.get(),
    }
}

fn now_run(world: &World, answer: Answer) -> Run {
    run(
        world,
        answer,
        &Options {
            ignore_hours: true,
            ..Options::default()
        },
        None,
        3,
    )
}

fn count(conn: &Connection, sql: &str) -> i64 {
    conn.query_row(sql, [], |row| row.get(0)).expect("count")
}

#[test]
fn a_new_title_gets_its_shots_vectors_and_state_and_nothing_is_left_queued_or_in_hand() {
    let world = world(
        &[("tmdb:1", "10", "movie")],
        vec![movie_record("10", 100, 7, "/media/a.mkv")],
    );
    let ran = now_run(&world, Answer::Vectors);
    assert_eq!((ran.stopped, ran.indexed, ran.failed), (STOP_EMPTY, 1, 0));
    assert_eq!(ran.titles, ["tmdb:1"]);

    let conn = open_frames(&ran.frames).expect("open");
    // Two Shots: (0, 3000) is longer than 2000 ms, so a frame at 1000 (the next, 3000,
    // is not inside it); (3000, 5000) is 2000 ms, so one frame at its midpoint.
    assert_eq!(count(&conn, "SELECT count(*) FROM shots"), 2);
    assert_eq!(count(&conn, "SELECT count(*) FROM cuts"), 1);
    assert_eq!(
        count(
            &conn,
            "SELECT count(*) FROM frame_vec WHERE item_id = 'tmdb:1'"
        ),
        2
    );
    let (part, model, dims, quant, duration, bytes, hash, shots, vectors, error): (
        i64,
        String,
        i64,
        String,
        i64,
        i64,
        String,
        i64,
        i64,
        Option<String>,
    ) = conn
        .query_row(
            "SELECT part_id, model, dims, quant, duration_ms, bytes, file_hash, shots, vectors, \
             error FROM frame_index_state WHERE item_id = 'tmdb:1'",
            [],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                    r.get(7)?,
                    r.get(8)?,
                    r.get(9)?,
                ))
            },
        )
        .expect("state");
    assert_eq!(
        (part, model.as_str(), dims, quant.as_str(), duration, bytes),
        (7, "fake-clip-v2", FRAME_DIMS as i64, "int8", 5000, 1007)
    );
    assert_eq!(hash, "hash:/library/media/a.mkv:1007");
    assert_eq!((shots, vectors, error), (2, 2, None));
    assert_eq!(count(&conn, "SELECT count(*) FROM frame_harvest"), 0);
    let line = ran
        .log
        .iter()
        .find(|l| l.contains("reason=new"))
        .expect("a title line");
    assert!(
        line.starts_with("frames: 2026-10-26T03:00:00+00:00 item_id=tmdb:1 part=7 bytes=1007 ")
    );
    assert!(line.contains("shots=2 vectors=2 decode_warnings=2 reason=new"));
}

#[test]
fn queued_titles_come_first_then_a_changed_part_then_new_movies_newest_first() {
    let world = world(
        &[
            ("a", "1", "movie"),
            ("b", "2", "movie"),
            ("c", "3", "movie"),
            ("d", "4", "movie"),
        ],
        vec![
            movie_record("1", 100, 11, "/media/a.mkv"),
            movie_record("2", 300, 12, "/media/b.mkv"),
            movie_record("3", 200, 13, "/media/c.mkv"),
            movie_record("4", 50, 14, "/media/d.mkv"),
        ],
    );
    let conn = open_frames(&world.dir.path().join("frames/frames.db")).expect("create");
    conn.execute(
        "INSERT INTO frame_index_state (item_id, part_id, file_hash, indexed_at) \
         VALUES ('c', 99, 'old', '2026-01-01T00:00:00+00:00')",
        [],
    )
    .expect("an indexed title whose part Plex has replaced");
    conn.execute(
        "INSERT INTO frame_queue (item_id, queued_at, queued_by) VALUES ('d', '2026-02-01', 't')",
        [],
    )
    .expect("queued");
    drop(conn);

    let ran = now_run(&world, Answer::Vectors);
    assert_eq!(ran.titles, ["d", "c", "b", "a"]);
    assert!(
        ran.log
            .iter()
            .any(|l| l == "frames: c: indexed part 99 is not Plex's part 13")
    );
    let reasons: Vec<&str> = ["queued", "part changed", "new"]
        .into_iter()
        .filter(|reason| {
            ran.log
                .iter()
                .any(|l| l.ends_with(&format!("reason={reason}")))
        })
        .collect();
    assert_eq!(reasons, ["queued", "part changed", "new"]);
    let conn = open_frames(&ran.frames).expect("open");
    assert_eq!(count(&conn, "SELECT count(*) FROM frame_queue"), 0);
}

#[test]
fn a_title_whose_file_changed_since_it_was_indexed_is_indexed_again_and_an_unchanged_one_is_not() {
    let world = world(
        &[("a", "1", "movie"), ("b", "2", "movie")],
        vec![
            movie_record("1", 100, 11, "/media/a.mkv"),
            movie_record("2", 100, 12, "/media/b.mkv"),
        ],
    );
    let conn = open_frames(&world.dir.path().join("frames/frames.db")).expect("create");
    for (item, part, hash) in [
        ("a", 11, "hash:/library/media/a.mkv:1011"),
        ("b", 12, "stale"),
    ] {
        conn.execute(
            "INSERT INTO frame_index_state (item_id, part_id, file_hash, indexed_at) \
             VALUES (?1, ?2, ?3, '2026-01-01T00:00:00+00:00')",
            (item, part, hash),
        )
        .expect("state");
    }
    drop(conn);
    let ran = now_run(&world, Answer::Vectors);
    assert_eq!(ran.titles, ["b"]);
    assert!(ran.log.iter().any(|l| l.ends_with("reason=changed")));
    assert_eq!(
        ran.media.scanned.borrow().as_slice(),
        ["/library/media/b.mkv"]
    );
}

#[test]
fn an_unreadable_file_is_recorded_with_its_reason_and_is_not_retried_while_its_part_stands() {
    let world = world(
        &[("a", "1", "movie")],
        vec![movie_record("1", 100, 11, "/media/broken.mkv")],
    );
    let first = now_run(&world, Answer::Vectors);
    assert_eq!((first.indexed, first.failed), (0, 1));
    let conn = open_frames(&first.frames).expect("open");
    let (error, reason, part): (String, String, i64) = conn
        .query_row(
            "SELECT error, reason, part_id FROM frame_index_state WHERE item_id = 'a'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .expect("state");
    assert_eq!(error, "/library/media/broken.mkv: source file damaged");
    assert_eq!(reason, "source file damaged (1 decode errors)");
    assert_eq!(part, 11);
    assert!(
        first
            .log
            .iter()
            .any(|l| l.contains("error='/library/media/broken.mkv: source file damaged'"))
    );

    let second = now_run(&world, Answer::Vectors);
    assert_eq!(second.titles, Vec::<String>::new());
    assert_eq!(second.stopped, STOP_EMPTY);
}

#[test]
fn an_encoder_that_stops_answering_ends_the_run_and_leaves_the_title_untouched() {
    let world = world(
        &[("a", "1", "movie")],
        vec![movie_record("1", 100, 11, "/media/a.mkv")],
    );
    let ran = now_run(&world, Answer::Unavailable);
    assert_eq!((ran.stopped, ran.indexed, ran.failed), (STOP_ENCODER, 0, 0));
    assert!(
        ran.log
            .iter()
            .any(|l| l == "frames: stopping, connection refused")
    );
    let conn = open_frames(&ran.frames).expect("open");
    assert_eq!(count(&conn, "SELECT count(*) FROM frame_index_state"), 0);
    assert_eq!(count(&conn, "SELECT count(*) FROM frame_harvest"), 0);
}

#[test]
fn three_titles_in_a_row_the_encoder_refuses_end_the_run() {
    let titles: Vec<(String, String)> = (1..=4).map(|n| (format!("t{n}"), n.to_string())).collect();
    let rows: Vec<(&str, &str, &str)> = titles
        .iter()
        .map(|(item, key)| (item.as_str(), key.as_str(), "movie"))
        .collect();
    let records = (1..=4)
        .map(|n| movie_record(&n.to_string(), 100 + n, 10 + n, &format!("/media/{n}.mkv")))
        .collect();
    let world = world(&rows, records);
    let ran = now_run(&world, Answer::Rejected);
    assert_eq!(
        (ran.stopped, ran.indexed, ran.failed),
        (STOP_REJECTED, 0, 3)
    );
    assert_eq!(ran.requests, 3);
    let conn = open_frames(&ran.frames).expect("open");
    let reason: String = conn
        .query_row(
            "SELECT reason FROM frame_index_state WHERE item_id = 't4'",
            [],
            |r| r.get(0),
        )
        .expect("newest title tried first");
    assert_eq!(reason, "encoder refused the frames");
}

#[test]
fn vectors_of_the_wrong_width_fail_the_title_rather_than_the_store() {
    let world = world(
        &[("a", "1", "movie")],
        vec![movie_record("1", 100, 11, "/media/a.mkv")],
    );
    let ran = now_run(&world, Answer::NarrowVectors);
    assert_eq!(ran.failed, 1);
    let conn = open_frames(&ran.frames).expect("open");
    let error: String = conn
        .query_row(
            "SELECT error FROM frame_index_state WHERE item_id = 'a'",
            [],
            |r| r.get(0),
        )
        .expect("state");
    assert_eq!(
        error,
        "the encoder returned 3-wide vectors; frame_vec holds 768"
    );
    assert_eq!(count(&conn, "SELECT count(*) FROM frame_vec"), 0);
}

#[test]
fn the_limit_and_the_window_stop_the_run() {
    let world = world(
        &[("a", "1", "movie"), ("b", "2", "movie")],
        vec![
            movie_record("1", 100, 11, "/media/a.mkv"),
            movie_record("2", 200, 12, "/media/b.mkv"),
        ],
    );
    let limited = run(
        &world,
        Answer::Vectors,
        &Options {
            ignore_hours: true,
            limit: Some(1),
            keep_float: false,
        },
        None,
        3,
    );
    assert_eq!((limited.stopped, limited.indexed), (STOP_LIMIT, 1));
    assert_eq!(limited.titles, ["b"]);

    // 01:00 to 07:00, asked at 12:00.
    let closed = run(
        &world,
        Answer::Vectors,
        &Options::default(),
        Some((3_600_000_000, 25_200_000_000)),
        12,
    );
    assert_eq!((closed.stopped, closed.indexed), (STOP_WINDOW, 0));
}

#[test]
fn keep_float_writes_each_vector_beside_the_index() {
    let world = world(
        &[("a", "1", "movie")],
        vec![movie_record("1", 100, 11, "/media/a.mkv")],
    );
    let ran = run(
        &world,
        Answer::Vectors,
        &Options {
            ignore_hours: true,
            keep_float: true,
            ..Options::default()
        },
        None,
        3,
    );
    let float = Connection::open(ran.frames.with_file_name("frames-float.db")).expect("float db");
    assert_eq!(count(&float, "SELECT count(*) FROM frame_float"), 2);
    let blob: Vec<u8> = float
        .query_row(
            "SELECT vec FROM frame_float ORDER BY t_ms LIMIT 1",
            [],
            |r| r.get(0),
        )
        .expect("vector");
    assert_eq!(blob.len(), FRAME_DIMS * 4);
    assert_eq!(&blob[..4], 1.0f32.to_le_bytes());
}

#[test]
fn a_second_harvester_is_refused_while_one_holds_the_slot() {
    let world = world(
        &[("a", "1", "movie")],
        vec![movie_record("1", 100, 11, "/media/a.mkv")],
    );
    let frames = world.dir.path().join("frames/frames.db");
    let _held = plexdb::frames::HarvestLock::acquire(&frames).expect("first slot");
    let media = FakeMedia {
        scanned: RefCell::new(Vec::new()),
    };
    let encoder = FakeEncoder {
        answer: Answer::Vectors,
        requests: Cell::new(0),
    };
    let locate = |file: &str| -> Result<PathBuf, MediaError> { Ok(PathBuf::from(file)) };
    let err = harvest(
        &frames,
        &Inputs {
            plex: &world.plex,
            media: &media,
            encoder: &encoder,
            locate: &locate,
            store_path: &world.dir.path().join("plexdb.db"),
            hours: None,
            now: &|| at(3),
        },
        &Options::default(),
        &mut |_| {},
    )
    .expect_err("the slot is taken");
    assert!(
        err.to_string()
            .starts_with("another harvester is running (pid "),
        "{err}"
    );
}

#[test]
fn a_panic_mid_title_leaves_no_title_in_hand() {
    let world = world(
        &[("a", "1", "movie")],
        vec![movie_record("1", 100, 11, "/media/a.mkv")],
    );
    let frames = world.dir.path().join("frames/frames.db");
    let media = FakeMedia {
        scanned: RefCell::new(Vec::new()),
    };
    let encoder = FakeEncoder {
        answer: Answer::Vectors,
        requests: Cell::new(0),
    };
    let locate = |file: &str| -> Result<PathBuf, MediaError> {
        Ok(PathBuf::from("/library").join(file.trim_start_matches('/')))
    };
    let panicked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = harvest(
            &frames,
            &Inputs {
                plex: &world.plex,
                media: &media,
                encoder: &encoder,
                locate: &locate,
                store_path: &world.dir.path().join("plexdb.db"),
                hours: None,
                now: &|| at(3),
            },
            &Options {
                ignore_hours: true,
                ..Options::default()
            },
            &mut |line| {
                if line.contains("stage=scan start") {
                    panic!("stdout closed");
                }
            },
        );
    }));
    assert!(panicked.is_err(), "the panic still reaches the caller");
    let conn = open_frames(&frames).expect("open");
    assert_eq!(count(&conn, "SELECT count(*) FROM frame_harvest"), 0);
}
