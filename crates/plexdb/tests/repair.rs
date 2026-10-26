//! `repair-identities`: splitting identities that fused two unrelated titles
//! (issue #23), against a store built by the real `plexdb migrate`.

mod common;

use common::{FakePlex, movies, one, store};
use plexdb::plex::{Section, TYPE_EPISODE, TYPE_SHOW};
use plexdb::repair::{fused_item_ids, kinds_by_rating_key, repair};
use plexdb::walk::walk_all;
use rusqlite::Connection;
use serde_json::{Value, json};

const FUSED_ID: &str = "imdb:tt0047034";

fn godzilla() -> Value {
    json!({
        "ratingKey": "5550", "type": "movie", "title": "Godzilla", "year": 1954,
        "Guid": [{"id": "imdb://tt0047034"}, {"id": "tmdb://1678"}, {"id": "tvdb://5015"}],
        "Media": [{"Part": [{"file": "/media/movies/Godzilla (1954)/g.mkv"}]}],
    })
}

fn golden_girls() -> Value {
    json!({
        "ratingKey": "141718", "type": "show", "title": "The Golden Girls", "year": 1985,
        "key": "/library/metadata/141718",
        "Guid": [{"id": "imdb://tt0088526"}, {"id": "tmdb://1678"}, {"id": "tvdb://71292"}],
    })
}

fn library() -> FakePlex {
    let mut plex = movies(vec![godzilla()]);
    plex.sections.push(Section {
        key: "2".into(),
        kind: "show".into(),
        title: "TV Shows".into(),
    });
    plex.set("2", TYPE_SHOW, vec![golden_girls()]);
    plex.set("2", TYPE_EPISODE, vec![]);
    plex
}

/// One identity carrying both titles' ids and both titles' rating keys, the state a store
/// held when `external_ids` was keyed on `(ns, value)` alone.
fn fused(dir: &std::path::Path, with_play: bool) -> Connection {
    let conn = store(dir);
    conn.execute(
        "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', 'Godzilla', 1954)",
        [FUSED_ID],
    )
    .expect("item");
    for (ns, value) in [
        ("imdb", "tt0047034"),
        ("imdb", "tt0088526"),
        ("tmdb", "1678"),
        ("tvdb", "5015"),
        ("tvdb", "71292"),
    ] {
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) \
             VALUES (?, ?, ?, 'movie', '2024-01-01T00:00:00+00:00')",
            (FUSED_ID, ns, value),
        )
        .expect("external id");
    }
    for (rating_key, section) in [("5550", "1"), ("141718", "2")] {
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
             VALUES (?, ?, ?, '2024-01-01T00:00:00+00:00')",
            (rating_key, FUSED_ID, section),
        )
        .expect("rating key");
    }
    if with_play {
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES ('h1', ?, 1, 1000)",
            [FUSED_ID],
        )
        .expect("play");
        conn.execute(
            "INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, 9000)",
            [],
        )
        .expect("cursor");
    }
    conn
}

fn fused_ids(conn: &Connection, plex: &FakePlex) -> Vec<String> {
    fused_item_ids(conn, &kinds_by_rating_key(plex).expect("kinds")).expect("fused")
}

#[test]
fn an_identity_covering_a_movie_and_a_show_is_split_into_the_two_titles_plex_reports() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = fused(dir.path(), false);
    assert_eq!(fused_ids(&conn, &library()), [FUSED_ID]);

    let stats = repair(&mut conn, &library(), &[]).expect("repair");

    assert_eq!(stats.fused_found, 1);
    assert_eq!(
        common::texts(
            &conn,
            "SELECT rating_key || ' ' || item_id FROM plex_items ORDER BY 1"
        ),
        ["141718 imdb:tt0088526", "5550 imdb:tt0047034"]
    );
    assert_eq!(
        common::texts(
            &conn,
            "SELECT item_id || ' ' || title FROM items ORDER BY 1"
        ),
        ["imdb:tt0047034 Godzilla", "imdb:tt0088526 The Golden Girls"]
    );
    assert!(fused_ids(&conn, &library()).is_empty());
    let again = repair(&mut conn, &library(), &[]).expect("second repair");
    assert_eq!(again.fused_found, 0, "a repaired store is a fixed point");
}

#[test]
fn a_duplicate_title_or_a_second_id_on_one_kind_is_not_a_fusion() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    // The same episode twice, matched to different IMDb entries but one TVDB id: version 5
    // merges them, and flagging them would re-walk and drop their plays on every run.
    let episode = |rating_key: &str, imdb: &str, file: &str| {
        json!({
            "ratingKey": rating_key, "type": "episode", "title": "Twisted Christian",
            "grandparentTitle": "South Park", "parentIndex": 28, "index": 1,
            "Guid": [{"id": imdb}, {"id": "tvdb://11256175"}],
            "Media": [{"Part": [{"file": file}]}],
        })
    };
    let mut plex = FakePlex {
        sections: vec![Section {
            key: "2".into(),
            kind: "show".into(),
            title: "TV Shows".into(),
        }],
        ..FakePlex::default()
    };
    plex.set("2", TYPE_SHOW, vec![]);
    plex.set(
        "2",
        TYPE_EPISODE,
        vec![
            episode("139312", "imdb://tt38043385", "/media/tv/South Park/a.mkv"),
            episode("139321", "imdb://tt26764428", "/media/tv/South Park/b.mkv"),
        ],
    );
    walk_all(&mut conn, &plex, &[], None).expect("walk");
    assert_eq!(
        one::<i64>(&conn, "SELECT COUNT(DISTINCT item_id) FROM plex_items"),
        1
    );

    assert!(fused_ids(&conn, &plex).is_empty());
}

#[test]
fn a_play_follows_its_own_rating_key_and_a_play_without_one_is_dropped_with_the_cursor_rewound() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = fused(dir.path(), true);
    // A pre-v5 play carries no rating key; these two do, one for each half.
    conn.execute(
        "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at, seconds_watched) \
         VALUES ('movie', ?, '5550', 1, 1000, 5400)",
        [FUSED_ID],
    )
    .expect("movie play");
    conn.execute(
        "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at) \
         VALUES ('show', ?, '141718', 1, 2000)",
        [FUSED_ID],
    )
    .expect("show play");

    let stats = repair(&mut conn, &library(), &[]).expect("repair");

    assert_eq!(stats.plays_dropped, 1);
    // The movie's play already sat on the movie's id: only the show's travelled.
    assert_eq!(stats.plays_repointed, 1);
    assert_eq!(stats.cursor_rewound_to, Some(1000));
    assert_eq!(
        one::<i64>(
            &conn,
            "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1"
        ),
        1000
    );
    assert_eq!(
        common::texts(
            &conn,
            "SELECT history_key || ' ' || item_id FROM plays ORDER BY 1"
        ),
        ["movie imdb:tt0047034", "show imdb:tt0088526"]
    );
    assert_eq!(
        one::<i64>(
            &conn,
            "SELECT seconds_watched FROM plays WHERE history_key = 'movie'"
        ),
        5400
    );
}

#[test]
fn a_play_whose_rating_key_left_the_library_stays_where_it_is() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = fused(dir.path(), false);
    conn.execute(
        "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at) \
         VALUES ('gone', ?, '999999', 1, 1000)",
        [FUSED_ID],
    )
    .expect("play");

    let stats = repair(&mut conn, &library(), &[]).expect("repair");

    assert_eq!(
        (
            stats.plays_orphaned,
            stats.plays_repointed,
            stats.plays_dropped
        ),
        (1, 0, 0)
    );
    assert_eq!(
        common::texts(
            &conn,
            "SELECT item_id FROM plays WHERE history_key = 'gone'"
        ),
        [FUSED_ID]
    );
    assert_eq!(
        one::<i64>(
            &conn,
            &format!("SELECT COUNT(*) FROM items WHERE item_id = '{FUSED_ID}'")
        ),
        1,
        "the identity holding an unplaceable play is kept, or the play goes with it"
    );
}

#[test]
fn a_pass_that_died_before_moving_the_plays_is_finished_by_the_next_run() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    walk_all(&mut conn, &library(), &[], None).expect("walk");
    // The state an interrupted run leaves: the play names its rating key but sits elsewhere.
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES ('stale', 'movie', 'Stale')",
        [],
    )
    .expect("item");
    conn.execute(
        "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at) \
         VALUES ('orphan', 'stale', '141718', 1, 1000)",
        [],
    )
    .expect("play");

    let stats = repair(&mut conn, &library(), &[]).expect("repair");

    assert_eq!((stats.fused_found, stats.plays_repointed), (0, 1));
    assert_eq!(
        common::texts(&conn, "SELECT item_id FROM plays"),
        ["imdb:tt0088526"]
    );
}
