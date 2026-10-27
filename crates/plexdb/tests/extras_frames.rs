//! Extras in the Frame index (ADR-0030): `harvest-frames` takes them in their own order with no
//! Plex part, and `extras-near` folds two encodes of one video into one item and a person can
//! split them again.

mod common;

use std::cell::RefCell;
use std::path::{Path, PathBuf};

use common::{FakePlex, store};
use plexdb::extras_cmd::status_lines;
use plexdb::extras_near::{self, SplitError};
use plexdb::extras_views::{extras_fields, extras_items};
use plexdb::frame_encoder::{Embedded, Encoder, EncoderError};
use plexdb::frame_media::{Cut, Jpegs, MediaError, MediaReader, Scan};
use plexdb::frames::{FRAME_DIMS, open_frames};
use plexdb::harvest_frames::{Inputs, Options, harvest};
use plexdb::localtime::Local;
use plexdb::plex::{Section, TYPE_EPISODE, TYPE_MOVIE};
use rusqlite::Connection;
use serde_json::json;

const T1: &str = "2026-10-27T00:00:00+00:00";

struct Media {
    scanned: RefCell<Vec<String>>,
}

impl MediaReader for Media {
    fn file_hash(&self, path: &Path, size: i64) -> Result<String, MediaError> {
        Ok(format!("hash:{}:{size}", path.display()))
    }

    fn scan(&self, path: &Path) -> Result<Scan, MediaError> {
        self.scanned.borrow_mut().push(path.display().to_string());
        Ok(Scan {
            cuts: vec![Cut {
                t_ms: 3000,
                score: Some(14.5),
                source: "scdet",
            }],
            last_frame_ms: 5000,
            warnings: 0,
        })
    }

    fn frames(&self, _path: &Path, times_ms: &[i64]) -> Result<Jpegs, MediaError> {
        Ok(times_ms
            .iter()
            .map(|t| Some(t.to_string().into_bytes()))
            .collect())
    }
}

struct Encoder1;

impl Encoder for Encoder1 {
    fn model(&self) -> &str {
        "fake-clip"
    }

    fn timed_images(&self, images: &[Vec<u8>]) -> Result<Embedded, EncoderError> {
        Ok(Embedded {
            vectors: images
                .iter()
                .map(|_| {
                    let mut vector = vec![0.0; FRAME_DIMS];
                    vector[0] = 1.0;
                    vector
                })
                .collect(),
            model: "fake-clip".into(),
            first_byte_s: 0.0,
            total_s: 0.0,
        })
    }
}

fn at() -> Local {
    Local {
        year: 2026,
        month: 10,
        day: 27,
        hour: 3,
        minute: 0,
        second: 0,
        micro: 0,
        offset_secs: 0,
    }
}

fn add_extra(
    conn: &Connection,
    item_id: &str,
    organized: i64,
    first_seen: &str,
    path: &str,
    missing: Option<&str>,
) {
    conn.execute(
        "INSERT OR IGNORE INTO extra_library (name, root, type, template, declared_at) \
         VALUES ('Music Videos', '/library/mv', 'music-video', NULL, ?1)",
        [T1],
    )
    .expect("library");
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?1, 'extra', ?1)",
        [item_id],
    )
    .expect("item");
    conn.execute(
        "INSERT INTO extra_item (item_id, library, organized, first_seen, organized_at) \
         VALUES (?1, 'Music Videos', ?2, ?3, ?4)",
        (
            item_id,
            organized,
            first_seen,
            (organized == 1).then_some(first_seen),
        ),
    )
    .expect("extra item");
    conn.execute(
        "INSERT INTO extra_file (path, item_id, primary_file, size, mtime, sha256, first_seen, \
         last_seen, missing_since) VALUES (?1, ?2, 1, 900, 1, ?1, ?3, ?3, ?4)",
        (path, item_id, first_seen, missing),
    )
    .expect("file");
}

fn count(conn: &Connection, sql: &str) -> i64 {
    conn.query_row(sql, [], |row| row.get(0)).expect("count")
}

#[test]
fn extras_are_indexed_without_a_part_and_in_the_order_the_tab_and_plex_set() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES ('tmdb:1', 'movie', 'A')",
        [],
    )
    .expect("movie");
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
         VALUES ('10', 'tmdb:1', '1', '2026-01-01')",
        [],
    )
    .expect("plex item");
    add_extra(
        &conn,
        "fs:organized",
        1,
        "2026-10-20T00:00:00+00:00",
        "/library/mv/o.mkv",
        None,
    );
    add_extra(
        &conn,
        "fs:waiting",
        0,
        "2026-10-21T00:00:00+00:00",
        "/library/mv/w.mkv",
        None,
    );
    add_extra(
        &conn,
        "fs:gone",
        0,
        "2026-10-21T00:00:00+00:00",
        "/library/mv/g.mkv",
        Some(T1),
    );
    add_extra(
        &conn,
        "fs:folded",
        0,
        "2026-10-22T00:00:00+00:00",
        "/library/mv/f.mkv",
        None,
    );
    conn.execute(
        "INSERT INTO extra_merge VALUES ('fs:folded', 'fs:organized', 0.99, ?1)",
        [T1],
    )
    .expect("merge");

    let mut plex = FakePlex {
        sections: vec![Section {
            key: "1".into(),
            kind: "movie".into(),
            title: "Movies".into(),
        }],
        ..FakePlex::default()
    };
    plex.set(
        "1",
        TYPE_MOVIE,
        vec![json!({
            "ratingKey": "10", "addedAt": 100,
            "Media": [{"Part": [{"id": 7, "size": 1007, "file": "/media/a.mkv"}]}]
        })],
    );
    plex.set("1", TYPE_EPISODE, vec![]);
    let media = Media {
        scanned: RefCell::new(Vec::new()),
    };
    let locate = |file: &str| -> Result<PathBuf, MediaError> {
        Ok(PathBuf::from("/disk").join(file.trim_start_matches('/')))
    };
    let frames = dir.path().join("frames/frames.db");
    let result = harvest(
        &frames,
        &Inputs {
            plex: &plex,
            media: &media,
            encoder: &Encoder1,
            locate: &locate,
            store_path: &dir.path().join("plexdb.db"),
            hours: None,
            now: &at,
        },
        &Options {
            ignore_hours: true,
            ..Options::default()
        },
        &mut |_| {},
    )
    .expect("the harvest runs");

    // The waiting extra first, then the Plex movie, then the organized extra; the missing and
    // the folded extras are left alone.
    assert_eq!(result.titles, ["fs:waiting", "tmdb:1", "fs:organized"]);
    assert_eq!(result.indexed, 3);
    assert_eq!(
        media.scanned.borrow().first().map(String::as_str),
        Some("/disk/library/mv/w.mkv")
    );
    let index = open_frames(&frames).expect("open");
    let part: Option<i64> = index
        .query_row(
            "SELECT part_id FROM frame_index_state WHERE item_id = 'fs:waiting'",
            [],
            |row| row.get(0),
        )
        .expect("state");
    assert_eq!(part, None, "an extra has no Plex part");
    let part: Option<i64> = index
        .query_row(
            "SELECT part_id FROM frame_index_state WHERE item_id = 'tmdb:1'",
            [],
            |row| row.get(0),
        )
        .expect("state");
    assert_eq!(part, Some(7));
    assert_eq!(
        count(
            &index,
            "SELECT count(*) FROM frame_index_state WHERE item_id IN ('fs:gone', 'fs:folded')"
        ),
        0
    );
}

/// `n` frames whose int8 vector is `unit` along `axis`.
fn index_extra(frames: &Connection, item_id: &str, axis: usize, n: usize, last_ms: i64, at: &str) {
    for shot in 0..n {
        let mut v = vec![0i32; FRAME_DIMS];
        v[axis] = 100;
        v[(axis + 1) % FRAME_DIMS] = 5 + shot as i32;
        let json = format!(
            "[{}]",
            v.iter().map(i32::to_string).collect::<Vec<_>>().join(",")
        );
        frames
            .execute(
                "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?, ?, ?, vec_int8(?))",
                (item_id, shot as i64 * 2000, shot as i64, json),
            )
            .expect("vector");
    }
    frames
        .execute(
            "INSERT INTO frame_index_state (item_id, model, dims, duration_ms, indexed_at) \
             VALUES (?, 'fake-clip', ?, ?, ?)",
            (item_id, FRAME_DIMS as i64, last_ms, at),
        )
        .expect("state");
    frames
        .execute(
            "INSERT INTO frame_queue VALUES (?, '2026-01-01', 'test')",
            [item_id],
        )
        .expect("queue");
}

struct Near {
    dir: tempfile::TempDir,
    conn: Connection,
    frames_path: PathBuf,
    frames: Connection,
}

fn near_world() -> Near {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    add_extra(
        &conn,
        "fs:first",
        1,
        "2026-10-20T00:00:00+00:00",
        "/library/mv/a.mkv",
        None,
    );
    add_extra(
        &conn,
        "fs:copy",
        0,
        "2026-10-21T00:00:00+00:00",
        "/library/mv/a-720.mkv",
        None,
    );
    add_extra(
        &conn,
        "fs:other",
        0,
        "2026-10-22T00:00:00+00:00",
        "/library/mv/b.mkv",
        None,
    );
    let frames_path = dir.path().join("frames/frames.db");
    let frames = open_frames(&frames_path).expect("frames");
    index_extra(
        &frames,
        "fs:first",
        0,
        4,
        60_000,
        "2026-10-27T01:00:00+00:00",
    );
    index_extra(
        &frames,
        "fs:copy",
        0,
        4,
        60_300,
        "2026-10-27T01:01:00+00:00",
    );
    index_extra(
        &frames,
        "fs:other",
        300,
        4,
        60_000,
        "2026-10-27T01:02:00+00:00",
    );
    Near {
        dir,
        conn,
        frames_path,
        frames,
    }
}

fn pass(w: &mut Near, now: &str) -> extras_near::Stats {
    let readonly = extras_near::open_frames_readonly(&w.frames_path).expect("ro");
    let prepared = extras_near::prepare(&w.conn, &readonly, 0.97).expect("prepare");
    extras_near::apply(&mut w.conn, &prepared, now).expect("apply")
}

#[test]
fn two_encodes_of_one_video_fold_into_the_earlier_item_and_split_restores_both() {
    let mut w = near_world();
    let stats = pass(&mut w, T1);
    assert_eq!(stats.folded, 1);
    assert_eq!(stats.compared, 3);
    let (merged, kept, score): (String, String, f64) = w
        .conn
        .query_row(
            "SELECT merged_item_id, kept_item_id, score FROM extra_merge",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .expect("the one merge");
    assert_eq!((merged.as_str(), kept.as_str()), ("fs:copy", "fs:first"));
    assert!(score >= 0.97, "{score}");

    // Folded frames leave the Frame index; the other two items stay.
    let cleaned = extras_near::clean_frames(&w.frames_path, &w.conn).expect("clean");
    assert_eq!(cleaned, Some(1));
    assert_eq!(
        count(
            &w.frames,
            "SELECT count(*) FROM frame_vec WHERE item_id = 'fs:copy'"
        ),
        0
    );
    assert_eq!(
        count(
            &w.frames,
            "SELECT count(*) FROM frame_index_state WHERE item_id = 'fs:copy'"
        ),
        0
    );
    assert_eq!(count(&w.frames, "SELECT count(*) FROM frame_queue"), 2);
    assert_eq!(
        count(&w.frames, "SELECT count(*) FROM frame_index_state"),
        2
    );

    // A second pass compares nothing and folds nothing.
    let again = pass(&mut w, T1);
    assert_eq!((again.folded, again.compared), (0, 0));

    // The folded item is not an item of its own to consumers, and its file counts for the kept one.
    w.conn
        .execute("UPDATE extra_item SET organized = 1", [])
        .expect("organize");
    let listed = extras_items(&w.conn, None)
        .map(|j| plexdb::pyjson::dumps_compact(&j))
        .expect("items");
    assert!(
        listed.contains("fs:first") && listed.contains("fs:other"),
        "{listed}"
    );
    assert!(!listed.contains("fs:copy"), "{listed}");
    let fields = extras_fields(&w.conn, &["fs:copy".into()])
        .map(|j| plexdb::pyjson::dumps_compact(&j))
        .expect("fields");
    assert!(!fields.contains("fs:copy"), "{fields}");
    assert_eq!(
        status_lines(&w.conn).expect("status"),
        [
            "Music Videos: files 3, items 2, in queue 0, organized 2, missing 0, more than one file 1"
        ]
    );

    // Split deletes the merge, and the item is whole again.
    let mut conn = Connection::open(w.dir.path().join("plexdb.db")).expect("open");
    extras_near::split(&mut conn, "fs:copy", T1).expect("split");
    assert_eq!(count(&conn, "SELECT count(*) FROM extra_merge"), 0);
    let listed = extras_items(&conn, None)
        .map(|j| plexdb::pyjson::dumps_compact(&j))
        .expect("items");
    assert!(listed.contains("fs:copy"), "{listed}");
    assert_eq!(
        status_lines(&conn).expect("status"),
        [
            "Music Videos: files 3, items 3, in queue 0, organized 3, missing 0, more than one file 0"
        ]
    );
    assert!(matches!(
        extras_near::split(&mut conn, "fs:copy", T1),
        Err(SplitError::NotMerged(_))
    ));

    // Indexed again, it is not folded again: a person kept it apart.
    index_extra(
        &w.frames,
        "fs:copy",
        0,
        4,
        60_300,
        "2026-10-28T01:00:00+00:00",
    );
    w.conn = conn;
    let after = pass(&mut w, T1);
    assert_eq!((after.folded, after.compared), (0, 1));
}

#[test]
fn a_video_of_another_length_or_other_frames_is_not_folded() {
    let mut w = near_world();
    w.frames
        .execute(
            "UPDATE frame_index_state SET duration_ms = 90000 WHERE item_id = 'fs:copy'",
            [],
        )
        .expect("longer");
    let stats = pass(&mut w, T1);
    assert_eq!(stats.folded, 0);
    assert_eq!(count(&w.conn, "SELECT count(*) FROM extra_merge"), 0);
}

#[test]
fn an_item_organized_after_the_comparison_is_never_hidden_behind_an_unorganized_one() {
    let mut w = near_world();
    w.conn
        .execute("UPDATE extra_item SET organized = 0", [])
        .expect("queue all");
    let readonly = extras_near::open_frames_readonly(&w.frames_path).expect("ro");
    let prepared = extras_near::prepare(&w.conn, &readonly, 0.97).expect("prepare");
    assert_eq!(prepared.pairs.len(), 1);
    w.conn
        .execute(
            "UPDATE extra_item SET organized = 1 WHERE item_id = 'fs:copy'",
            [],
        )
        .expect("a person organizes the later item");
    let stats = extras_near::apply(&mut w.conn, &prepared, T1).expect("apply");
    assert_eq!(stats.folded, 0);
    assert_eq!(count(&w.conn, "SELECT count(*) FROM extra_merge"), 0);
}
