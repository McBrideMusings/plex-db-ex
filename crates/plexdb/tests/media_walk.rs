//! `media-walk` records parts and streams from a recorded `/library/metadata`
//! response, and a second run over an unchanged `updatedAt` makes no request.

mod common;

use common::{FakePlex, fixture, one, recorded, store};
use plexdb::media::media_walk;
use plexdb::walk::walk_all;
use rusqlite::Connection;
use serde_json::{Value, json};

type Stream = (
    i64,
    String,
    Option<String>,
    Option<String>,
    i64,
    Option<String>,
);

const RATING_KEY: &str = "13714";
const ROOTS: [&str; 1] = ["/media"];

/// The recorded library with every title's `updatedAt` set, and the recorded full
/// record for the one title that has one. Every other title answers with the same
/// single part under a part id of its own.
fn source(updated_at: i64) -> FakePlex {
    let mut plex = recorded();
    for records in plex.records.values_mut() {
        for record in records {
            record["updatedAt"] = json!(updated_at);
        }
    }
    let full = fixture("plex/metadata_13714.json")["MediaContainer"]["Metadata"][0].clone();
    let keys: Vec<String> = plex
        .records
        .values()
        .flatten()
        .filter_map(|record| record["ratingKey"].as_str().map(String::from))
        .collect();
    for key in keys {
        let mut record = full.clone();
        if key != RATING_KEY {
            record["Media"][0]["Part"][0]["id"] = json!(key.parse::<i64>().expect("numeric"));
        }
        plex.full.insert(key, record);
    }
    plex
}

fn walked_store(dir: &std::path::Path) -> Connection {
    let mut conn = store(dir);
    let roots: Vec<String> = ROOTS.iter().map(|root| (*root).into()).collect();
    walk_all(&mut conn, &source(1_760_000_000), &roots, None).expect("walk");
    conn
}

#[test]
fn the_recorded_response_writes_the_part_and_its_streams() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());
    let plex = source(1_760_000_000);

    let stats = media_walk(&mut conn, &plex, None).expect("media walk");

    assert!(
        plex.metadata_requests
            .borrow()
            .iter()
            .any(|key| key == RATING_KEY)
    );
    assert_eq!(stats.missing, 0);
    assert_eq!(stats.parts_written, stats.titles_listed);
    let part: (
        String,
        String,
        i64,
        String,
        String,
        String,
        i64,
        i64,
        i64,
        i64,
    ) = conn
        .query_row(
            "SELECT rating_key, item_id, size, container, video_codec, audio_codec, height, \
             bitrate, duration_ms, plex_updated_at FROM media_part WHERE part_id = 31001",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                    row.get(8)?,
                    row.get(9)?,
                ))
            },
        )
        .expect("the part");
    assert_eq!(
        part,
        (
            RATING_KEY.into(),
            "imdb:tt0096734".into(),
            7_021_345_678,
            "mkv".into(),
            "h264".into(),
            "dts".into(),
            1080,
            9200,
            6_100_386,
            1_760_000_000
        )
    );
    let streams: Vec<Stream> = conn
        .prepare("SELECT idx, kind, codec, lang, forced, title FROM media_stream WHERE part_id = 31001 ORDER BY idx")
        .expect("prepare")
        .query_map([], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?))
        })
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows");
    let some = |text: &str| Some(text.to_string());
    assert_eq!(
        streams,
        [
            (0, "video".into(), some("h264"), None, 0, None),
            (
                1,
                "audio".into(),
                some("dts"),
                some("eng"),
                0,
                some("English (DTS 5.1)")
            ),
            (
                2,
                "audio".into(),
                some("ac3"),
                some("fra"),
                0,
                some("French (AC3 5.1)")
            ),
            (
                3,
                "subtitle".into(),
                some("srt"),
                some("eng"),
                1,
                some("English (SRT Forced)")
            ),
            (
                4,
                "subtitle".into(),
                some("srt"),
                some("spa"),
                0,
                some("Spanish (SRT)")
            ),
        ]
    );
}

#[test]
fn a_second_run_with_an_unchanged_updated_at_fetches_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());
    media_walk(&mut conn, &source(1_760_000_000), None).expect("first run");

    let again = source(1_760_000_000);
    let stats = media_walk(&mut conn, &again, None).expect("second run");

    assert!(again.metadata_requests.borrow().is_empty());
    assert_eq!(stats.fetched, 0);
    assert!(stats.titles_listed > 1);
    assert_eq!(stats.unchanged, stats.titles_listed);
}

#[test]
fn a_changed_updated_at_replaces_the_streams_and_drops_vanished_parts() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());
    media_walk(&mut conn, &source(1_760_000_000), None).expect("first run");

    let mut changed = source(1_770_000_000);
    let full = changed.full.get_mut(RATING_KEY).expect("the full record");
    let part = &mut full["Media"][0]["Part"][0];
    let kept: Vec<Value> = part["Stream"].as_array().expect("streams")[..2].to_vec();
    part["Stream"] = json!(kept);
    part["id"] = json!(31002);
    media_walk(&mut conn, &changed, None).expect("second run");

    assert_eq!(
        one::<i64>(
            &conn,
            &format!("SELECT part_id FROM media_part WHERE rating_key = '{RATING_KEY}'")
        ),
        31002
    );
    assert_eq!(
        one::<i64>(
            &conn,
            "SELECT COUNT(*) FROM media_stream WHERE part_id IN (31001, 31002)"
        ),
        2
    );
    assert_eq!(
        one::<i64>(
            &conn,
            "SELECT COUNT(DISTINCT plex_updated_at) FROM media_part"
        ),
        1
    );
    assert_eq!(
        one::<i64>(&conn, "SELECT MAX(plex_updated_at) FROM media_part"),
        1_770_000_000
    );
}

#[test]
fn a_title_whose_listing_names_no_file_loses_its_parts_without_a_request() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());
    media_walk(&mut conn, &source(1_760_000_000), None).expect("first run");

    let mut emptied = source(1_770_000_000);
    let movies = emptied.records_mut("1", 1);
    let listed = movies.len();
    for record in movies {
        record["Media"] = json!([]);
    }
    let stats = media_walk(&mut conn, &emptied, None).expect("second run");

    assert_eq!(stats.emptied, listed);
    assert!(
        !emptied
            .metadata_requests
            .borrow()
            .iter()
            .any(|key| key == RATING_KEY)
    );
    assert_eq!(
        one::<i64>(
            &conn,
            &format!("SELECT COUNT(*) FROM media_part WHERE rating_key = '{RATING_KEY}'")
        ),
        0
    );
    assert_eq!(
        one::<i64>(
            &conn,
            "SELECT COUNT(*) FROM media_stream WHERE part_id = 31001"
        ),
        0
    );
}
