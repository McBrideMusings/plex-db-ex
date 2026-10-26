//! `fold-list-decisions` against a store built by the real `plexdb migrate`, so
//! the test fails when a migration changes `list_decision` under the fold.

use std::path::Path;

use plexdb::list_decisions::fold;
use plexdb::migrate::migrate;
use rusqlite::Connection;

const MINE: &str = "https://mdblist.com/lists/lina/top-watched";
const AT: &str = "2026-10-01T00:00:00+00:00";
const LATER: &str = "2026-10-02T00:00:00+00:00";

fn store(dir: &Path) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    Connection::open(&path).expect("open the fixture store")
}

fn write(path: &Path, entries: &[(&str, &str, &str)]) {
    let body: Vec<_> = entries
        .iter()
        .map(|(url, decision, at)| {
            serde_json::json!({"url": url, "decision": decision, "decided_at": at})
        })
        .collect();
    std::fs::write(path, serde_json::to_string(&body).expect("json")).expect("write the file");
}

fn row(conn: &Connection) -> Vec<(String, String, i64)> {
    let mut stmt = conn
        .prepare("SELECT url, decision, added FROM list_decision ORDER BY url")
        .expect("prepare");
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

#[test]
fn an_added_list_stays_added_through_a_refetch_until_it_is_removed() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let file = dir.path().join("list_decisions.json");

    // A spelling other than the stored one names the same row.
    write(
        &file,
        &[(
            "https://www.mdblist.com/lists/Lina/Top-Watched/",
            "added",
            AT,
        )],
    );
    let stats = fold(&mut conn, &file).expect("fold");
    assert_eq!((stats.entries, stats.matched, stats.unmatched), (1, 1, 0));
    assert_eq!(row(&conn), [(MINE.into(), "added".into(), 1)]);

    write(&file, &[(MINE, "refetch", LATER)]);
    fold(&mut conn, &file).expect("fold");
    assert_eq!(row(&conn), [(MINE.into(), "refetch".into(), 1)]);

    write(&file, &[(MINE, "removed", LATER)]);
    fold(&mut conn, &file).expect("fold");
    assert_eq!(row(&conn), [(MINE.into(), "removed".into(), 0)]);
}

#[test]
fn a_file_with_one_bad_entry_changes_no_row() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    let file = dir.path().join("list_decisions.json");
    write(&file, &[(MINE, "added", AT)]);
    fold(&mut conn, &file).expect("fold");

    write(
        &file,
        &[
            (MINE, "removed", LATER),
            ("https://example.com/x", "added", LATER),
        ],
    );
    let err = fold(&mut conn, &file).expect_err("the second entry names no list");

    assert!(
        err.to_string()
            .ends_with("entry 1 has a url that is not an MDBList or Letterboxd list"),
        "{err}"
    );
    assert_eq!(row(&conn), [(MINE.into(), "added".into(), 1)]);
}
