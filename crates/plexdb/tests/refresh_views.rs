//! `refresh-map`, `refresh-tagnetwork` and `publish` against a store built by the real
//! `plexdb migrate`: the stored views follow the keyword rows, one store gives one layout,
//! and the snapshot is a self-contained copy.

mod common;

use plexdb::keyword_views::{keyword_rows, merge_map};
use plexdb::layout::{embed_2d, sign_bits};
use plexdb::publish::publish;
use plexdb::refresh_map::{self, map_fingerprint};
use plexdb::refresh_tagnetwork::{self, draw as draw_network};
use plexdb::staleness::Now;
use rusqlite::Connection;

fn add_title(conn: &Connection, id: &str, kind: &str, keywords: &[&str]) {
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        (id, kind, id),
    )
    .expect("item");
    for keyword in keywords {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES (?, 'keywords', 'tmdb', 'keyword', ?, '2026-01-01T00:00:00+00:00')",
            (id, keyword),
        )
        .expect("keyword");
    }
}

/// Sixty movies in three themes of twenty, each with its theme's tags plus a shared one, and
/// a few shows.
fn themed(conn: &Connection) {
    for theme in 0..3 {
        for n in 0..20 {
            let own = format!("theme{theme}");
            let sub = format!("theme{theme}-sub{}", n % 4);
            let odd = format!("odd{}", n % 7);
            add_title(
                conn,
                &format!("fs:{theme}-{n:02}"),
                "movie",
                &[&own, &sub, &odd, "common"],
            );
        }
    }
    for n in 0..6 {
        add_title(conn, &format!("fs:show-{n}"), "show", &["drama", "serial"]);
    }
}

fn positions(conn: &Connection) -> Vec<(String, String, f64, f64)> {
    let mut statement = conn
        .prepare("SELECT kind, item_id, x, y FROM title_map ORDER BY kind, item_id")
        .expect("prepare");
    statement
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .expect("query")
        .map(|r| r.expect("row"))
        .collect()
}

#[test]
fn a_map_is_drawn_once_and_kept_while_the_keywords_are_unchanged() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = common::store(dir.path());
    themed(&conn);

    let first = refresh_map::refresh(&mut conn, &Now::read()).expect("refresh");
    assert_eq!(
        first
            .iter()
            .map(|r| (r.kind, r.redrawn, r.placed))
            .collect::<Vec<_>>(),
        [("movie", true, 60), ("show", true, 6)]
    );
    let again = refresh_map::refresh(&mut conn, &Now::read()).expect("refresh again");
    assert_eq!(
        again
            .iter()
            .map(|r| (r.kind, r.redrawn, r.placed))
            .collect::<Vec<_>>(),
        [("movie", false, 60), ("show", false, 6)]
    );
    let (min_x, max_x, min_y, max_y): (f64, f64, f64, f64) = conn
        .query_row(
            "SELECT MIN(x), MAX(x), MIN(y), MAX(y) FROM title_map WHERE kind = 'movie'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .expect("bounds");
    assert_eq!((min_x.min(min_y), max_x.max(max_y)), (0.0, 1.0));
    let labelled: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM title_map_label WHERE kind = 'movie'",
            [],
            |r| r.get(0),
        )
        .expect("labels");
    assert!(
        labelled > 0,
        "a map of 60 titles in themes has regions to name"
    );
}

#[test]
fn two_runs_of_one_store_give_identical_coordinates() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = common::store(dir.path());
    themed(&conn);
    refresh_map::refresh(&mut conn, &Now::read()).expect("refresh");
    let first = positions(&conn);
    conn.execute("DELETE FROM title_map_state", [])
        .expect("stale");
    let redrawn = refresh_map::refresh(&mut conn, &Now::read()).expect("redraw");
    assert!(redrawn.iter().all(|r| r.redrawn));
    assert_eq!(positions(&conn), first);
}

#[test]
fn a_changed_keyword_row_makes_the_stored_map_stale() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = common::store(dir.path());
    themed(&conn);
    let before = map_fingerprint(&conn, "movie").expect("fingerprint");
    conn.execute(
        "UPDATE enrichment SET fetched_at = '2026-02-01T00:00:00+00:00' \
         WHERE item_id = 'fs:0-00' AND value = 'common'",
        [],
    )
    .expect("restamp");
    assert_ne!(
        map_fingerprint(&conn, "movie").expect("fingerprint"),
        before
    );
    assert!(before.starts_with(refresh_map::MAP_RECIPE));
}

#[test]
fn the_projection_and_the_layout_depend_only_on_the_seed() {
    assert_eq!(sign_bits(0, 5), sign_bits(0, 5));
    assert_ne!(sign_bits(0, 5), sign_bits(0, 6));
    assert_ne!(sign_bits(0, 5), sign_bits(1, 5));
    // Wide enough to be projected: 300 columns, 40 rows in two blocks.
    let cells: Vec<(usize, usize, f64)> = (0..40)
        .flat_map(|r| (0..12).map(move |k| (r, (r / 20) * 150 + (r * 7 + k * 11) % 150, 1.0)))
        .collect();
    let a = embed_2d(40, 300, &cells, 3).expect("layout");
    let b = embed_2d(40, 300, &cells, 3).expect("layout");
    assert_eq!(a, b);
    assert_ne!(a, embed_2d(40, 300, &cells, 4).expect("layout"));
}

#[test]
fn a_tag_network_keeps_every_tags_strongest_links_exactly() {
    let rows: Vec<(String, String)> = [
        ("t1", "a"),
        ("t1", "b"),
        ("t1", "c"),
        ("t2", "a"),
        ("t2", "b"),
        ("t3", "a"),
        ("t3", "b"),
        ("t3", "c"),
        ("t4", "a"),
        ("t4", "c"),
        ("t5", "rare"),
    ]
    .iter()
    .map(|(i, v)| (i.to_string(), v.to_string()))
    .collect();
    let network = draw_network(&rows, 0).expect("network");
    assert_eq!(
        network
            .nodes
            .iter()
            .map(|n| (n.value.as_str(), n.df))
            .collect::<Vec<_>>(),
        [("a", 4), ("b", 3), ("c", 3)]
    );
    assert_eq!(
        network.edges,
        [
            ("a".to_string(), "b".to_string(), 3),
            ("a".to_string(), "c".to_string(), 3),
            ("b".to_string(), "c".to_string(), 2),
        ]
    );
}

#[test]
fn the_stored_network_follows_the_merge_map() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = common::store(dir.path());
    themed(&conn);
    conn.execute(
        "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) \
         VALUES ('theme0-sub0', 'theme0-sub1', 0.95, 'jev', '2026-01-01T00:00:00+00:00')",
        [],
    )
    .expect("pair");
    let folded = merge_map(&conn).expect("merge map");
    assert_eq!(folded.len(), 1);
    let rows = keyword_rows(&conn, "movie", &folded).expect("rows");
    let gone = folded.keys().next().expect("a folded keyword").clone();
    assert!(rows.iter().all(|(_, value)| *value != gone));

    refresh_tagnetwork::refresh(&mut conn, &Now::read()).expect("refresh");
    let stored: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM tag_network WHERE kind = 'movie' AND value = ?",
            [&gone],
            |r| r.get(0),
        )
        .expect("count");
    assert_eq!(stored, 0, "a merged keyword is no node of its own");
}

#[test]
fn publish_writes_a_self_contained_copy_and_replaces_it_on_a_second_run() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = common::store(dir.path());
    themed(&conn);
    drop(conn);
    let snapshot = dir.path().join("out").join("snapshot.db");

    let (version, size) = publish(&dir.path().join("plexdb.db"), &snapshot).expect("publish");
    assert_eq!(version, plexdb_schema::SCHEMA_VERSION);
    assert_eq!(size, std::fs::metadata(&snapshot).expect("snapshot").len());
    assert!(!snapshot.with_file_name("snapshot.db.tmp").exists());
    assert!(!snapshot.with_file_name("snapshot.db-wal").exists());
    let copy = Connection::open(&snapshot).expect("open snapshot");
    let titles: i64 = copy
        .query_row("SELECT COUNT(*) FROM items", [], |r| r.get(0))
        .expect("count");
    assert_eq!(titles, 66);
    drop(copy);
    publish(&dir.path().join("plexdb.db"), &snapshot).expect("publish again");
}
