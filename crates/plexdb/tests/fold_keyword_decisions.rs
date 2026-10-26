//! `fold-merge-decisions`, `fold-role-decisions` and `fold-name-decisions` against
//! a store built by the real `plexdb migrate`, so a test fails when a migration
//! changes a table under a fold.

use std::path::Path;

use plexdb::migrate::migrate;
use plexdb::{merge_decisions, name_decisions, role_decisions};
use rusqlite::Connection;
use serde_json::{Value, json};

const AT: &str = "2026-10-01T12:00:00+00:00";
const LATER: &str = "2026-10-02T12:00:00+00:00";

/// A store holding one title that carries `keywords`.
fn store(dir: &Path, keywords: &[&str]) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open the fixture store");
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'One')",
        [],
    )
    .expect("insert the title");
    for keyword in keywords {
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
             VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, ?)",
            (keyword, AT),
        )
        .expect("insert the keyword");
    }
    conn
}

fn write(path: &Path, entries: &[Value]) {
    std::fs::write(path, serde_json::to_string(entries).expect("json")).expect("write the file");
}

fn pair(a: &str, b: &str, decision: &str, at: &str) -> Value {
    json!({"surface_a": a, "surface_b": b, "decision": decision, "decided_at": at})
}

fn judged(conn: &Connection, a: &str, b: &str) {
    conn.execute(
        "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at) \
         VALUES (?, ?, 0.9, 'jev-test', ?)",
        (a, b, AT),
    )
    .expect("insert a judged pair");
}

fn pairs(conn: &Connection) -> Vec<(String, String, Option<String>, Option<String>)> {
    let mut stmt = conn
        .prepare(
            "SELECT keyword_a, keyword_b, decision, decided_at FROM keyword_pairs ORDER BY 1, 2",
        )
        .expect("prepare");
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn some(text: &str) -> Option<String> {
    Some(text.to_string())
}

#[test]
fn merge_folding_sets_clears_inserts_and_counts_decisions() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(
        dir.path(),
        &["bank robbery", "heist", "jail", "prison", "robbery"],
    );
    judged(&conn, "bank robbery", "heist");
    judged(&conn, "jail", "prison");
    let file = dir.path().join("merge_decisions.json");
    write(
        &file,
        &[
            // Spellings other than the stored ones name the same pairs, either way round.
            pair("Heists", "Bank-Robbery", "accepted", AT),
            pair("Jail", "prisons", "rejected", AT),
            pair("prison", "jail", "cleared", LATER),
            // No row, both stored: a decision-only row.
            pair("heist", "robbery", "rejected", AT),
            pair("aaa", "bbb", "accepted", AT),
            // Two spellings of one value are no pair.
            pair("Heist", "heists", "accepted", AT),
        ],
    );

    let stats = merge_decisions::fold(&mut conn, &file).expect("fold");

    assert_eq!((stats.entries, stats.matched, stats.unmatched), (6, 4, 2));
    assert_eq!(
        pairs(&conn),
        [
            (
                "bank robbery".into(),
                "heist".into(),
                some("accepted"),
                some(AT)
            ),
            ("heist".into(), "robbery".into(), some("rejected"), some(AT)),
            ("jail".into(), "prison".into(), None, None),
        ]
    );
}

#[test]
fn merge_clearing_deletes_a_row_no_judge_wrote_and_only_clears_one_a_judge_scored() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path(), &["heist", "robbery", "zebra"]);
    judged(&conn, "heist", "robbery");
    let file = dir.path().join("merge_decisions.json");
    write(
        &file,
        &[
            pair("heist", "robbery", "accepted", AT),
            pair("heist", "zebra", "accepted", AT),
        ],
    );
    merge_decisions::fold(&mut conn, &file).expect("fold");
    assert_eq!(pairs(&conn).len(), 2);

    write(
        &file,
        &[
            pair("heist", "robbery", "cleared", LATER),
            pair("heist", "zebra", "cleared", LATER),
        ],
    );
    let stats = merge_decisions::fold(&mut conn, &file).expect("fold");

    assert_eq!((stats.matched, stats.unmatched), (2, 0));
    assert_eq!(
        pairs(&conn),
        [("heist".into(), "robbery".into(), None, None)]
    );
}

#[test]
fn a_missing_decisions_file_changes_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path(), &["heist"]);
    let missing = dir.path().join("none.json");

    let merge = merge_decisions::fold(&mut conn, &missing).expect("fold");
    let role = role_decisions::fold(&mut conn, &missing).expect("fold");
    let name = name_decisions::fold(&mut conn, &missing).expect("fold");

    for stats in [merge, role, name] {
        assert_eq!((stats.file_found, stats.entries), (false, 0));
    }
}

#[test]
fn a_malformed_merge_file_is_refused_before_any_row_changes() {
    let cases: Vec<(&str, Vec<Value>, &str)> = vec![
        (
            "unknown decision",
            vec![pair("heist", "robbery", "maybe", AT)],
            "entry 1 has decision 'maybe', expected one of ('accepted', 'rejected', 'cleared')",
        ),
        (
            "one surface twice",
            vec![pair("heist", "heist", "accepted", AT)],
            "entry 1 names one surface twice",
        ),
        (
            "an empty surface",
            vec![pair("", "heist", "accepted", AT)],
            "entry 1 has an empty surface",
        ),
        (
            "an empty time",
            vec![pair("heist", "robbery", "accepted", "")],
            "entry 1 has decided_at '', expected an ISO 8601 time",
        ),
        (
            "a vague time",
            vec![pair("heist", "robbery", "accepted", "yesterday")],
            "entry 1 has decided_at 'yesterday', expected an ISO 8601 time",
        ),
        (
            "no time",
            vec![json!({"surface_a": "heist", "surface_b": "robbery", "decision": "accepted"})],
            "entry 1 needs text surface_a, surface_b, decision and decided_at",
        ),
    ];
    for (name, bad, message) in cases {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut conn = store(dir.path(), &["heist", "jail", "prison", "robbery"]);
        judged(&conn, "jail", "prison");
        let file = dir.path().join("merge_decisions.json");
        let mut entries = vec![pair("jail", "prison", "accepted", AT)];
        entries.extend(bad);
        write(&file, &entries);

        let err = merge_decisions::fold(&mut conn, &file).expect_err(name);

        assert!(err.to_string().ends_with(message), "{name}: {err}");
        assert_eq!(
            pairs(&conn),
            [("jail".into(), "prison".into(), None, None)],
            "{name}"
        );
    }
}

fn judge_role(conn: &Connection, keyword: &str, role: &str, error: Option<&str>) {
    let (score, model): (Option<f64>, Option<&str>) = match error {
        None => (Some(0.9), Some("jev-test")),
        Some(_) => (None, None),
    };
    conn.execute(
        "INSERT INTO keyword_roles (keyword, role, source, score, model, error, stated_at) \
         VALUES (?, ?, 'jev', ?, ?, ?, ?)",
        (keyword, role, score, model, error, AT),
    )
    .expect("insert a verdict");
}

fn role(surface: &str, role: &str, decision: &str, at: &str) -> Value {
    json!({"surface": surface, "role": role, "decision": decision, "decided_at": at})
}

fn role_rows(conn: &Connection) -> Vec<(String, String, String, String)> {
    let mut stmt = conn
        .prepare(
            "SELECT keyword, role, decision, decided_at FROM keyword_role_decisions ORDER BY 1, 2",
        )
        .expect("prepare");
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn role_store(dir: &Path) -> Connection {
    let conn = store(dir, &["bleak", "grief", "tokyo"]);
    judge_role(&conn, "bleak", "tone", None);
    judge_role(&conn, "grief", "theme", None);
    judge_role(&conn, "tokyo", "tone", None);
    judge_role(&conn, "odd", "tone", Some("refused"));
    conn.execute(
        "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) \
         VALUES ('grief', 'theme', 'rejected', ?)",
        [AT],
    )
    .expect("insert a decision");
    conn
}

#[test]
fn role_folding_applies_in_order_and_counts_what_has_no_verdict() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = role_store(dir.path());
    let file = dir.path().join("role_decisions.json");
    write(
        &file,
        &[
            role("bleak", "tone", "rejected", AT),
            role("Bleak", "tone", "accepted", LATER),
            role("tokyo", "tone", "accepted", AT),
            role("TOKYO", "tone", "cleared", LATER),
            // A refusal is no verdict.
            role("odd", "tone", "accepted", AT),
            role("nowhere", "era", "accepted", AT),
        ],
    );

    let stats = role_decisions::fold(&mut conn, &file).expect("fold");

    assert_eq!((stats.entries, stats.matched, stats.unmatched), (6, 4, 2));
    assert_eq!(
        role_rows(&conn),
        [
            (
                "bleak".into(),
                "tone".into(),
                "accepted".into(),
                LATER.into()
            ),
            ("grief".into(), "theme".into(), "rejected".into(), AT.into()),
        ]
    );
}

#[test]
fn an_invalid_role_file_is_refused_whole_and_changes_nothing() {
    let cases: Vec<(&str, Vec<Value>, &str)> = vec![
        (
            "unknown decision",
            vec![role("bleak", "tone", "maybe", AT)],
            "entry 1 has decision 'maybe', expected one of ('accepted', 'rejected', 'cleared')",
        ),
        (
            "unknown role",
            vec![role("bleak", "mood", "accepted", AT)],
            "entry 1 has role 'mood', expected one of ('tone', 'era', 'region', 'theme', 'character_trait')",
        ),
        (
            "an empty surface",
            vec![role("", "tone", "accepted", AT)],
            "entry 1 has an empty surface",
        ),
        (
            "a vague time",
            vec![role("bleak", "tone", "accepted", "yesterday")],
            "entry 1 has decided_at 'yesterday', expected an ISO 8601 time",
        ),
        (
            "no time",
            vec![json!({"surface": "bleak", "role": "tone", "decision": "accepted"})],
            "entry 1 needs text surface, role, decision and decided_at",
        ),
    ];
    for (name, bad, message) in cases {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut conn = role_store(dir.path());
        let file = dir.path().join("role_decisions.json");
        let mut entries = vec![role("bleak", "tone", "accepted", AT)];
        entries.extend(bad);
        write(&file, &entries);

        let err = role_decisions::fold(&mut conn, &file).expect_err(name);

        assert!(err.to_string().ends_with(message), "{name}: {err}");
        assert_eq!(role_rows(&conn).len(), 1, "{name}");
    }
    // Not JSON, and not a list.
    for (text, message) in [
        ("{not json", "is not valid JSON"),
        (r#"{"not": "a list"}"#, "does not hold a list of decisions"),
    ] {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut conn = role_store(dir.path());
        let file = dir.path().join("role_decisions.json");
        std::fs::write(&file, text).expect("write");
        let err = role_decisions::fold(&mut conn, &file).expect_err(text);
        assert!(err.to_string().contains(message), "{text}: {err}");
    }
}

fn named(surface: &str, name: &str, at: &str) -> Value {
    json!({"surface": surface, "decision": "named", "name": name, "decided_at": at})
}

fn cleared(surface: &str, at: &str) -> Value {
    json!({"surface": surface, "decision": "cleared", "decided_at": at})
}

fn names(conn: &Connection) -> Vec<(String, String)> {
    let mut stmt = conn
        .prepare("SELECT keyword, name FROM keyword_names ORDER BY 1")
        .expect("prepare");
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

#[test]
fn name_folding_writes_clears_and_counts_a_keyword_no_title_carries() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path(), &["age gap", "bank robbery", "heist"]);
    let file = dir.path().join("name_decisions.json");
    write(
        &file,
        &[
            named("Heists", "Heist", AT),
            named("Bank Robbery", "Robbery", AT),
            cleared("Bank Robbery", LATER),
            named("Age Gap", "May–December", AT),
            named("zeppelins", "Airships", AT),
        ],
    );

    let stats = name_decisions::fold(&mut conn, &file).expect("fold");

    assert_eq!((stats.entries, stats.matched, stats.unmatched), (5, 4, 1));
    assert_eq!(
        names(&conn),
        [
            ("age gap".into(), "May–December".into()),
            ("heist".into(), "Heist".into())
        ]
    );
}

#[test]
fn a_named_entry_without_a_usable_name_refuses_the_whole_file() {
    let too_long = "x".repeat(201);
    let cases: Vec<(Value, &str)> = vec![
        (
            json!({"surface": "heist", "decision": "named", "decided_at": AT}),
            "entry 1 needs text name",
        ),
        (named("heist", "   ", AT), "entry 1 has a blank name"),
        (
            named("heist", &too_long, AT),
            "entry 1 has a name over 200 characters",
        ),
    ];
    for (bad, message) in cases {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut conn = store(dir.path(), &["heist"]);
        let file = dir.path().join("name_decisions.json");
        write(&file, &[named("Heists", "Heist", AT), bad]);

        let err = name_decisions::fold(&mut conn, &file).expect_err(message);

        assert!(err.to_string().ends_with(message), "{message}: {err}");
        assert!(names(&conn).is_empty(), "{message}");
    }
}

#[test]
fn a_name_of_exactly_200_characters_is_stored() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path(), &["heist"]);
    let file = dir.path().join("name_decisions.json");
    let name = "é".repeat(200);
    write(&file, &[named("heist", &name, AT)]);

    name_decisions::fold(&mut conn, &file).expect("fold");

    assert_eq!(names(&conn), [("heist".into(), name)]);
}
