//! Ingesting Plex watch history into `plays` and enriching it from Tautulli
//! (ADR-0004, ADR-0010), against a store built by the real `plexdb migrate`.

mod common;

use common::{FakePlex, FakeTautulli, one, recorded, store};
use plexdb::plays::{
    AccountIdMap, MATCH_WINDOW_SECONDS, build_account_id_map, ingest_plays, match_tautulli_history,
};
use plexdb::walk::walk_all;
use rusqlite::Connection;
use serde_json::{Value, json};

fn walked_store(dir: &std::path::Path) -> Connection {
    let mut conn = store(dir);
    walk_all(&mut conn, &recorded(), &[], None).expect("walk");
    conn
}

fn play_keys(conn: &Connection) -> Vec<String> {
    common::texts(conn, "SELECT history_key FROM plays ORDER BY history_key")
}

#[test]
fn history_becomes_one_play_per_resolvable_event() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());

    let stats = ingest_plays(&mut conn, &recorded()).expect("ingest");

    assert_eq!(
        (
            stats.events_seen,
            stats.plays_written,
            stats.already_recorded,
            stats.unresolved_rating_key,
            stats.unresolved_device
        ),
        (4, 3, 0, 1, 1)
    );
    assert_eq!(
        play_keys(&conn),
        [
            "/status/sessions/history/1998",
            "/status/sessions/history/2000",
            "/status/sessions/history/2001"
        ]
    );
    let burbs: (String, i64, String, String, i64, Option<String>) = conn
        .query_row(
            "SELECT item_id, plex_account_id, client_identifier, platform, viewed_at, ip \
             FROM plays WHERE history_key = '/status/sessions/history/2001'",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .expect("the play");
    assert_eq!(
        burbs,
        (
            "imdb:tt0096734".into(),
            1,
            "device-alpha-001".into(),
            "Roku".into(),
            1_700_000_400,
            None
        )
    );
    // An empty clientIdentifier identifies nothing; an unknown device is no device.
    let blank: (Option<String>, Option<String>) = conn
        .query_row(
            "SELECT client_identifier, platform FROM plays WHERE history_key = '/status/sessions/history/2000'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .expect("the play");
    assert_eq!(blank, (None, Some("Chromecast".into())));
    let unknown: (Option<String>, Option<String>) = conn
        .query_row(
            "SELECT client_identifier, platform FROM plays WHERE history_key = '/status/sessions/history/1998'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .expect("the play");
    assert_eq!(unknown, (None, None));
}

#[test]
fn re_reported_events_add_no_duplicate_rows() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());
    let mut plex = recorded();
    plex.ignore_cutoff = true;

    let first = ingest_plays(&mut conn, &plex).expect("first ingest");
    let second = ingest_plays(&mut conn, &plex).expect("second ingest");

    assert_eq!(first.plays_written, 3);
    assert_eq!(
        (
            second.events_seen,
            second.plays_written,
            second.already_recorded
        ),
        (4, 0, 3)
    );
    assert_eq!(one::<i64>(&conn, "SELECT count(*) FROM plays"), 3);
}

#[test]
fn the_cursor_stays_at_the_oldest_unresolved_event_until_a_walk_resolves_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());

    let first = recorded();
    ingest_plays(&mut conn, &first).expect("first ingest");
    assert_eq!(
        *first.history_cutoffs.borrow(),
        [None],
        "an empty store asks for it all"
    );

    // The newest event wrote, but an older one did not resolve: the next cutoff is pinned
    // at the older one, or it would fall below the next fetch's floor and never return.
    let second = recorded();
    let stats = ingest_plays(&mut conn, &second).expect("second ingest");
    assert_eq!(*second.history_cutoffs.borrow(), [Some(1_699_999_000)]);
    assert_eq!(stats.unresolved_rating_key, 1);

    // A later walk adds the rating key; the event is picked up and the cursor moves on.
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
         VALUES ('999999', 'imdb:tt0096734', '1', '2024-01-01T00:00:00+00:00')",
        [],
    )
    .expect("the later walk");
    let third = ingest_plays(&mut conn, &recorded()).expect("third ingest");
    assert_eq!((third.unresolved_rating_key, third.plays_written), (0, 1));
    assert_eq!(
        one::<i64>(
            &conn,
            "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1"
        ),
        1_700_000_400
    );
}

#[test]
fn a_history_with_no_events_writes_nothing_and_leaves_the_cursor() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = walked_store(dir.path());

    let stats = ingest_plays(&mut conn, &FakePlex::default()).expect("ingest");

    assert_eq!((stats.events_seen, stats.plays_written), (0, 0));
    assert_eq!(
        one::<i64>(&conn, "SELECT count(*) FROM plays_ingest_cursor"),
        0
    );
}

fn seed(conn: &Connection, history_key: &str, account: i64, client: Option<&str>, viewed_at: i64) {
    conn.execute(
        "INSERT OR IGNORE INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A Title')",
        [],
    )
    .expect("item");
    conn.execute(
        "INSERT OR IGNORE INTO plex_items (rating_key, item_id, section_id, last_seen) \
         VALUES ('9001', 'imdb:tt1', '1', '2024-01-01T00:00:00+00:00')",
        [],
    )
    .expect("rating key");
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, viewed_at) \
         VALUES (?, 'imdb:tt1', ?, ?, ?)",
        (history_key, account, client, viewed_at),
    )
    .expect("play");
}

fn row(
    id: Option<i64>,
    user_id: i64,
    machine: &str,
    stopped: f64,
    duration: i64,
    paused: i64,
) -> Value {
    json!({
        "id": id, "rating_key": 9001, "user_id": user_id, "machine_id": machine,
        "stopped": stopped, "duration": duration, "paused_counter": paused,
        "percent_complete": 90, "ip_address": "203.0.113.10",
    })
}

fn tautulli(rows: Vec<Value>) -> FakeTautulli {
    FakeTautulli {
        rows,
        users: vec![],
    }
}

type TautulliColumns = (
    Option<String>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
);

fn tautulli_columns(conn: &Connection, history_key: &str) -> TautulliColumns {
    conn.query_row(
        "SELECT ip, percent_complete, paused_counter, seconds_watched, tautulli_id \
         FROM plays WHERE history_key = ?",
        [history_key],
        |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        },
    )
    .expect("the play")
}

#[test]
fn a_matched_row_writes_ip_completion_paused_time_and_duration_verbatim() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "h1", 1, Some("device-alpha-001"), 1_700_000_400);

    let stats = match_tautulli_history(
        &mut conn,
        &tautulli(vec![row(
            Some(9001),
            1,
            "device-alpha-001",
            1_700_000_450.0,
            2361,
            1167,
        )]),
        &AccountIdMap::default(),
    )
    .expect("match");

    assert_eq!(
        (
            stats.rows_seen,
            stats.rows_matched,
            stats.plays_without_tautulli_data
        ),
        (1, 1, 0)
    );
    // `duration` is already net of paused time: subtracting `paused_counter` would do it twice.
    assert_eq!(
        tautulli_columns(&conn, "h1"),
        (
            Some("203.0.113.10".into()),
            Some(90),
            Some(1167),
            Some(2361),
            Some(9001)
        )
    );
}

#[test]
fn the_window_includes_its_boundary_and_excludes_one_second_past_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "h1", 1, Some("c"), 1_700_000_400);
    let at = |offset: f64| tautulli(vec![row(Some(1), 1, "c", 1_700_000_400.0 + offset, 10, 0)]);

    let outside = match_tautulli_history(
        &mut conn,
        &at(MATCH_WINDOW_SECONDS + 1.0),
        &AccountIdMap::default(),
    )
    .expect("outside");
    assert_eq!(
        (
            outside.rows_matched,
            outside.rows_unmatched,
            outside.plays_without_tautulli_data
        ),
        (0, 1, 1)
    );

    let boundary = match_tautulli_history(
        &mut conn,
        &at(MATCH_WINDOW_SECONDS),
        &AccountIdMap::default(),
    )
    .expect("boundary");
    assert_eq!(boundary.rows_matched, 1);
}

#[test]
fn an_in_progress_row_is_skipped_and_a_second_run_changes_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "h1", 1, Some("c"), 1_700_000_400);

    let pending = match_tautulli_history(
        &mut conn,
        &tautulli(vec![row(None, 1, "c", 1_700_000_450.0, 10, 0)]),
        &AccountIdMap::default(),
    )
    .expect("pending");
    assert_eq!((pending.rows_in_progress, pending.rows_unmatched), (1, 0));

    let done = tautulli(vec![row(Some(9001), 1, "c", 1_700_000_450.0, 10, 0)]);
    let first = match_tautulli_history(&mut conn, &done, &AccountIdMap::default()).expect("first");
    let second =
        match_tautulli_history(&mut conn, &done, &AccountIdMap::default()).expect("second");
    assert_eq!(
        (
            first.rows_matched,
            second.rows_matched,
            second.rows_already_matched
        ),
        (1, 0, 1)
    );
}

#[test]
fn the_nearest_play_wins_and_a_claimed_play_is_not_taken_by_another_row() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "far", 1, Some("c"), 1_700_000_000);
    seed(&conn, "near", 1, Some("c"), 1_700_000_400);

    match_tautulli_history(
        &mut conn,
        &tautulli(vec![row(Some(1), 1, "c", 1_700_000_450.0, 10, 0)]),
        &AccountIdMap::default(),
    )
    .expect("first row");
    assert_eq!(tautulli_columns(&conn, "near").4, Some(1));
    assert_eq!(tautulli_columns(&conn, "far").4, None);

    // A second Tautulli row for the same moment finds "near" claimed, and takes the
    // farther play only if it is inside the window.
    let stats = match_tautulli_history(
        &mut conn,
        &tautulli(vec![row(Some(2), 1, "c", 1_700_000_450.0, 10, 0)]),
        &AccountIdMap::default(),
    )
    .expect("second row");
    assert_eq!((stats.rows_matched, stats.rows_unmatched), (1, 0));
    assert_eq!(tautulli_columns(&conn, "far").4, Some(2));
    assert_eq!(
        tautulli_columns(&conn, "near").4,
        Some(1),
        "the claimed play keeps its row"
    );
}

#[test]
fn a_null_machine_id_matches_a_play_with_no_client_identifier() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "h1", 1, None, 1_700_000_400);

    let stats = match_tautulli_history(
        &mut conn,
        &tautulli(vec![row(Some(5), 1, "", 1_700_000_450.0, 10, 0)]),
        &AccountIdMap::default(),
    )
    .expect("match");

    assert_eq!(stats.rows_matched, 1);
}

#[test]
fn the_owners_differing_id_is_resolved_by_a_join_on_account_name() {
    let accounts = [
        json!({"id": 0, "name": ""}),
        json!({"id": 1, "name": "owner"}),
        json!({"id": 7, "name": "guest"}),
        json!({"id": 9, "name": "plex-only"}),
    ];
    let users = [
        json!({"user_id": 0, "username": "Local"}),
        json!({"user_id": 4_242_424, "username": "owner"}),
        json!({"user_id": 7, "username": "guest"}),
        json!({"user_id": 8, "username": "tautulli-only"}),
    ];

    let map = build_account_id_map(&accounts, &users);

    assert_eq!(map.by_tautulli_user_id.get(&4_242_424), Some(&1));
    assert_eq!(map.by_tautulli_user_id.get(&7), Some(&7));
    assert_eq!(
        map.by_tautulli_user_id.len(),
        2,
        "id 0 never pairs with id 0"
    );
    assert_eq!(map.tautulli_only_names, ["tautulli-only"]);
    assert_eq!(map.plex_only_names, ["plex-only"]);

    // A row under the owner's plex.tv id matches the play stored under local account 1.
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "h1", 1, Some("c"), 1_700_000_400);
    let source = tautulli(vec![row(Some(3), 4_242_424, "c", 1_700_000_450.0, 10, 0)]);
    let without =
        match_tautulli_history(&mut conn, &source, &AccountIdMap::default()).expect("without map");
    assert_eq!(without.rows_matched, 0);
    let with = match_tautulli_history(&mut conn, &source, &map).expect("with map");
    assert_eq!(with.rows_matched, 1);
}
