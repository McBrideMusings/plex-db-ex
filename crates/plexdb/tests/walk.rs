//! `walk` against a store built by the real `plexdb migrate` and a recorded Plex: the
//! identity a title keeps through every way Plex can change under it (ADR-0008).

mod common;

use common::{FakePlex, movie, movies, one, recorded, store, texts};
use plexdb::identity::{canonical_path, derive_item_id};
use plexdb::plex::{Section, TYPE_EPISODE, TYPE_SHOW};
use plexdb::walk::{FoundBy, walk_all, walk_at};
use rusqlite::Connection;
use serde_json::{Value, json};

fn walked(conn: &mut Connection, plex: &FakePlex) -> plexdb::walk::WalkStats {
    walk_all(conn, plex, &[], None).expect("walk")
}

fn mapping(conn: &Connection) -> Vec<(String, String)> {
    conn.prepare("SELECT rating_key, item_id FROM plex_items ORDER BY rating_key")
        .expect("prepare")
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn pair(a: &str, b: &str) -> (String, String) {
    (a.into(), b.into())
}

#[test]
fn a_recorded_library_walks_into_items_ids_and_the_rating_key_map() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let stats = walked(&mut conn, &recorded());

    assert_eq!(
        (
            stats.sections_walked,
            stats.titles_seen,
            stats.titles_written,
            stats.fallback_to_path
        ),
        (4, 6, 6, 1)
    );
    let burbs: (String, String, i64, i64) = conn
        .query_row(
            "SELECT title_sort, studio, year, duration_ms FROM items WHERE item_id = 'imdb:tt0096734'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .expect("the movie");
    assert_eq!(
        burbs,
        (
            "'Burbs".into(),
            "Universal Pictures".into(),
            1989,
            6_100_386
        )
    );
    let episode: (String, String, i64, i64, Option<String>) = conn
        .query_row(
            "SELECT show_title, show_item_id, season, episode, studio FROM items \
             WHERE item_id = 'imdb:tt18469978'",
            [],
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
        .expect("the episode");
    assert_eq!(
        episode,
        ("1923".into(), "imdb:tt18335752".into(), 1, 1, None),
        "an episode's studio is the one Plex sent on it: none"
    );
    // A title with no recognised GUID lands under the hash of its path.
    let air_mater = derive_item_id(
        &[],
        &canonical_path(
            "/media/movies/Air Mater (2011) {imdb-tt2100093}/Air Mater (2011).mkv",
            &[],
        ),
    );
    assert!(air_mater.starts_with("fs:"));
    assert_eq!(
        one::<i64>(
            &conn,
            &format!("SELECT count(*) FROM external_ids WHERE item_id = '{air_mater}'")
        ),
        0
    );
    assert_eq!(
        texts(&conn, "SELECT DISTINCT type FROM items ORDER BY type"),
        ["episode", "movie", "show"]
    );
}

#[test]
fn a_second_walk_over_an_unchanged_library_changes_no_row() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    walked(&mut conn, &recorded());
    let before = (
        mapping(&conn),
        one::<i64>(&conn, "SELECT count(*) FROM items"),
    );

    let again = walked(&mut conn, &recorded());

    assert_eq!(
        (
            mapping(&conn),
            one::<i64>(&conn, "SELECT count(*) FROM items")
        ),
        before
    );
    assert!(again.kept_by(FoundBy::RatingKey).is_empty());
}

#[test]
fn a_rematched_title_keeps_its_identity_and_records_both_ids() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let file = "/media/movies/Test Movie (2000)/test.mkv";
    walked(
        &mut conn,
        &movies(vec![movie("999", "Test Movie", "imdb://tt1111111", file)]),
    );

    // Plex re-matches the same rating key to a different IMDb id.
    let stats = walked(
        &mut conn,
        &movies(vec![movie("999", "Test Movie", "imdb://tt2222222", file)]),
    );

    assert_eq!(stats.kept_by(FoundBy::RatingKey).len(), 1);
    assert_eq!(mapping(&conn), [pair("999", "imdb:tt1111111")]);
    assert_eq!(one::<i64>(&conn, "SELECT count(*) FROM items"), 1);
    assert_eq!(
        texts(
            &conn,
            "SELECT value FROM external_ids WHERE item_id = 'imdb:tt1111111' ORDER BY value"
        ),
        ["tt1111111", "tt2222222"]
    );
}

#[test]
fn a_re_added_title_with_the_same_guids_maps_both_rating_keys_to_one_identity() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let file = "/media/movies/Test Movie (2000)/test.mkv";
    walked(
        &mut conn,
        &movies(vec![movie("500", "Test Movie", "imdb://tt3333333", file)]),
    );

    let stats = walked(
        &mut conn,
        &movies(vec![movie("501", "Test Movie", "imdb://tt3333333", file)]),
    );

    assert_eq!(
        mapping(&conn),
        [pair("500", "imdb:tt3333333"), pair("501", "imdb:tt3333333")]
    );
    assert_eq!(one::<i64>(&conn, "SELECT count(*) FROM items"), 1);
    assert!(stats.identities_kept.is_empty());
}

#[test]
fn a_gained_higher_priority_guid_does_not_move_a_title_off_the_id_it_already_has() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let file = "/media/movies/Test Movie 2 (2000)/test.mkv";
    walked(
        &mut conn,
        &movies(vec![movie("600", "Test Movie", "tvdb://4444", file)]),
    );

    let mut gained = movie("600", "Test Movie", "tmdb://12345", file);
    gained["Guid"] = json!([{"id": "tmdb://12345"}, {"id": "tvdb://4444"}]);
    let stats = walked(&mut conn, &movies(vec![gained]));

    assert_eq!(mapping(&conn), [pair("600", "tvdb:4444")]);
    assert_eq!(stats.kept_by(FoundBy::ExternalId).len(), 1);
    assert!(stats.kept_by(FoundBy::RatingKey).is_empty());
}

#[test]
fn an_id_plex_stops_reporting_keeps_its_older_last_seen() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let file = "/media/movies/Test Movie (2000)/test.mkv";
    let mut first = movie("999", "Test Movie", "imdb://tt1111111", file);
    first["Guid"] = json!([{"id": "imdb://tt1111111"}, {"id": "tmdb://42"}]);
    let sections = movies(vec![first]);
    walk_at(&mut conn, &sections, &[], None, "2026-01-01T00:00:00+00:00").expect("first walk");

    walk_at(
        &mut conn,
        &movies(vec![movie("999", "Test Movie", "imdb://tt1111111", file)]),
        &[],
        None,
        "2026-01-02T00:00:00+00:00",
    )
    .expect("second walk");

    assert_eq!(
        texts(
            &conn,
            "SELECT ns || ' ' || last_seen FROM external_ids WHERE item_id = 'imdb:tt1111111' ORDER BY ns"
        ),
        [
            "imdb 2026-01-02T00:00:00+00:00",
            "tmdb 2026-01-01T00:00:00+00:00"
        ],
        "only the ids a walk saw are stamped"
    );
}

fn simpsons(rating_key: &str, index: i64, title: &str, guids: &[&str]) -> Value {
    json!({
        "ratingKey": rating_key,
        "title": title,
        "grandparentTitle": "The Simpsons",
        "parentIndex": 37,
        "index": index,
        "Guid": guids.iter().map(|id| json!({"id": id})).collect::<Vec<_>>(),
        "Media": [{"Part": [{"file": format!("/media/television/The Simpsons/S37E{index:02}.mkv")}]}],
    })
}

fn episodes(records: Vec<Value>) -> FakePlex {
    let mut plex = FakePlex {
        sections: vec![Section {
            key: "2".into(),
            kind: "show".into(),
            title: "TV Shows".into(),
        }],
        ..FakePlex::default()
    };
    plex.set("2", TYPE_SHOW, vec![]);
    plex.set("2", TYPE_EPISODE, records);
    plex
}

const E14_TITLE: &str = "Irrational Treasure [800th episode]";

#[test]
fn two_different_episodes_plex_gave_one_tvdb_id_stay_two_rows() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let e14 = simpsons(
        "149812",
        14,
        E14_TITLE,
        &["imdb://tt36431487", "tmdb://6878206", "tvdb://11464298"],
    );
    let e16 = simpsons(
        "149814",
        16,
        "Extreme Makeover: Homer Edition",
        &["tvdb://11464298"],
    );
    let fork = derive_item_id(
        &[],
        &canonical_path("/media/television/The Simpsons/S37E16.mkv", &[]),
    );

    let stats = walked(&mut conn, &episodes(vec![e14, e16]));

    assert_eq!(
        mapping(&conn),
        [pair("149812", "imdb:tt36431487"), pair("149814", &fork)]
    );
    assert_eq!(
        texts(
            &conn,
            "SELECT title FROM items WHERE item_id = 'imdb:tt36431487'"
        ),
        [E14_TITLE]
    );
    let [forked] = stats.identities_forked.as_slice() else {
        panic!("one fork, got {:?}", stats.identities_forked)
    };
    assert_eq!(
        (
            forked.rating_key.as_str(),
            forked.contested_by.as_str(),
            forked.forked_id.as_str()
        ),
        ("149814", "149812", fork.as_str())
    );
    assert_eq!(forked.contested_title, E14_TITLE);
}

#[test]
fn the_fork_does_not_depend_on_which_episode_is_walked_first() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let e16 = simpsons(
        "149814",
        16,
        "Extreme Makeover: Homer Edition",
        &["tvdb://11464298"],
    );
    let e14 = simpsons(
        "149812",
        14,
        E14_TITLE,
        &["imdb://tt36431487", "tmdb://6878206", "tvdb://11464298"],
    );

    walked(&mut conn, &episodes(vec![e16, e14]));

    assert_eq!(
        mapping(&conn),
        [
            pair("149812", "imdb:tt36431487"),
            pair("149814", "tvdb:11464298")
        ]
    );
}

#[test]
fn one_episode_listed_under_two_season_numbers_stays_one_row() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let listed = |rating_key: &str, season: i64, index: i64| {
        json!({
            "ratingKey": rating_key,
            "title": "Twisted Christian",
            "grandparentTitle": "South Park",
            "parentIndex": season,
            "index": index,
            "Guid": [{"id": "tvdb://11256175"}, {"id": "tmdb://6443923"}],
            "Media": [{"Part": [{"file": format!("/media/television/South Park/S{season:02}E{index:02}.mkv")}]}],
        })
    };

    let stats = walked(
        &mut conn,
        &episodes(vec![listed("200", 27, 6), listed("201", 28, 1)]),
    );

    assert_eq!(
        mapping(&conn),
        [pair("200", "tmdb:6443923"), pair("201", "tmdb:6443923")]
    );
    assert_eq!(one::<i64>(&conn, "SELECT count(*) FROM items"), 1);
    assert!(stats.identities_forked.is_empty());
}

#[test]
fn a_store_already_holding_the_fused_row_is_repaired_by_the_next_walk() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title, show_title, season, episode) VALUES \
           ('imdb:tt36431487', 'episode', 'Extreme Makeover: Homer Edition', 'The Simpsons', 37, 16); \
         INSERT INTO external_ids (item_id, ns, value, kind, last_seen) VALUES \
           ('imdb:tt36431487', 'imdb', 'tt36431487', 'episode', '2026-08-12T19:50:09+00:00'), \
           ('imdb:tt36431487', 'tmdb', '6878206', 'episode', '2026-08-12T19:50:09+00:00'), \
           ('imdb:tt36431487', 'tvdb', '11464298', 'episode', '2026-08-12T19:50:09+00:00'); \
         INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) VALUES \
           ('149812', 'imdb:tt36431487', '2', '2026-08-12T19:50:09+00:00'), \
           ('149814', 'imdb:tt36431487', '2', '2026-08-12T19:50:09+00:00');",
    )
    .expect("seed the fused row");
    let e14 = simpsons(
        "149812",
        14,
        E14_TITLE,
        &["imdb://tt36431487", "tmdb://6878206", "tvdb://11464298"],
    );
    let e16 = simpsons(
        "149814",
        16,
        "Extreme Makeover: Homer Edition",
        &["tvdb://11464298"],
    );
    let fork = derive_item_id(
        &[],
        &canonical_path("/media/television/The Simpsons/S37E16.mkv", &[]),
    );

    walked(&mut conn, &episodes(vec![e14, e16]));

    assert_eq!(
        mapping(&conn),
        [pair("149812", "imdb:tt36431487"), pair("149814", &fork)]
    );
    assert_eq!(
        texts(
            &conn,
            "SELECT title FROM items WHERE item_id = 'imdb:tt36431487'"
        ),
        [E14_TITLE]
    );
}

#[test]
fn a_movie_and_a_show_sharing_a_tmdb_number_stay_two_identities() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let mut plex = movies(vec![json!({
        "ratingKey": "5550", "type": "movie", "title": "Godzilla", "year": 1954,
        "Guid": [{"id": "imdb://tt0047034"}, {"id": "tmdb://1678"}],
        "Media": [{"Part": [{"file": "/media/movies/Godzilla (1954)/g.mkv"}]}],
    })]);
    plex.sections.push(Section {
        key: "2".into(),
        kind: "show".into(),
        title: "TV Shows".into(),
    });
    plex.set(
        "2",
        TYPE_SHOW,
        vec![json!({
            "ratingKey": "141718", "type": "show", "title": "The Golden Girls", "year": 1985,
            "key": "/library/metadata/141718",
            "Guid": [{"id": "imdb://tt0088526"}, {"id": "tmdb://1678"}],
        })],
    );
    plex.set("2", TYPE_EPISODE, vec![]);

    let stats = walked(&mut conn, &plex);

    assert_eq!(
        mapping(&conn),
        [
            pair("141718", "imdb:tt0088526"),
            pair("5550", "imdb:tt0047034")
        ]
    );
    assert_eq!(
        texts(
            &conn,
            "SELECT item_id || ' ' || kind FROM external_ids WHERE ns = 'tmdb' ORDER BY 1"
        ),
        ["imdb:tt0047034 movie", "imdb:tt0088526 show"]
    );
    assert!(stats.identities_kept.is_empty());
}

#[test]
fn a_walk_scoped_to_one_section_and_an_unknown_section_type_are_handled() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());

    let scoped = walk_all(&mut conn, &recorded(), &[], Some("1")).expect("walk");
    assert_eq!((scoped.sections_walked, scoped.titles_seen), (1, 2));
    assert_eq!(texts(&conn, "SELECT DISTINCT type FROM items"), ["movie"]);

    let photos = FakePlex {
        sections: vec![Section {
            key: "9".into(),
            kind: "photo".into(),
            title: "Photos".into(),
        }],
        ..FakePlex::default()
    };
    let skipped = walked(&mut conn, &photos);
    assert_eq!((skipped.sections_walked, skipped.titles_seen), (0, 0));
}
