//! Extras libraries (ADR-0030): `fold-extras` and `extras-walk` over a store the real
//! migration built and a directory of real files.

mod common;

use std::fs;
use std::path::Path;

use plexdb::extras_cmd::status_lines;
use plexdb::extras_declaration::{ExtrasError, fold_file};
use plexdb::extras_walk::walk_all;
use plexdb_store::{WriteLock, open_store_exclusive};
use rusqlite::Connection;

const T1: &str = "2026-10-27T00:00:00+00:00";
const T2: &str = "2026-10-27T00:10:00+00:00";

struct World {
    _dir: tempfile::TempDir,
    root: String,
    roots: Vec<String>,
    json: std::path::PathBuf,
    conn: Connection,
    store: std::path::PathBuf,
}

fn declaration(root: &str) -> String {
    format!(
        r#"[{{"name": "Music Videos", "root": "{root}/musicvideos", "type": "music-video",
            "template": "{{artist}}/{{title}}",
            "fields": [{{"name": "artist", "kind": "text", "required": true}},
                       {{"name": "featured", "kind": "list"}},
                       {{"name": "title", "kind": "text", "required": true}},
                       {{"name": "year", "kind": "year"}}]}}]"#
    )
}

fn world() -> World {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = fs::canonicalize(dir.path())
        .expect("canonical")
        .to_string_lossy()
        .into_owned();
    fs::create_dir_all(format!("{root}/musicvideos")).expect("library root");
    let conn = common::store(dir.path());
    let json = dir.path().join("extras.json");
    fs::write(&json, declaration(&root)).expect("extras.json");
    let store = dir.path().join("plexdb.db");
    World {
        roots: vec![root.clone()],
        root,
        json,
        conn,
        store,
        _dir: dir,
    }
}

fn put(world: &World, relative: &str, body: &str) -> String {
    let path = format!("{}/musicvideos/{relative}", world.root);
    fs::create_dir_all(Path::new(&path).parent().expect("parent")).expect("dirs");
    fs::write(&path, body).expect("write");
    path
}

fn fold(world: &mut World) -> Result<(), ExtrasError> {
    fold_file(&mut world.conn, &world.json, &world.roots, T1).map(|_| ())
}

fn walk(world: &mut World, now: &str) {
    walk_all(&mut world.conn, &world.roots, None, now).expect("walk");
}

fn count(conn: &Connection, sql: &str) -> i64 {
    conn.query_row(sql, [], |row| row.get(0)).expect("count")
}

#[test]
fn two_identical_files_are_one_item_with_two_file_rows() {
    let mut w = world();
    fold(&mut w).expect("fold");
    put(&w, "a/one.mkv", "same bytes");
    put(&w, "b/two.mkv", "same bytes");
    walk(&mut w, T1);

    assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_item"), 1);
    assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_file"), 2);
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_file WHERE primary_file = 1"
        ),
        1
    );
    assert_eq!(
        count(&w.conn, "SELECT COUNT(*) FROM items WHERE type = 'extra'"),
        1
    );
    let id: String = w
        .conn
        .query_row("SELECT item_id FROM extra_item", [], |row| row.get(0))
        .expect("id");
    assert!(id.starts_with("fs:") && id.len() == 19, "{id}");
}

#[test]
fn moving_a_file_keeps_its_item_id() {
    let mut w = world();
    fold(&mut w).expect("fold");
    let old = put(&w, "a/song.mkv", "content one");
    walk(&mut w, T1);
    let before: String = w
        .conn
        .query_row("SELECT item_id FROM extra_item", [], |row| row.get(0))
        .expect("id");

    let new = format!("{}/musicvideos/b/renamed.mkv", w.root);
    fs::create_dir_all(Path::new(&new).parent().expect("parent")).expect("dirs");
    fs::rename(&old, &new).expect("move");
    walk(&mut w, T2);

    let after: (String, String) = w
        .conn
        .query_row("SELECT item_id, path FROM extra_file", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .expect("one file row");
    assert_eq!(after, (before, new));
    assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_item"), 1);
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_file WHERE missing_since IS NOT NULL"
        ),
        0
    );
}

#[test]
fn the_first_scan_organizes_everything_and_a_later_file_waits_in_the_queue() {
    let mut w = world();
    fold(&mut w).expect("fold");
    put(&w, "a/old.mkv", "old");
    walk(&mut w, T1);
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_item WHERE organized = 1"
        ),
        1
    );

    put(&w, "drop.mkv", "new");
    walk(&mut w, T2);
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_item WHERE organized = 1"
        ),
        1
    );
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_item WHERE organized = 0"
        ),
        1
    );
    assert_eq!(
        status_lines(&w.conn).expect("status"),
        [
            "Music Videos: files 2, items 2, in queue 1, organized 1, missing 0, more than one file 0"
        ]
    );
}

#[test]
fn a_file_that_leaves_is_marked_missing_and_clears_when_it_returns() {
    let mut w = world();
    fold(&mut w).expect("fold");
    let path = put(&w, "a.mkv", "x");
    walk(&mut w, T1);
    fs::remove_file(&path).expect("remove");
    walk(&mut w, T2);
    let missing: Option<String> = w
        .conn
        .query_row("SELECT missing_since FROM extra_file", [], |row| row.get(0))
        .expect("row");
    assert_eq!(missing.as_deref(), Some(T2));
    fs::write(&path, "x").expect("restore");
    walk(&mut w, "2026-10-27T00:20:00+00:00");
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_file WHERE missing_since IS NULL"
        ),
        1
    );
}

#[test]
fn an_unmounted_root_marks_nothing_missing() {
    let mut w = world();
    fold(&mut w).expect("fold");
    put(&w, "a.mkv", "x");
    walk(&mut w, T1);
    fs::remove_dir_all(format!("{}/musicvideos", w.root)).expect("unmount");
    walk(&mut w, T2);
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_file WHERE missing_since IS NOT NULL"
        ),
        0
    );
}

#[test]
fn a_rejected_extras_json_keeps_the_stored_declaration_and_says_why() {
    let mut w = world();
    fold(&mut w).expect("fold");
    let stored = count(&w.conn, "SELECT COUNT(*) FROM extra_field");
    assert_eq!(stored, 4);

    let cases = [
        (
            declaration(&w.root).replace(r#""kind": "year""#, r#""kind": "date""#),
            "kind",
        ),
        (
            declaration(&w.root).replace("{artist}/{title}", "{artist}/{album}"),
            "not a declared field",
        ),
        (
            declaration(&w.root).replace(r#""name": "year""#, r#""name": "title""#),
            "repeats the field name",
        ),
        (
            declaration(&w.root).replace("/musicvideos", "/elsewhere"),
            "not a directory",
        ),
        (
            declaration(&w.root).replace(&w.root, "/not-a-source-root"),
            "not under PLEX_SOURCE_ROOTS",
        ),
    ];
    for (text, why) in cases {
        fs::write(&w.json, &text).expect("write");
        let err = fold(&mut w).expect_err("refused").to_string();
        assert!(err.contains(why), "{err} should mention {why}");
        assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_field"), stored);
        let root: String = w
            .conn
            .query_row("SELECT root FROM extra_library", [], |row| row.get(0))
            .expect("root");
        assert_eq!(root, format!("{}/musicvideos", w.root));
    }
}

#[test]
fn a_repeated_name_is_refused_and_a_library_holding_extras_cannot_be_dropped() {
    let mut w = world();
    let one = declaration(&w.root);
    let entry = one
        .trim()
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_string();
    fs::write(&w.json, format!("[{entry},{entry}]")).expect("write");
    assert!(
        fold(&mut w)
            .expect_err("refused")
            .to_string()
            .contains("repeats a library name")
    );

    fs::write(&w.json, &one).expect("write");
    fold(&mut w).expect("fold");
    put(&w, "a.mkv", "x");
    walk(&mut w, T1);
    fs::write(&w.json, "[]").expect("write");
    assert!(
        fold(&mut w)
            .expect_err("refused")
            .to_string()
            .contains("holds 1 extra")
    );
    assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_library"), 1);
}

#[test]
fn a_tick_refuses_while_a_sweep_holds_the_writer_lock() {
    let w = world();
    let sweep = WriteLock::shared(&w.store).expect("a sweep's shared lock");
    assert!(open_store_exclusive(&w.store).expect("asked").is_none());
    drop(sweep);
    assert!(open_store_exclusive(&w.store).expect("asked").is_some());
}

#[test]
fn a_new_file_at_a_moved_files_old_path_is_a_conflict_not_a_copy() {
    let mut w = world();
    fold(&mut w).expect("fold");
    let old = put(&w, "p.mkv", "first video");
    walk(&mut w, T1);
    let moved = format!("{}/musicvideos/q.mkv", w.root);
    fs::rename(&old, &moved).expect("move");
    walk(&mut w, T2);
    put(&w, "p.mkv", "a different video");
    let stats = walk_all(&mut w.conn, &w.roots, None, "2026-10-27T00:20:00+00:00").expect("walk");
    assert_eq!(stats[0].id_conflicts, 1);
    assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_file"), 1);
}

#[test]
fn a_library_holding_extras_cannot_change_its_root() {
    let mut w = world();
    fold(&mut w).expect("fold");
    put(&w, "a.mkv", "x");
    walk(&mut w, T1);
    fs::create_dir_all(format!("{}/other", w.root)).expect("dir");
    fs::write(
        &w.json,
        declaration(&w.root).replace("/musicvideos", "/other"),
    )
    .expect("write");
    let err = fold(&mut w).expect_err("refused").to_string();
    assert!(err.contains("root cannot change"), "{err}");
}

const YOUTUBE_RULE: &str = r#"{"match": "^(?P<id>[A-Za-z0-9_-]{11})(\\.f\\d+)?$",
    "set": {"source_url": "https://www.youtube.com/watch?v={id}"}}"#;

fn declaration_with(root: &str, parsers: &str) -> String {
    format!(
        r#"[{{"name": "Music Videos", "root": "{root}/musicvideos", "type": "music-video",
            "fields": [{{"name": "artist", "kind": "text"}}, {{"name": "title", "kind": "text"}}],
            "parsers": {parsers}}}]"#
    )
}

fn source_urls(conn: &Connection) -> Vec<(String, Option<String>)> {
    let mut stmt = conn
        .prepare(
            "SELECT i.title, e.source_url FROM extra_item e JOIN items i USING (item_id) \
             ORDER BY i.title",
        )
        .expect("prepare");
    stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

#[test]
fn a_parser_proposes_a_source_url_from_the_names_of_real_files() {
    let mut w = world();
    fs::write(
        &w.json,
        declaration_with(&w.root, &format!("[{YOUTUBE_RULE}]")),
    )
    .expect("json");
    fold(&mut w).expect("fold");
    for name in [
        "3YxaaGgTQYM.mp4",
        "NgylvUo3piE.f328.mp4",
        "ghibli.mp4",
        "9_minutes_of_funny_forgotten_RHONY_moments_nqug8aoUc4o_.mp4",
    ] {
        put(&w, name, name);
    }
    walk(&mut w, T1);

    let want = |id: &str| Some(format!("https://www.youtube.com/watch?v={id}"));
    assert_eq!(
        source_urls(&w.conn),
        vec![
            ("3YxaaGgTQYM".to_string(), want("3YxaaGgTQYM")),
            (
                "9_minutes_of_funny_forgotten_RHONY_moments_nqug8aoUc4o_".to_string(),
                None
            ),
            ("NgylvUo3piE.f328".to_string(), want("NgylvUo3piE")),
            ("ghibli".to_string(), None),
        ]
    );
}

#[test]
fn a_parser_proposes_fields_as_path_sourced_enrichment_and_the_first_rule_wins() {
    let mut w = world();
    let rules = r#"[{"match": "^(?P<artist>.+) - (?P<title>.+)$",
                     "set": {"artist": "{artist}", "title": "{title}"}},
                    {"match": "^(?P<a>.+) - ", "set": {"artist": "{a}!"}}]"#;
    fs::write(&w.json, declaration_with(&w.root, rules)).expect("json");
    fold(&mut w).expect("fold");
    put(&w, "Daft Punk - One More Time.mp4", "x");
    walk(&mut w, T1);

    let rows: Vec<(String, String, String)> = {
        let mut stmt = w
            .conn
            .prepare(
                "SELECT key, value, source FROM enrichment WHERE namespace = 'extra' ORDER BY key",
            )
            .expect("prepare");
        stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .expect("query")
            .collect::<Result<_, _>>()
            .expect("rows")
    };
    assert_eq!(
        rows,
        vec![
            ("artist".into(), "Daft Punk".into(), "path".into()),
            ("title".into(), "One More Time".into(), "path".into()),
        ]
    );
    let title: String = w
        .conn
        .query_row("SELECT title FROM items WHERE type = 'extra'", [], |row| {
            row.get(0)
        })
        .expect("title");
    assert_eq!(title, "One More Time");
}

#[test]
fn a_person_set_source_url_survives_a_rescan() {
    let mut w = world();
    fs::write(
        &w.json,
        declaration_with(&w.root, &format!("[{YOUTUBE_RULE}]")),
    )
    .expect("json");
    fold(&mut w).expect("fold");
    let path = put(&w, "3YxaaGgTQYM.mp4", "first");
    walk(&mut w, T1);
    w.conn
        .execute(
            "UPDATE extra_item SET source_url = 'https://vimeo.com/1'",
            [],
        )
        .expect("person sets it");

    fs::write(&path, "second, longer").expect("rewrite");
    walk(&mut w, T2);
    put(&w, "NgylvUo3piE.f328.mp4", "other");
    walk(&mut w, T2);

    assert_eq!(
        source_urls(&w.conn),
        vec![
            (
                "3YxaaGgTQYM".to_string(),
                Some("https://vimeo.com/1".to_string())
            ),
            (
                "NgylvUo3piE.f328".to_string(),
                Some("https://www.youtube.com/watch?v=NgylvUo3piE".to_string())
            ),
        ]
    );
}

#[test]
fn a_bad_parser_makes_fold_extras_refuse_the_file_and_keep_the_stored_declaration() {
    let mut w = world();
    fold(&mut w).expect("fold the plain declaration");
    let stored = count(&w.conn, "SELECT COUNT(*) FROM extra_library");
    for (rules, why) in [
        (
            r#"[{"match": "(unclosed", "set": {"title": "{x}"}}]"#,
            "not a regex",
        ),
        (
            r#"[{"match": "^(?P<id>.+)$", "set": {"mood": "{id}"}}]"#,
            "neither a declared field nor source_url",
        ),
        (
            r#"[{"match": "^(?P<id>.+)$", "set": {"title": "{nope}"}}]"#,
            "not a group of its match",
        ),
        (r#"{"match": "x"}"#, "not a list"),
        (r#"[{"match": "x", "set": {}}]"#, "empty set"),
    ] {
        fs::write(&w.json, declaration_with(&w.root, rules)).expect("json");
        match fold(&mut w) {
            Err(ExtrasError::Refused(message)) => {
                assert!(message.contains(why), "{message} lacks {why}")
            }
            other => panic!("expected a refusal for {rules}, got {other:?}"),
        }
    }
    assert_eq!(count(&w.conn, "SELECT COUNT(*) FROM extra_library"), stored);
    assert_eq!(
        count(
            &w.conn,
            "SELECT COUNT(*) FROM extra_library WHERE parsers IS NOT NULL"
        ),
        0
    );
}
