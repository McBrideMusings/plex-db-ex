//! The chain builds every table a current store carries, stops cleanly where it
//! cannot go on, and each migration carries an older store's rows forward as it
//! shipped. A store at an older version is built by applying a prefix of the
//! chain, so each step runs against the shape it really met.

use std::collections::BTreeSet;

use plexdb_schema::{
    MIGRATIONS, Migration, READER_SHAPE, SCHEMA_VERSION, SchemaError, Shrinks, apply, apply_chain,
    current_version,
};
use rusqlite::Connection;

fn open() -> Connection {
    let conn = Connection::open_in_memory().expect("open");
    conn.pragma_update(None, "foreign_keys", "ON")
        .expect("foreign keys on, as the writer opens the store");
    conn
}

fn store_at(version: usize) -> Connection {
    let mut conn = open();
    apply_chain(&mut conn, &MIGRATIONS[..version]).expect("apply the prefix");
    conn
}

fn tables(conn: &Connection) -> BTreeSet<String> {
    let mut stmt = conn
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
        .expect("prepare");
    stmt.query_map([], |row| row.get(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn columns(conn: &Connection, table: &str) -> BTreeSet<String> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .expect("prepare");
    stmt.query_map([], |row| row.get(1))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn rows<T: rusqlite::types::FromSql>(conn: &Connection, sql: &str) -> Vec<Vec<Option<T>>> {
    let mut stmt = conn.prepare(sql).expect("prepare");
    let width = stmt.column_count();
    stmt.query_map([], |row| (0..width).map(|i| row.get(i)).collect())
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn text(conn: &Connection, sql: &str) -> Vec<Vec<Option<String>>> {
    rows(conn, sql)
}

/// Where a run started and ended, whatever shrinks its steps declared.
fn landed(result: Result<(i64, i64, Shrinks), SchemaError>) -> (i64, i64) {
    let (was, now, _) = result.expect("apply");
    (was, now)
}

fn s(value: &str) -> Option<String> {
    Some(value.to_string())
}

#[test]
fn an_empty_store_reaches_every_table_the_schema_holds_and_nothing_else() {
    let mut conn = open();

    assert_eq!(landed(apply(&mut conn)), (0, SCHEMA_VERSION));

    let want: BTreeSet<String> = [
        "items",
        "external_ids",
        "plex_items",
        "enrichment",
        "plays",
        "plays_ingest_cursor",
        "edges",
        "collection",
        "collection_membership",
        "enrichment_cursor",
        "title_map",
        "title_map_state",
        "keyword_forms",
        "tag_network",
        "tag_network_edge",
        "tag_network_state",
        "keyword_pairs",
        "keyword_roles",
        "keyword_role_decisions",
        "keyword_role_statements",
        "keyword_surfaces",
        "keyword_names",
        "title_map_label",
        "collection_entry",
        "list_decision",
        "list_attempt",
        "media_part",
        "media_stream",
        "media_check",
        "reader_shape",
        "schema_version",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    assert_eq!(tables(&conn), want);
    assert_eq!(
        text(&conn, "SELECT CAST(version AS TEXT) FROM reader_shape"),
        vec![vec![Some(READER_SHAPE.to_string())]]
    );
    for (table, needed) in [
        ("items", &["item_id", "type", "title", "year"][..]),
        (
            "external_ids",
            &["item_id", "ns", "value", "kind", "last_seen"],
        ),
        ("plex_items", &["rating_key", "item_id", "section_id"]),
        (
            "enrichment",
            &[
                "item_id",
                "namespace",
                "source",
                "key",
                "value",
                "fetched_at",
                "rank",
            ],
        ),
        (
            "plays",
            &[
                "history_key",
                "item_id",
                "plex_account_id",
                "viewed_at",
                "seconds_watched",
                "tautulli_id",
                "rating_key",
            ],
        ),
        (
            "edges",
            &["from_id", "to_id", "edge_type", "rank", "fetched_at"],
        ),
    ] {
        let have = columns(&conn, table);
        for column in needed {
            assert!(have.contains(*column), "{table} lacks {column}");
        }
    }
}

#[test]
fn applying_a_current_store_changes_nothing() {
    let mut conn = open();
    apply(&mut conn).expect("apply");
    let changes = conn.total_changes();

    assert_eq!(landed(apply(&mut conn)), (SCHEMA_VERSION, SCHEMA_VERSION));
    assert_eq!(conn.total_changes(), changes);
}

#[test]
fn a_store_from_the_future_is_refused() {
    let mut conn = open();
    apply(&mut conn).expect("apply");
    conn.execute(
        "UPDATE schema_version SET version = ?1",
        [SCHEMA_VERSION + 5],
    )
    .expect("stamp");

    let err = apply(&mut conn).expect_err("a newer store");
    assert!(err.to_string().contains("only understands"), "{err}");
}

#[test]
fn a_store_missing_its_version_row_is_damaged_not_empty() {
    let mut conn = open();
    apply(&mut conn).expect("apply");
    conn.execute("DELETE FROM schema_version", [])
        .expect("delete");

    let err = apply(&mut conn).expect_err("a damaged store");
    assert!(err.to_string().contains("damaged"), "{err}");
}

fn broken(_: &rusqlite::Transaction) -> Result<Shrinks, SchemaError> {
    Err(SchemaError::Refused("this step always fails".into()))
}

#[test]
fn a_step_that_fails_leaves_the_store_at_the_last_step_that_landed() {
    let mut conn = store_at(2);
    let mut chain: Vec<Migration> = Vec::new();
    for migration in &MIGRATIONS[..3] {
        chain.push(match migration {
            Migration::Sql(sql) => Migration::Sql(sql),
            Migration::Step(step) => Migration::Step(*step),
        });
    }
    chain.push(Migration::Sql("CREATE TABLE half_built (id INTEGER);"));
    chain.push(Migration::Step(broken));

    assert!(apply_chain(&mut conn, &chain).is_err());

    assert_eq!(current_version(&conn).expect("version"), 4);
    assert!(tables(&conn).contains("half_built"), "step 4 landed whole");
    assert!(tables(&conn).contains("edges"), "step 3 landed whole");
}

#[test]
fn a_sql_step_that_fails_partway_leaves_none_of_its_tables() {
    let mut conn = open();
    let chain = [Migration::Sql(
        "CREATE TABLE first (id INTEGER); CREATE TABLE first (id INTEGER);",
    )];

    assert!(apply_chain(&mut conn, &chain).is_err());

    assert_eq!(current_version(&conn).expect("version"), 0);
    assert!(!tables(&conn).contains("first"));
}

#[test]
fn v5_takes_an_existing_ids_kind_from_its_items_type() {
    let mut conn = store_at(4);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('m', 'movie', 'Godzilla');
         INSERT INTO items (item_id, type, title) VALUES ('s', 'show', 'Golden');
         INSERT INTO external_ids (item_id, ns, value) VALUES ('m', 'imdb', 'tt0047034');
         INSERT INTO external_ids (item_id, ns, value) VALUES ('s', 'imdb', 'tt0088526');",
    )
    .expect("seed");

    assert_eq!(landed(apply(&mut conn)), (4, SCHEMA_VERSION));

    assert_eq!(
        text(
            &conn,
            "SELECT item_id, kind FROM external_ids ORDER BY item_id"
        ),
        vec![vec![s("m"), s("movie")], vec![s("s"), s("show")]]
    );
}

#[test]
fn v5_backfills_a_plays_rating_key_only_when_it_is_unambiguous() {
    // An identity mapping to two rating keys is a fused one (issue #23), where the
    // answer is unknowable and `repair-identities` re-ingests.
    let mut conn = store_at(4);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('one', 'movie', 'One Key');
         INSERT INTO items (item_id, type, title) VALUES ('two', 'movie', 'Fused');
         INSERT INTO plex_items (rating_key, item_id, section_id, last_seen)
             VALUES ('11', 'one', '1', '2024-01-01T00:00:00+00:00');
         INSERT INTO plex_items (rating_key, item_id, section_id, last_seen)
             VALUES ('21', 'two', '1', '2024-01-01T00:00:00+00:00');
         INSERT INTO plex_items (rating_key, item_id, section_id, last_seen)
             VALUES ('22', 'two', '2', '2024-01-01T00:00:00+00:00');
         INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at)
             VALUES ('h1', 'one', 1, 1000);
         INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at)
             VALUES ('h2', 'two', 1, 2000);",
    )
    .expect("seed");

    apply(&mut conn).expect("apply");

    assert_eq!(
        text(
            &conn,
            "SELECT history_key, rating_key FROM plays ORDER BY history_key"
        ),
        vec![vec![s("h1"), s("11")], vec![s("h2"), None]]
    );
}

#[test]
fn v7_moves_every_bookkeeping_row_out_of_enrichment() {
    // The migration that made `enrichment` a facts-only table (issue #41).
    let mut conn = store_at(6);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'X');
         INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) VALUES
             ('imdb:tt1', 'tmdb_keywords', 'keyword', 'heist', '2026-01-01T00:00:00+00:00'),
             ('imdb:tt1', 'tmdb_keywords', '_fetched', '1', '2026-01-02T00:00:00+00:00'),
             ('imdb:tt1', 'tmdb_edges', '_fetched_recommendations', '1', '2026-01-03T00:00:00+00:00'),
             ('imdb:tt1', 'tmdb_edges', '_fetched_similar', '1', '2026-01-04T00:00:00+00:00');",
    )
    .expect("seed");

    apply_chain(&mut conn, &MIGRATIONS[..7]).expect("apply v7");

    assert_eq!(
        text(&conn, "SELECT namespace, key, value FROM enrichment"),
        vec![vec![s("tmdb_keywords"), s("keyword"), s("heist")]]
    );
    // Each cursor keeps its namespace, loses the `_`, and keeps its fetched_at,
    // so no sweep re-fetches anything.
    assert_eq!(
        text(
            &conn,
            "SELECT namespace, key, fetched_at FROM enrichment_cursor ORDER BY namespace, key"
        ),
        vec![
            vec![
                s("tmdb_edges"),
                s("fetched_recommendations"),
                s("2026-01-03T00:00:00+00:00")
            ],
            vec![
                s("tmdb_edges"),
                s("fetched_similar"),
                s("2026-01-04T00:00:00+00:00")
            ],
            vec![
                s("tmdb_keywords"),
                s("fetched"),
                s("2026-01-02T00:00:00+00:00")
            ],
        ]
    );
}

#[test]
fn v8_backfills_last_seen_on_every_existing_external_id() {
    let mut conn = store_at(7);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'X');
         INSERT INTO external_ids (item_id, ns, value, kind)
             VALUES ('imdb:tt1', 'imdb', 'tt1', 'movie');",
    )
    .expect("seed");

    apply_chain(&mut conn, &MIGRATIONS[..8]).expect("apply v8");

    let found = text(&conn, "SELECT ns, value, last_seen FROM external_ids");
    assert_eq!(found.len(), 1);
    assert_eq!(found[0][..2], [s("imdb"), s("tt1")]);
    assert!(found[0][2].is_some(), "last_seen backfilled");
}

#[test]
fn v10_gives_each_fact_and_cursor_its_source() {
    // ADR-0016. A non-keyword fact takes its own namespace as its source; both
    // known cursor namespaces came from TMDB, and the keyword one is renamed.
    let mut conn = store_at(9);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat');
         INSERT INTO enrichment (item_id, namespace, key, value, fetched_at)
             VALUES ('imdb:tt1', 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00');
         INSERT INTO enrichment_cursor (item_id, namespace, key, fetched_at) VALUES
             ('imdb:tt1', 'tmdb_keywords', 'fetched', '2026-01-02T00:00:00+00:00'),
             ('imdb:tt1', 'tmdb_edges', 'fetched_recommendations', '2026-01-02T00:00:00+00:00');",
    )
    .expect("seed");

    apply_chain(&mut conn, &MIGRATIONS[..10]).expect("apply v10");

    assert_eq!(
        text(
            &conn,
            "SELECT namespace, source, key, value FROM enrichment"
        ),
        vec![vec![s("mdblist"), s("mdblist"), s("list"), s("top250")]]
    );
    assert_eq!(
        text(
            &conn,
            "SELECT namespace, source, key FROM enrichment_cursor ORDER BY namespace"
        ),
        vec![
            vec![s("keywords"), s("tmdb"), s("fetched")],
            vec![s("tmdb_edges"), s("tmdb"), s("fetched_recommendations")],
        ]
    );
}

#[test]
fn v13_carries_every_keyword_pair_over_and_accepts_a_refused_pair() {
    let mut conn = store_at(12);
    conn.execute(
        "INSERT INTO keyword_pairs VALUES ('hippo', 'hippopotamus', 0.95, 'jev-1.13.0', \
         '2026-10-01T00:00:00Z', 'accepted', '2026-10-01T01:00:00Z')",
        [],
    )
    .expect("seed");
    let pairs = "SELECT keyword_a, keyword_b, CAST(jev_score AS TEXT), jev_model, judged_at, \
                 decision, decided_at FROM keyword_pairs";
    let before = text(&conn, pairs);

    apply(&mut conn).expect("apply");

    assert_eq!(text(&conn, pairs), before);
    conn.execute(
        "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_error, judged_at) \
         VALUES ('a', 'b', 'Jev returned 400', '2026-10-01T00:00:00Z')",
        [],
    )
    .expect("a refusal is a row");
    for bad in [
        "('c', 'd', NULL, NULL, NULL, '2026-10-01T00:00:00Z')",
        "('c', 'd', 0.5, 'jev-1.13.0', 'Jev returned 400', '2026-10-01T00:00:00Z')",
        "('c', 'd', 0.5, NULL, NULL, '2026-10-01T00:00:00Z')",
    ] {
        let insert = format!(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, jev_error, \
             judged_at) VALUES {bad}"
        );
        assert!(conn.execute(&insert, []).is_err(), "{bad} was accepted");
    }
}

#[test]
fn v14_adds_a_null_rank_to_every_existing_enrichment_row() {
    let mut conn = store_at(13);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat');
         INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) VALUES
             ('imdb:tt1', 'mdblist', 'mdblist', 'list', 'top250', '2026-10-01T00:00:00Z');",
    )
    .expect("seed");

    apply_chain(&mut conn, &MIGRATIONS[..14]).expect("apply v14");

    assert_eq!(
        text(&conn, "SELECT value, CAST(rank AS TEXT) FROM enrichment"),
        vec![vec![s("top250"), None]]
    );
}

#[test]
fn v16_makes_every_wikidata_title_due_and_keeps_its_rows() {
    let mut conn = store_at(15);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'x');
         INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at)
             VALUES ('imdb:tt1', 'wikidata', 'wikidata', 'qid', 'Q1', '2026-10-01T00:00:00Z');
         INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) VALUES
             ('imdb:tt1', 'keywords', 'wikidata', 'fetched', '2026-10-01T00:00:00Z'),
             ('imdb:tt1', 'keywords', 'tmdb', 'fetched', '2026-10-01T00:00:00Z');",
    )
    .expect("seed");

    apply_chain(&mut conn, &MIGRATIONS[..16]).expect("apply v16");

    assert_eq!(
        text(&conn, "SELECT source FROM enrichment_cursor"),
        vec![vec![s("tmdb")]]
    );
    assert_eq!(
        text(&conn, "SELECT value FROM enrichment"),
        vec![vec![s("Q1")]]
    );
}

#[test]
fn v21_keeps_every_stored_list_and_marks_it_as_a_feed_list() {
    // ADR-0027: a list stored before `collection.provenance` came from the
    // source's own top lists.
    let mut conn = store_at(20);
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, observed_at) \
         VALUES ('mdblist:1', 'mdblist', 'Old', '2026-01-01T00:00:00+00:00')",
        [],
    )
    .expect("seed");

    apply(&mut conn).expect("apply");

    assert_eq!(
        text(
            &conn,
            "SELECT collection_id, provenance, CAST(seed_count AS TEXT) FROM collection"
        ),
        vec![vec![s("mdblist:1"), s("feed"), None]]
    );
}

const AT: &str = "2026-10-01T00:00:00Z";

#[test]
fn v10_moves_tmdb_keywords_to_one_namespace_merging_spellings_that_collide() {
    // ADR-0016. Two spellings of one idea on one title collapse to one row that keeps
    // the newer `fetched_at`; the raw spellings are recorded; the step declares the
    // exact row count it landed at.
    let mut conn = store_at(9);
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat');
         INSERT INTO items (item_id, type, title) VALUES ('imdb:tt2', 'movie', 'Ronin');
         INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) VALUES
             ('imdb:tt1', 'tmdb_keywords', 'keyword', 'Heists', '2026-01-01T00:00:00+00:00'),
             ('imdb:tt1', 'tmdb_keywords', 'keyword', 'heist', '2026-01-02T00:00:00+00:00'),
             ('imdb:tt1', 'tmdb_keywords', 'keyword', 'bank-heist', '2026-01-03T00:00:00+00:00'),
             ('imdb:tt2', 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00');",
    )
    .expect("seed");

    let (_, _, declared) = apply_chain(&mut conn, &MIGRATIONS[..10]).expect("apply v10");

    assert_eq!(declared, vec![("enrichment", 3)]);
    assert_eq!(
        text(
            &conn,
            "SELECT item_id, namespace, source, key, value, fetched_at FROM enrichment \
             ORDER BY item_id, value"
        ),
        vec![
            vec![
                s("imdb:tt1"),
                s("keywords"),
                s("tmdb"),
                s("keyword"),
                s("bank heist"),
                s("2026-01-03T00:00:00+00:00")
            ],
            // The two colliding rows merged into one, keeping the newer fetched_at.
            vec![
                s("imdb:tt1"),
                s("keywords"),
                s("tmdb"),
                s("keyword"),
                s("heist"),
                s("2026-01-02T00:00:00+00:00")
            ],
            // No known writer other than tmdb_keywords existed, so the namespace names its own source.
            vec![
                s("imdb:tt2"),
                s("mdblist"),
                s("mdblist"),
                s("list"),
                s("top250"),
                s("2024-01-01T00:00:00+00:00")
            ],
        ]
    );
    assert_eq!(
        text(
            &conn,
            "SELECT surface, keyword FROM keyword_forms ORDER BY surface"
        ),
        vec![
            vec![s("Heists"), s("heist")],
            vec![s("bank-heist"), s("bank heist")],
            vec![s("heist"), s("heist")],
        ]
    );
}

#[test]
fn v17_renames_a_stem_with_one_new_form_and_refetches_one_that_splits() {
    // `christma` has one spelling, so it becomes `christmas` and its pair moves with
    // it; `anim` was both Animals and Anime, so its row stays and its title is
    // fetched again. Nothing else is lost.
    let mut conn = store_at(16);
    conn.execute_batch(&format!(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Elf');
         INSERT INTO items (item_id, type, title) VALUES ('imdb:tt2', 'movie', 'Akira');
         INSERT INTO keyword_forms (surface, keyword) VALUES
             ('Christmas', 'christma'), ('Christmas film', 'christma film'),
             ('Animals', 'anim'), ('Anime', 'anim');
         INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) VALUES
             ('imdb:tt1', 'keywords', 'tmdb', 'keyword', 'christma', '{AT}'),
             ('imdb:tt1', 'keywords', 'wikidata', 'keyword', 'christma film', '{AT}'),
             ('imdb:tt2', 'keywords', 'tmdb', 'keyword', 'anim', '{AT}');
         INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) VALUES
             ('imdb:tt1', 'keywords', 'tmdb', 'fetched', '{AT}'),
             ('imdb:tt2', 'keywords', 'tmdb', 'fetched', '{AT}');
         INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at)
             VALUES ('christma', 'christma film', 0.09, 'jev-1', '{AT}');"
    ))
    .expect("seed");

    let (_, _, declared) = apply(&mut conn).expect("apply");

    assert_eq!(declared.last(), Some(&("enrichment", 3)));
    assert_eq!(
        text(&conn, "SELECT item_id, value FROM enrichment ORDER BY 1, 2"),
        vec![
            vec![s("imdb:tt1"), s("christmas")],
            vec![s("imdb:tt1"), s("christmas film")],
            vec![s("imdb:tt2"), s("anim")],
        ]
    );
    assert_eq!(
        text(&conn, "SELECT item_id FROM enrichment_cursor"),
        vec![vec![s("imdb:tt1")]]
    );
    assert_eq!(
        text(&conn, "SELECT keyword_a, keyword_b FROM keyword_pairs"),
        vec![vec![s("christmas"), s("christmas film")]]
    );
    assert_eq!(
        text(
            &conn,
            "SELECT surface, keyword FROM keyword_forms ORDER BY surface"
        ),
        vec![
            vec![s("Animals"), s("animal")],
            vec![s("Anime"), s("anime")],
            vec![s("Christmas"), s("christmas")],
            vec![s("Christmas film"), s("christmas film")],
        ]
    );
    assert_eq!(
        text(
            &conn,
            "SELECT item_id, surface FROM keyword_surfaces ORDER BY 1, 2"
        ),
        vec![
            vec![s("imdb:tt1"), s("Christmas")],
            vec![s("imdb:tt1"), s("Christmas film")],
            vec![s("imdb:tt2"), s("anim")],
        ]
    );
}
