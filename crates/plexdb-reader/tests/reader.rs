//! Integration tests: every accessor round-tripped against a fixture store
//! built by this repository's own writer (see `tests/common/mod.rs`). No
//! live Plex, no network, no consumer repository.

mod common;

use plexdb_reader::{Reader, ReaderError};

#[test]
fn enrichment_round_trips_filtered_by_namespace() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);

    let reader = Reader::open(&path).expect("open the fixture store");
    let facts = reader
        .enrichment_for("imdb:tt1", "tmdb_keywords")
        .expect("query enrichment");

    let values: Vec<&str> = facts.iter().map(|f| f.value.as_str()).collect();
    assert_eq!(values, vec!["ensemble cast", "heist"]);
    assert!(facts.iter().all(|f| f.namespace == "tmdb_keywords"));

    // A namespace with nothing recorded comes back empty, not an error.
    let empty = reader
        .enrichment_for("imdb:tt1", "no_such_namespace")
        .expect("query an empty namespace");
    assert!(empty.is_empty());
}

#[test]
fn edges_are_queryable_in_both_directions_filtered_by_type() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);

    let reader = Reader::open(&path).expect("open the fixture store");

    let outgoing = reader
        .edges_from("imdb:tt1", "tmdb_similar")
        .expect("query outgoing edges");
    assert_eq!(
        outgoing
            .iter()
            .map(|e| e.to_id.as_str())
            .collect::<Vec<_>>(),
        vec!["imdb:tt2", "imdb:tt3"],
        "ranked ascending by the source's own rank"
    );

    let incoming = reader
        .edges_to("imdb:tt1", "tmdb_recommendations")
        .expect("query incoming edges");
    assert_eq!(incoming.len(), 1);
    assert_eq!(incoming[0].from_id, "imdb:tt2");

    // Filtered by type: tt1 -> {tt2, tt3} are `tmdb_similar`, not
    // `tmdb_recommendations` — a wrong-type query must not leak them.
    let none = reader
        .edges_from("imdb:tt1", "tmdb_recommendations")
        .expect("query a type with no matches");
    assert!(none.is_empty());
}

#[test]
fn the_taste_vector_is_an_unweighted_rollup_and_is_stable_across_calls() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);

    let reader = Reader::open(&path).expect("open the fixture store");
    let first = reader
        .taste_vector_for(42)
        .expect("compute the taste vector");
    let second = reader
        .taste_vector_for(42)
        .expect("compute it again from unchanged input");
    assert_eq!(
        first, second,
        "unchanged input must roll up to the same vector"
    );

    let heist = first
        .iter()
        .find(|a| a.namespace == "tmdb_keywords" && a.value == "heist")
        .expect("account 42 watched two distinct titles carrying `heist`");
    assert_eq!(
        heist.weight, 2,
        "tt1 and tt2 both carry `heist`; the tt1 rewatch must not inflate it"
    );

    let ensemble = first
        .iter()
        .find(|a| a.value == "ensemble cast")
        .expect("tt1 carries `ensemble cast` and was watched once");
    assert_eq!(ensemble.weight, 1);

    assert!(
        first.iter().all(|a| a.value != "space"),
        "tt3 (the only title carrying `space`) was never played by account 42"
    );

    // An account with no plays at all gets an empty vector, not an error.
    let nobody = reader
        .taste_vector_for(9999)
        .expect("compute the vector for an account with no plays");
    assert!(nobody.is_empty());
}

#[test]
fn opening_a_store_of_an_unknown_schema_version_fails_naming_both_versions() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    let conn = common::build_fixture(&path);
    conn.execute("UPDATE schema_version SET version = 99", [])
        .expect("force the store to an unknown version");
    drop(conn);

    let err = Reader::open(&path).expect_err("a version this build does not understand must fail");
    let message = err.to_string();
    assert!(
        message.contains("99"),
        "error must name the store's actual version: {message}"
    );
    assert!(
        message.contains(&plexdb_reader::SUPPORTED_SCHEMA_VERSION.to_string()),
        "error must name the version this build understands: {message}"
    );
}

#[test]
fn opening_a_missing_store_fails_loudly() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("absent.db");

    let err = Reader::open(&path).expect_err("a missing store must not open");
    assert!(
        matches!(err, ReaderError::Open { .. }),
        "expected ReaderError::Open, got {err:?}"
    );
}

#[test]
fn opening_a_file_with_no_schema_version_table_says_so() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("not_a_store.db");
    rusqlite::Connection::open(&path)
        .expect("open a plain sqlite file")
        .execute_batch("CREATE TABLE unrelated (id INTEGER);")
        .expect("give it some schema that isn't plexdb's");

    let err = Reader::open(&path).expect_err("a non-plexdb sqlite file must not open");
    assert!(
        matches!(err, ReaderError::NotAStore { .. }),
        "expected ReaderError::NotAStore, got {err:?}"
    );
}
