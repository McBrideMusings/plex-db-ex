//! A person's decisions about extras (ADR-0030): `extra_decisions.json` folded into a store
//! the real migration built, and organize moving real files.

mod common;

use std::fs;
use std::path::{Path, PathBuf};

use plexdb::extras_decisions::{self as decisions, Decision, Entry};
use plexdb::extras_declaration::fold_file;
use plexdb::extras_organize::{self as organize, Action};
use plexdb::extras_walk::walk_all;
use rusqlite::Connection;

const T1: &str = "2026-10-27T00:00:00+00:00";
const T2: &str = "2026-10-27T00:10:00+00:00";
const T3: &str = "2026-10-27T00:20:00+00:00";
const T4: &str = "2026-10-27T00:30:00+00:00";

struct World {
    _dir: tempfile::TempDir,
    root: String,
    roots: Vec<String>,
    decisions: PathBuf,
    conn: Connection,
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
    fs::write(
        &json,
        format!(
            r#"[{{"name": "Music Videos", "root": "{root}/musicvideos", "type": "music-video",
                "template": "{{artist}}/{{title}}",
                "fields": [{{"name": "artist", "kind": "text", "required": true}},
                           {{"name": "featured", "kind": "list"}},
                           {{"name": "title", "kind": "text", "required": true}},
                           {{"name": "year", "kind": "year"}}]}}]"#
        ),
    )
    .expect("extras.json");
    let roots = vec![root.clone()];
    let mut w = World {
        decisions: dir.path().join("extra_decisions.json"),
        root,
        roots,
        conn,
        _dir: dir,
    };
    fold_file(&mut w.conn, &json, &w.roots, T1).expect("declare");
    w
}

/// One file the walk found, sitting in the queue. A library's first walk marks what it finds
/// organized, so the item is put back in the queue by hand.
fn queued(w: &mut World, relative: &str, body: &str) -> (String, String) {
    walk_all(&mut w.conn, &w.roots, None, T1).expect("first walk");
    let path = put(w, relative, body);
    walk_all(&mut w.conn, &w.roots, None, T2).expect("walk");
    let id: String = w
        .conn
        .query_row(
            "SELECT item_id FROM extra_file WHERE path = ?",
            [&path],
            |row| row.get(0),
        )
        .expect("item");
    w.conn
        .execute(
            "UPDATE extra_item SET organized = 0 WHERE item_id = ?",
            [&id],
        )
        .expect("queue");
    (path, id)
}

fn put(w: &World, relative: &str, body: &str) -> String {
    let path = format!("{}/musicvideos/{relative}", w.root);
    fs::create_dir_all(Path::new(&path).parent().expect("parent")).expect("dirs");
    fs::write(&path, body).expect("write");
    path
}

fn decide(w: &World, decision: Decision, at: &str) {
    let entry = Entry {
        decision,
        decided_at: at.into(),
    };
    decisions::record(&w.decisions, &entry).expect("record");
}

fn set(w: &World, id: &str, field: &str, value: &str, at: &str) {
    decide(
        w,
        Decision::Set {
            item_id: id.into(),
            field: field.into(),
            value: value.into(),
        },
        at,
    );
}

fn fold(w: &mut World, now: &str) -> decisions::FoldStats {
    decisions::fold(&mut w.conn, &w.decisions, &w.roots, now).expect("fold")
}

fn run_organize(w: &mut World, now: &str) -> Vec<String> {
    organize::run_pending(&mut w.conn, &w.decisions, now).expect("organize")
}

fn column(conn: &Connection, sql: &str) -> Vec<String> {
    let mut stmt = conn.prepare(sql).expect("prepare");
    stmt.query_map([], |row| {
        Ok(match row.get::<_, rusqlite::types::Value>(0)? {
            rusqlite::types::Value::Integer(n) => n.to_string(),
            rusqlite::types::Value::Text(text) => text,
            other => format!("{other:?}"),
        })
    })
    .expect("query")
    .collect::<Result<_, _>>()
    .expect("rows")
}

#[test]
fn set_writes_person_values_validated_against_the_declared_kind() {
    let mut w = world();
    let (_, id) = queued(&mut w, "drop.mkv", "a");
    set(&w, &id, "artist", "Björk", T3);
    set(&w, &id, "featured", "A; B ;;C", T3);
    set(&w, &id, "year", "19xx", T3);
    set(&w, &id, "mood", "sad", T3);
    set(&w, &id, "title", "Army of Me", T3);

    let stats = fold(&mut w, T3);
    assert_eq!(stats.applied, 3);
    assert_eq!(stats.refused.len(), 2, "{:?}", stats.refused);
    assert!(stats.refused.iter().any(|why| why.contains("a year")));
    assert!(
        stats
            .refused
            .iter()
            .any(|why| why.contains("declares no field mood"))
    );
    assert_eq!(
        column(
            &w.conn,
            "SELECT key || '=' || value || '/' || source || '/' || rank FROM enrichment \
             WHERE namespace = 'extra' ORDER BY key, rank"
        ),
        [
            "artist=Björk/person/0",
            "featured=A/person/0",
            "featured=B/person/1",
            "featured=C/person/2",
            "title=Army of Me/person/0",
        ]
    );
    assert_eq!(
        column(
            &w.conn,
            &format!("SELECT title FROM items WHERE item_id = '{id}'")
        ),
        ["Army of Me"]
    );

    let before = column(
        &w.conn,
        "SELECT key || value || fetched_at FROM enrichment ORDER BY 1",
    );
    fold(&mut w, T4);
    assert_eq!(
        column(
            &w.conn,
            "SELECT key || value || fetched_at FROM enrichment ORDER BY 1"
        ),
        before,
        "a second fold changes no row"
    );
}

#[test]
fn tag_adds_a_person_keyword_and_keeps_the_ones_already_there() {
    let mut w = world();
    let (_, id) = queued(&mut w, "drop.mkv", "a");
    for tag in ["Live", "Black and White"] {
        decide(
            &w,
            Decision::Tag {
                item_id: id.clone(),
                tag: tag.into(),
            },
            T3,
        );
    }
    let stats = fold(&mut w, T3);
    assert_eq!(stats.applied, 2);
    fold(&mut w, T4);
    assert_eq!(
        column(
            &w.conn,
            "SELECT surface FROM keyword_surfaces WHERE source = 'person' ORDER BY surface"
        ),
        ["Black and White", "Live"]
    );
    assert_eq!(
        column(
            &w.conn,
            "SELECT value FROM enrichment WHERE namespace = 'keywords' AND source = 'person' \
             ORDER BY value"
        )
        .len(),
        2
    );
}

#[test]
fn organize_is_refused_while_a_required_field_is_empty_then_moves_and_undoes() {
    let mut w = world();
    let (path, id) = queued(&mut w, "drop.mkv", "video");
    set(&w, &id, "artist", "Björk", T3);
    fold(&mut w, T3);

    let plan = organize::plan(&w.conn, &id, &organize::Overlay::default()).expect("plan");
    assert_eq!(
        plan.action,
        Action::Refused("required field title is empty".into())
    );

    set(&w, &id, "title", "Army of Me", T3);
    let entries = decisions::load(&w.decisions).expect("load").expect("file");
    let overlay = organize::Overlay::of(&w.conn, &entries).expect("overlay");
    assert!(
        matches!(
            organize::plan(&w.conn, &id, &overlay).expect("plan").action,
            Action::Move { .. }
        ),
        "a plan sees a set that no tick has folded yet"
    );
    fold(&mut w, T3);
    let target = format!("{}/musicvideos/Björk/Army of Me.mkv", w.root);
    let plan = organize::plan(&w.conn, &id, &organize::Overlay::default()).expect("plan");
    assert_eq!(plan.action, Action::Move { to: target.clone() });
    assert!(Path::new(&path).exists(), "planning moves nothing");

    decide(
        &w,
        Decision::Organize {
            item_id: id.clone(),
        },
        T3,
    );
    let lines = run_organize(&mut w, T4);
    assert!(lines[1].contains("move"), "{lines:?}");
    assert!(!Path::new(&path).exists());
    assert_eq!(fs::read_to_string(&target).expect("moved"), "video");
    assert_eq!(
        column(
            &w.conn,
            &format!("SELECT path FROM extra_file WHERE item_id = '{id}'")
        ),
        vec![target.clone()]
    );
    assert_eq!(
        column(&w.conn, "SELECT organized || organized_at FROM extra_item"),
        [format!("1{T4}")]
    );
    assert_eq!(
        column(
            &w.conn,
            "SELECT from_path || '>' || to_path FROM extra_moved"
        ),
        [format!("{path}>{target}")]
    );

    // The tick finds the decision again and the scan finds the file: neither moves it.
    assert_eq!(
        run_organize(&mut w, T4),
        ["extras organize: 0 decision(s) waiting"]
    );
    walk_all(&mut w.conn, &w.roots, None, T4).expect("walk");
    assert!(Path::new(&target).exists());
    assert_eq!(column(&w.conn, "SELECT COUNT(*) FROM extra_item"), ["1"]);

    let move_id: i64 = w
        .conn
        .query_row("SELECT move_id FROM extra_moved", [], |row| row.get(0))
        .expect("move");
    organize::undo(&mut w.conn, move_id, "2026-10-27T01:00:00+00:00").expect("undo");
    assert_eq!(fs::read_to_string(&path).expect("back"), "video");
    assert!(!Path::new(&target).exists());
    assert_eq!(column(&w.conn, "SELECT organized FROM extra_item"), ["0"]);
    assert_eq!(column(&w.conn, "SELECT COUNT(*) FROM extra_moved"), ["0"]);
    assert_eq!(
        run_organize(&mut w, "2026-10-27T01:10:00+00:00"),
        ["extras organize: 0 decision(s) waiting"],
        "an undone move is not redone by the decision that made it"
    );
}

#[test]
fn a_taken_target_leaves_the_extra_in_the_queue_and_a_same_content_target_drops_the_duplicate() {
    let mut w = world();
    let (path, id) = queued(&mut w, "drop.mkv", "video");
    set(&w, &id, "artist", "A", T3);
    set(&w, &id, "title", "T", T3);
    fold(&mut w, T3);
    let target = put(&w, "A/T.mkv", "someone else's file");

    decide(
        &w,
        Decision::Organize {
            item_id: id.clone(),
        },
        T3,
    );
    let lines = run_organize(&mut w, T4);
    assert!(lines[1].contains("target taken"), "{lines:?}");
    assert!(lines[1].contains("left in the queue"));
    assert!(Path::new(&path).exists());
    assert_eq!(column(&w.conn, "SELECT organized FROM extra_item"), ["0"]);

    fs::write(&target, "video").expect("same content");
    let lines = run_organize(&mut w, T4);
    assert!(lines[1].contains("duplicate"), "{lines:?}");
    assert!(!Path::new(&path).exists());
    assert_eq!(fs::read_to_string(&target).expect("kept"), "video");
    assert_eq!(
        column(&w.conn, "SELECT path FROM extra_file"),
        [target],
        "the item now owns the file that was already there"
    );
    assert_eq!(column(&w.conn, "SELECT organized FROM extra_item"), ["1"]);
}

#[test]
fn delete_is_refused_for_a_primary_file_and_removes_any_other() {
    let mut w = world();
    let (primary, id) = queued(&mut w, "a.mkv", "same bytes");
    let copy = put(&w, "copy.mkv", "same bytes");
    walk_all(&mut w.conn, &w.roots, None, T3).expect("walk");
    assert_eq!(
        column(
            &w.conn,
            &format!("SELECT COUNT(*) FROM extra_file WHERE item_id = '{id}'")
        ),
        ["2"]
    );
    let primary_now: String = w
        .conn
        .query_row(
            "SELECT path FROM extra_file WHERE primary_file = 1",
            [],
            |row| row.get(0),
        )
        .expect("primary");
    let other = if primary_now == primary {
        copy
    } else {
        primary
    };

    decide(
        &w,
        Decision::Delete {
            path: primary_now.clone(),
        },
        T3,
    );
    decide(
        &w,
        Decision::Delete {
            path: other.clone(),
        },
        T3,
    );
    let stats = fold(&mut w, T4);
    assert_eq!(stats.applied, 1);
    assert_eq!(stats.refused.len(), 1);
    assert!(stats.refused[0].contains("primary file"));
    assert!(Path::new(&primary_now).exists());
    assert!(!Path::new(&other).exists());
    assert_eq!(
        column(&w.conn, "SELECT path FROM extra_file"),
        [primary_now]
    );
}

#[test]
fn split_gives_a_merged_file_its_own_item_back() {
    let mut w = world();
    let (first, kept) = queued(&mut w, "one.mkv", "one");
    let second = put(&w, "two.mkv", "two");
    walk_all(&mut w.conn, &w.roots, None, T3).expect("walk");
    let merged: String = w
        .conn
        .query_row(
            "SELECT item_id FROM extra_file WHERE path = ?",
            [&second],
            |row| row.get(0),
        )
        .expect("second item");
    // Fold the second item into the first the way the near-duplicate step will.
    w.conn
        .execute(
            "UPDATE extra_file SET item_id = ?, primary_file = 0 WHERE path = ?",
            [&kept, &second],
        )
        .expect("merge");
    w.conn
        .execute("DELETE FROM items WHERE item_id = ?", [&merged])
        .expect("drop merged item");
    w.conn
        .execute(
            "INSERT INTO extra_merge (merged_item_id, kept_item_id, score, merged_at) VALUES (?, ?, 0.97, ?)",
            [&merged, &kept, T3],
        )
        .expect("merge row");

    decide(
        &w,
        Decision::Split {
            merged_item_id: merged.clone(),
        },
        T4,
    );
    let stats = fold(&mut w, T4);
    assert_eq!(stats.applied, 1, "{:?}", stats.refused);
    assert_eq!(
        column(
            &w.conn,
            &format!("SELECT path FROM extra_file WHERE item_id = '{merged}' AND primary_file = 1")
        ),
        [second]
    );
    assert_eq!(
        column(
            &w.conn,
            &format!("SELECT path FROM extra_file WHERE item_id = '{kept}' AND primary_file = 1")
        ),
        [first]
    );
    assert_eq!(column(&w.conn, "SELECT COUNT(*) FROM extra_merge"), ["0"]);
}

#[test]
fn a_newer_decision_on_the_same_subject_replaces_the_older() {
    let w = world();
    let id = "fs:0000000000000001";
    set(&w, id, "title", "One", T3);
    set(&w, id, "title", "Two", T4);
    set(&w, id, "artist", "X", T4);
    let entries = decisions::load(&w.decisions).expect("load").expect("file");
    assert_eq!(entries.len(), 2);
    assert_eq!(
        entries[1].decision,
        Decision::Set {
            item_id: id.into(),
            field: "artist".into(),
            value: "X".into()
        }
    );
}

#[test]
fn a_broken_file_changes_nothing() {
    let mut w = world();
    fs::write(&w.decisions, r#"[{"decision": "set", "item_id": "x"}]"#).expect("write");
    let err = decisions::fold(&mut w.conn, &w.decisions, &w.roots, T3).expect_err("refused");
    assert!(
        err.to_string().contains("needs text item_id, field, value"),
        "{err}"
    );
}

#[test]
fn a_delete_decision_never_removes_a_file_that_arrived_after_it() {
    let mut w = world();
    let (_, id) = queued(&mut w, "a.mkv", "same bytes");
    let copy = put(&w, "copy.mkv", "same bytes");
    walk_all(&mut w.conn, &w.roots, None, T3).expect("walk");
    let primary: String = w
        .conn
        .query_row(
            "SELECT path FROM extra_file WHERE primary_file = 1",
            [],
            |row| row.get(0),
        )
        .expect("primary");
    let other = if primary.ends_with("a.mkv") {
        copy
    } else {
        format!("{}/musicvideos/a.mkv", w.root)
    };
    decide(
        &w,
        Decision::Delete {
            path: other.clone(),
        },
        T2,
    );
    let stats = fold(&mut w, T4);
    assert_eq!(
        (stats.applied, stats.unmatched),
        (0, 1),
        "{id}: first seen at {T3}, decided at {T2}"
    );
    assert!(Path::new(&other).exists());
}
