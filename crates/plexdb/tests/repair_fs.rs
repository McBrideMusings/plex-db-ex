//! `repair-fs-identities`: moving `fs:` item_ids still keyed on the disk mount point
//! (issue #55), against a store built by the real `plexdb migrate`.

mod common;

use common::{FakePlex, movies, one, recorded, store, texts};
use plexdb::identity::{canonical_path, derive_item_id};
use plexdb::plex::Section;
use plexdb::repair_fs::{apply_fs_repairs, plan_fs_repairs, run};
use plexdb::walk::walk_all;
use plexdb_store::Config;
use rusqlite::Connection;
use serde_json::{Value, json};

const AIR_MATER_PATH: &str = "/media/movies/Air Mater (2011) {imdb-tt2100093}/Air Mater (2011).mkv";
const AIR_MATER_KEY: &str = "70936";

fn roots() -> Vec<String> {
    vec!["/media".into()]
}

/// The id a title with no external GUID held before `PLEX_SOURCE_ROOTS` was set.
fn old_wrong_id() -> String {
    derive_item_id(&[], &canonical_path(AIR_MATER_PATH, &[]))
}

fn corrected(path: &str) -> String {
    derive_item_id(&[], &canonical_path(path, &roots()))
}

fn seed(conn: &Connection, item_id: &str, rating_key: &str, title: &str) {
    conn.execute(
        "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, 2011)",
        (item_id, title),
    )
    .expect("item");
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
         VALUES (?, ?, '1', '2024-01-01T00:00:00+00:00')",
        (rating_key, item_id),
    )
    .expect("rating key");
    conn.execute(
        "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at, seconds_watched) \
         VALUES (?, ?, ?, 1, 1000, 300)",
        (format!("h-{rating_key}"), item_id, rating_key),
    )
    .expect("play");
}

/// Two library sections each listing the same GUID-less file.
fn duplicated() -> FakePlex {
    let movie = |rating_key: &str| -> Value {
        json!({
            "ratingKey": rating_key, "type": "movie", "title": "Dup",
            "Media": [{"Part": [{"file": "/media/movies/Dup/dup.mkv"}]}],
        })
    };
    let mut plex = movies(vec![movie("5550")]);
    plex.sections.push(Section {
        key: "3".into(),
        kind: "movie".into(),
        title: "More Movies".into(),
    });
    plex.set("3", 1, vec![movie("5551")]);
    plex
}

fn seed_duplicates(conn: &Connection) -> (String, String, String) {
    let new_path = "/media/movies/Dup/dup.mkv";
    let old_a = derive_item_id(&[], &canonical_path(new_path, &[]));
    // A distinct wrong id for the second copy, as if walked under another mount path.
    let old_b = derive_item_id(&[], &format!("/mnt/plex{}", canonical_path(new_path, &[])));
    seed(conn, &old_a, "5550", "Dup A");
    seed(conn, &old_b, "5551", "Dup B");
    (old_a, old_b, corrected(new_path))
}

fn walked_recorded(dir: &std::path::Path) -> Connection {
    let mut conn = store(dir);
    walk_all(&mut conn, &recorded(), &[], None).expect("walk");
    conn
}

#[test]
fn the_identity_moves_and_its_play_and_rating_key_follow() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    seed(&conn, &old_wrong_id(), AIR_MATER_KEY, "Air Mater");
    let new_id = corrected(AIR_MATER_PATH);
    // Every table whose `item_id` references `items` has to move, or the commit's
    // deferred foreign-key check refuses the whole repair.
    conn.execute(
        "INSERT INTO media_part (part_id, item_id, rating_key, file, fetched_at) \
         VALUES (1, ?, ?, 'a.mkv', '2024-01-01T00:00:00+00:00')",
        (old_wrong_id(), AIR_MATER_KEY),
    )
    .expect("part");
    conn.execute(
        "INSERT INTO keyword_surfaces (item_id, source, surface, spoiler, fetched_at) \
         VALUES (?, 'tmdb', 'Heist', 0, '2024-01-01T00:00:00+00:00')",
        [old_wrong_id()],
    )
    .expect("surface");

    let plan = plan_fs_repairs(&conn, &recorded(), &roots()).expect("plan");
    assert_eq!((plan.fs_found, plan.unchanged, plan.not_in_plex), (1, 0, 0));
    assert_eq!(plan.moves.get(&old_wrong_id()), Some(&new_id));
    assert_eq!(
        texts(&conn, "SELECT item_id FROM items"),
        [old_wrong_id()],
        "planning writes nothing"
    );

    let result = apply_fs_repairs(&conn, &plan).expect("apply");

    assert_eq!(result.rows_carried.get("plex_items"), Some(&1));
    assert_eq!(result.rows_carried.get("plays"), Some(&1));
    assert_eq!(result.rows_carried.get("media_part"), Some(&1));
    assert_eq!(result.rows_carried.get("keyword_surfaces"), Some(&1));
    assert_eq!(result.merges, 0);
    assert_eq!(
        texts(&conn, "SELECT item_id || ' ' || title FROM items"),
        [format!("{new_id} Air Mater")]
    );
    assert_eq!(
        texts(&conn, "SELECT item_id FROM plex_items"),
        std::slice::from_ref(&new_id)
    );
    assert_eq!(
        one::<i64>(&conn, "SELECT seconds_watched FROM plays"),
        300,
        "columns other than item_id survive the move"
    );

    let again = plan_fs_repairs(&conn, &recorded(), &roots()).expect("second plan");
    assert_eq!(
        (again.moves.len(), again.unchanged),
        (0, 1),
        "the corrected id is a fixed point"
    );
}

#[test]
fn a_title_that_left_plex_is_skipped_and_counted_not_guessed_at() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    seed(&conn, &old_wrong_id(), "999999", "Air Mater");

    let plan = plan_fs_repairs(&conn, &recorded(), &roots()).expect("plan");
    let result = apply_fs_repairs(&conn, &plan).expect("apply");

    assert_eq!(
        (plan.fs_found, plan.moves.len(), plan.not_in_plex),
        (1, 0, 1)
    );
    assert!(result.rows_carried.is_empty());
    assert_eq!(texts(&conn, "SELECT item_id FROM items"), [old_wrong_id()]);
}

#[test]
fn an_extra_is_not_counted_as_a_title_that_left_plex() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('fs:clip', 'extra', 'Clip'); \
         INSERT INTO extra_library (name, root, type, declared_at) \
           VALUES ('mv', '/media/mv', 'music_video', '2026-01-01T00:00:00+00:00'); \
         INSERT INTO extra_item (item_id, library, organized, first_seen) \
           VALUES ('fs:clip', 'mv', 1, '2026-01-01T00:00:00+00:00');",
    )
    .expect("extra");

    let plan = plan_fs_repairs(&conn, &recorded(), &roots()).expect("plan");

    assert_eq!(
        (plan.fs_found, plan.moves.len(), plan.not_in_plex),
        (0, 0, 0)
    );
}

#[test]
fn two_ids_converging_on_one_path_merge_and_each_source_keeps_its_own_keyword() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    let (old_a, old_b, new_id) = seed_duplicates(&conn);
    for (item_id, source) in [(&old_a, "tmdb"), (&old_b, "other")] {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES (?, 'keywords', ?, 'keyword', 'heist', '2024-01-01T00:00:00+00:00')",
            (item_id, source),
        )
        .expect("keyword");
        conn.execute(
            "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
             VALUES (?, 'keywords', ?, 'fetched', '2024-01-01T00:00:00+00:00')",
            (item_id, source),
        )
        .expect("cursor");
    }
    for item_id in [&old_a, &old_b] {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES (?, 'mdblist', 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')",
            [item_id],
        )
        .expect("list fact");
    }

    let plan = plan_fs_repairs(&conn, &duplicated(), &roots()).expect("plan");
    let result = apply_fs_repairs(&conn, &plan).expect("apply");

    assert_eq!((plan.moves.len(), result.merges), (2, 1));
    // The identical list fact folds into one row; the two sources' keywords do not.
    assert_eq!(result.duplicates_dropped.get("enrichment"), Some(&1));
    assert_eq!(result.duplicates_dropped.get("enrichment_cursor"), None);
    assert_eq!(
        texts(&conn, "SELECT item_id FROM items"),
        std::slice::from_ref(&new_id)
    );
    assert_eq!(
        texts(
            &conn,
            "SELECT source FROM enrichment WHERE namespace = 'keywords' ORDER BY source"
        ),
        ["other", "tmdb"]
    );
    assert_eq!(
        texts(
            &conn,
            "SELECT source FROM enrichment_cursor ORDER BY source"
        ),
        ["other", "tmdb"]
    );
    assert_eq!(texts(&conn, "SELECT DISTINCT item_id FROM plays"), [new_id]);
    assert_eq!(one::<i64>(&conn, "SELECT count(*) FROM plays"), 2);
}

#[test]
fn the_step_takes_a_backup_and_does_not_roll_back_a_legitimate_merge() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    let (old_a, old_b, new_id) = seed_duplicates(&conn);
    for item_id in [&old_a, &old_b] {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES (?, 'mdblist', 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')",
            [item_id],
        )
        .expect("list fact");
    }
    drop(conn);
    let config = Config {
        store_path: dir.path().join("plexdb.db"),
        snapshot_path: None,
        backup_dir: dir.path().join("backups"),
    };

    let dry = run(&config, &duplicated(), &roots(), true).expect("dry run");
    assert!(dry[0].contains("2 would move"), "{dry:?}");
    assert!(
        !config
            .backup_dir
            .join("plexdb.pre-repair-fs-identities.db")
            .exists()
    );

    let lines = run(&config, &duplicated(), &roots(), false).expect("a merge is not row loss");

    assert!(lines[0].contains("2 moved"), "{lines:?}");
    assert!(
        config
            .backup_dir
            .join("plexdb.pre-repair-fs-identities.db")
            .exists()
    );
    let conn = Connection::open(&config.store_path).expect("reopen");
    assert_eq!(texts(&conn, "SELECT item_id FROM items"), [new_id]);
}

#[test]
fn a_clean_store_has_nothing_to_repair() {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = walked_recorded(dir.path());
    // The recorded library holds one GUID-less title, already keyed on its (unrooted) path.
    let plan = plan_fs_repairs(&conn, &recorded(), &[]).expect("plan");
    assert_eq!((plan.fs_found, plan.moves.len(), plan.unchanged), (1, 0, 1));

    let empty_dir = tempfile::tempdir().expect("tempdir");
    let empty = store(empty_dir.path());
    let none = plan_fs_repairs(&empty, &recorded(), &roots()).expect("plan");
    assert_eq!(none.fs_found, 0);
    assert!(
        apply_fs_repairs(&empty, &none)
            .expect("apply")
            .rows_carried
            .is_empty()
    );
}

#[test]
fn a_restore_removes_the_sidecars_of_the_file_it_replaces() {
    let dir = tempfile::tempdir().expect("tempdir");
    let _ = store(dir.path());
    let path = dir.path().join("plexdb.db");
    let taken = plexdb::migrate::take_backup(&path, &dir.path().join("backups"), "plexdb.pre-v9")
        .expect("backup");
    std::fs::write(dir.path().join("plexdb.db-wal"), b"stale").expect("sidecar");

    plexdb::migrate::restore(&taken, &path).expect("restore");

    assert!(
        !dir.path().join("plexdb.db-wal").exists(),
        "a stale -wal describes the file that was replaced, and reading it against another is a corruption"
    );
}
