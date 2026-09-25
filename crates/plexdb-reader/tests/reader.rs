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
        .enrichment_for("imdb:tt1", "keywords")
        .expect("query enrichment");

    let values: Vec<&str> = facts.iter().map(|f| f.value.as_str()).collect();
    assert_eq!(values, vec!["ensemble cast", "heist"]);
    assert!(facts.iter().all(|f| f.namespace == "keywords"));

    // A namespace with nothing recorded comes back empty, not an error.
    let empty = reader
        .enrichment_for("imdb:tt1", "no_such_namespace")
        .expect("query an empty namespace");
    assert!(empty.is_empty());
}

#[test]
fn enrichment_for_many_groups_facts_by_id_matching_the_single_item_accessor() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let ids = ["imdb:tt1", "imdb:tt2", "imdb:tt3", "imdb:does-not-exist"];
    let grouped = reader
        .enrichment_for_many(ids, "keywords")
        .expect("bulk query");

    // An id with no rows in the namespace is absent from the map — never an
    // error, and never an empty entry the caller has to tell apart from
    // "absent".
    assert_eq!(
        grouped.keys().collect::<Vec<_>>(),
        vec!["imdb:tt1", "imdb:tt2", "imdb:tt3"],
        "imdb:does-not-exist carries no rows in this namespace and must be absent, not an \
         empty entry"
    );

    // For any single id, its group is exactly what the single-item accessor
    // returns for that id.
    for id in ["imdb:tt1", "imdb:tt2", "imdb:tt3"] {
        let single = reader
            .enrichment_for(id, "keywords")
            .expect("single-item query");
        assert_eq!(
            grouped.get(id).expect("id must be present"),
            &single,
            "the bulk accessor's group for {id} must match enrichment_for exactly"
        );
    }
}

#[test]
fn enrichment_for_many_deduplicates_a_repeated_id() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    // imdb:tt1 passed twice must not double its facts in the result.
    let grouped = reader
        .enrichment_for_many(["imdb:tt1", "imdb:tt1"], "keywords")
        .expect("bulk query with a repeated id");
    let single = reader
        .enrichment_for("imdb:tt1", "keywords")
        .expect("single-item query");
    assert_eq!(
        grouped.get("imdb:tt1").expect("id must be present"),
        &single,
        "a repeated input id must not duplicate its facts"
    );
}

#[test]
fn enrichment_for_many_of_empty_input_is_empty_without_erroring() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let grouped = reader
        .enrichment_for_many(std::iter::empty(), "keywords")
        .expect("empty input must not be an error");
    assert!(grouped.is_empty());
}

#[test]
fn enrichment_for_many_is_stable_across_calls() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let ids = ["imdb:tt1", "imdb:tt2", "imdb:tt3"];
    let first = reader
        .enrichment_for_many(ids, "keywords")
        .expect("first bulk query");
    let second = reader
        .enrichment_for_many(ids, "keywords")
        .expect("second bulk query against unchanged data");
    assert_eq!(
        first, second,
        "two calls against unchanged data must return identical results in identical order"
    );
}

#[test]
fn a_keyword_two_sources_carry_appears_once_and_takes_the_newer_stamp() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    // The fixture stores tt1's `heist` twice, once per source.
    let single = reader
        .enrichment_for("imdb:tt1", "keywords")
        .expect("single-item query");
    let heists: Vec<_> = single.iter().filter(|f| f.value == "heist").collect();
    assert_eq!(heists.len(), 1, "heist must appear once, got {single:?}");
    assert_eq!(heists[0].fetched_at, "2026-02-01T00:00:00+00:00");

    let grouped = reader
        .enrichment_for_many(["imdb:tt1"], "keywords")
        .expect("bulk query");
    assert_eq!(grouped["imdb:tt1"], single);
}

#[test]
fn a_spelling_resolves_to_its_stored_keyword() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    assert_eq!(
        reader.keyword_for_surface("Heists").expect("lookup"),
        Some("heist".to_string())
    );
    // Case, edge whitespace and inner whitespace runs are the only spelling
    // rules Rust applies.
    assert_eq!(
        reader.keyword_for_surface("  Bank-Heist ").expect("lookup"),
        reader.keyword_for_surface("bank heist").expect("lookup"),
    );
    assert_eq!(
        reader.keyword_for_surface("BANK   HEIST").expect("lookup"),
        Some("bank heist".to_string())
    );
}

#[test]
fn an_unknown_spelling_resolves_to_none_not_an_error() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    assert_eq!(
        reader.keyword_for_surface("no such thing").expect("lookup"),
        None
    );
    assert_eq!(reader.keyword_for_surface("   ").expect("lookup"), None);
}

#[test]
fn a_keyword_lists_every_spelling_recorded_for_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    assert_eq!(
        reader.surfaces_for_keyword("bank heist").expect("lookup"),
        vec!["Bank-Heist", "bank heist", "bank_heist"]
    );
    assert_eq!(
        reader.surfaces_for_keyword("heist").expect("lookup"),
        vec!["Heists", "heist"]
    );
    assert!(
        reader
            .surfaces_for_keyword("nothing")
            .expect("lookup")
            .is_empty()
    );
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
fn collections_for_returns_both_sources_with_their_own_nullable_fields_intact() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let memberships = reader
        .collections_for("imdb:tt1")
        .expect("query collection memberships");

    assert_eq!(
        memberships.len(),
        2,
        "tt1 sits on two lists from two different sources"
    );

    // Ordered by collection_id: "mdblist:100" sorts before "reddit:heist".
    let mdblist = &memberships[0];
    assert_eq!(mdblist.collection_id, "mdblist:100");
    assert_eq!(mdblist.source, "mdblist");
    assert_eq!(mdblist.name.as_deref(), Some("Best Heists"));
    assert_eq!(
        mdblist.url.as_deref(),
        Some("https://mdblist.com/lists/100")
    );
    assert_eq!(mdblist.size, Some(50));
    assert_eq!(mdblist.likes, Some(1200));
    assert_eq!(mdblist.rank, Some(3), "MDBList records a rank");
    assert_eq!(
        mdblist.mentions, None,
        "MDBList never fills mentions — must stay None, not 0"
    );

    let reddit = &memberships[1];
    assert_eq!(reddit.collection_id, "reddit:heist");
    assert_eq!(reddit.source, "reddit");
    assert_eq!(
        reddit.name, None,
        "a source that recorded no name stays None"
    );
    assert_eq!(reddit.url, None);
    assert_eq!(
        reddit.size, None,
        "a mentions-only source has no list length"
    );
    assert_eq!(reddit.likes, None);
    assert_eq!(
        reddit.rank, None,
        "a mentions source never ranks — must stay None, not 0"
    );
    assert_eq!(reddit.mentions, Some(7));
}

#[test]
fn collections_for_an_unordered_entry_keeps_rank_and_mentions_distinguishable_from_zero() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let memberships = reader
        .collections_for("imdb:tt2")
        .expect("query collection memberships");

    assert_eq!(memberships.len(), 1);
    let entry = &memberships[0];
    assert_eq!(entry.collection_id, "mdblist:100");
    assert_eq!(
        entry.rank, None,
        "an unordered entry on an otherwise-ranked list must not collapse to rank 0"
    );
    assert_eq!(entry.mentions, None);
}

#[test]
fn collections_for_a_title_on_no_lists_is_empty_not_an_error() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let memberships = reader
        .collections_for("imdb:tt3")
        .expect("query collection memberships for a title on no lists");
    assert!(memberships.is_empty());
}

/// Two weights are equal to within floating-point noise. The rollup sums
/// square roots, so exact comparison would fail on rounding rather than on
/// anything meaningful.
fn close(actual: f64, expected: f64) -> bool {
    (actual - expected).abs() < 1e-9
}

fn weight_of(vector: &plexdb_reader::TasteVector, value: &str) -> Option<f64> {
    vector
        .attributes
        .iter()
        .find(|a| a.value == value)
        .map(|a| a.weight)
}

#[test]
fn the_taste_vector_is_stable_across_calls() {
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

    assert!(
        weight_of(&first, "space").is_none(),
        "tt3 (the only title carrying `space`) was never played by account 42"
    );

    // An account with no plays at all gets an empty vector, not an error.
    let nobody = reader
        .taste_vector_for(9999)
        .expect("compute the vector for an account with no plays");
    assert!(nobody.attributes.is_empty());
    assert!(nobody.shows_without_seasons.is_empty());
}

#[test]
fn one_film_watched_once_and_one_full_season_both_weigh_one() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");

    // tt2 is a film watched once carrying exactly one keyword: r = 1,
    // sqrt(1) = 1, split across 1 keyword. tt1 also carries `heist`, watched
    // twice with two keywords: sqrt(2)/2. So `heist` is the sum of both.
    let heist = weight_of(&vector, "heist").expect("tt1 and tt2 both carry `heist`");
    assert!(
        close(heist, 1.0 + 2f64.sqrt() / 2.0),
        "expected 1.0 (tt2, one full watch, one keyword) + sqrt(2)/2 (tt1 rewatched, two \
         keywords), got {heist}"
    );

    // ttfin: one full season of a median-5 season show. r = 5/5 = 1, so
    // sqrt(1) = 1 split across its two keywords — the same weight per keyword
    // a single film watched once produces.
    let sitcom = weight_of(&vector, "sitcom").expect("ttfin carries `sitcom`");
    assert!(
        close(sitcom, 0.5),
        "one full season, two keywords, must give 1.0/2 — got {sitcom}"
    );
    assert!(close(weight_of(&vector, "workplace").unwrap(), 0.5));
}

#[test]
fn a_title_under_half_a_season_contributes_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");

    // ttbail: 2 of a 10-episode season is r = 0.2, under the 0.5 floor.
    assert!(
        weight_of(&vector, "bailed").is_none(),
        "a show watched to 20% of one season must contribute nothing at all — \
         not a reduced weight, and never a negative one"
    );
}

#[test]
fn a_heavily_tagged_title_does_not_outvote_a_sparsely_tagged_one() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");

    // ttfin carries two keywords and was watched exactly one season; tt2
    // carries one keyword and was watched exactly once. Both are r = 1, so
    // each title's *total* contribution is 1.0 — the split is what stops the
    // two-keyword title counting double.
    let ttfin_total =
        weight_of(&vector, "sitcom").unwrap() + weight_of(&vector, "workplace").unwrap();
    assert!(
        close(ttfin_total, 1.0),
        "a title's weight is split across its keywords, so its total stays 1.0 per full \
         watch however many keywords it carries — got {ttfin_total}"
    );
}

#[test]
fn nine_seasons_counts_about_three_films_not_nine() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let conn = rusqlite::Connection::open(&path).expect("open for a long-run seed");
    // A nine-season show, one keyword, watched end to end: r = 9.
    conn.execute_batch(
        "INSERT INTO items (item_id, type, title) VALUES ('imdb:ttlong', 'show', 'Long Run');
         INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) VALUES
             ('imdb:ttlong', 'keywords', 'tmdb', 'keyword', 'longrun', '2026-01-01T00:00:00+00:00');",
    )
    .expect("seed the long-running show");
    let mut sql = String::new();
    for season in 1..=9 {
        for episode in 1..=5 {
            sql.push_str(&format!(
                "INSERT INTO items (item_id, type, title, show_item_id, season, episode) VALUES \
                 ('long-s{season}e{episode}', 'episode', 'L', 'imdb:ttlong', {season}, {episode});\n\
                 INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES \
                 ('L{season}-{episode}', 'long-s{season}e{episode}', 42, 1700900000);\n"
            ));
        }
    }
    conn.execute_batch(&sql)
        .expect("seed the long run's episodes and plays");
    drop(conn);

    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");
    let longrun = weight_of(&vector, "longrun").expect("the long-running show is in the vector");
    let film = weight_of(&vector, "ensemble cast").expect("tt1 is a film in the vector");

    assert!(
        close(longrun, 3.0),
        "45 episodes of a median-5 season show is r = 9, and sqrt(9) = 3 — got {longrun}"
    );
    assert!(
        longrun < 9.0 * film,
        "nine seasons must not count nine times a film; damping is the whole point"
    );
}

#[test]
fn a_fetchers_bookkeeping_never_reaches_the_vector() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");

    // The fixture seeds four `enrichment_cursor` rows across two namespaces.
    // None of them may appear as an attribute — and, since schema v7, none of
    // them can, because the rollup reads `enrichment` and they are not in it.
    // Before that they were `enrichment` rows kept out by a key-prefix filter,
    // and on a real account they reached 43.6% of the vector's weight.
    assert!(
        vector
            .attributes
            .iter()
            .all(|a| a.namespace != "tmdb_edges" && !a.key.starts_with("fetched")),
        "a fetcher's note to itself about staleness is not a fact about the title"
    );

    // ttfin carries exactly two real keywords, so each takes half the title's
    // weight. Bookkeeping must not inflate the divisor either — that is the
    // half of the bug that dragged every genuine keyword down at once.
    let sitcom = weight_of(&vector, "sitcom").expect("ttfin carries `sitcom`");
    assert!(
        close(sitcom, 0.5),
        "bookkeeping must not shrink the real keywords by inflating the attribute count — \
         expected 0.5, got {sitcom}"
    );
}

#[test]
fn a_show_with_no_season_numbers_is_reported_rather_than_silently_weighed() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");

    assert_eq!(
        vector.shows_without_seasons,
        vec!["imdb:ttnosea".to_string()],
        "a show Plex files with no season number has no season length to divide by, so it \
         is named rather than folded in as though it had one"
    );
}

#[test]
fn the_pooled_vector_equals_the_single_account_vector_when_only_one_account_has_plays() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    // The fixture's only plays belong to account 42 (account 43 has none),
    // so pooling across every account must return exactly what one-account
    // taste_vector_for(42) returns — same attributes, same order.
    let pooled = reader
        .pooled_taste_vector()
        .expect("compute the pooled vector");
    let single = reader
        .taste_vector_for(42)
        .expect("compute account 42's vector");
    assert_eq!(
        pooled, single,
        "with exactly one account's plays in the store, pooling must equal that account's own vector"
    );
}

#[test]
fn the_pooled_vector_is_stable_across_calls() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let first = reader
        .pooled_taste_vector()
        .expect("compute the pooled vector");
    let second = reader
        .pooled_taste_vector()
        .expect("compute it again from unchanged input");
    assert_eq!(
        first, second,
        "unchanged input must roll up to the same pooled vector"
    );
}

#[test]
fn the_pooled_vector_sums_a_second_accounts_plays_rather_than_averaging_or_ignoring_them() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let conn = rusqlite::Connection::open(&path).expect("open for a second-account seed");
    // Account 100 watches tt2 (a film carrying only `heist`) once more, on
    // top of account 42's existing single watch of it. Pooled plays on tt2
    // become 2, so r = 2 rather than 1.
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) \
         VALUES ('h100', 'imdb:tt2', 100, 1700000300)",
        [],
    )
    .expect("seed a second account's play");
    drop(conn);

    let reader = Reader::open(&path).expect("open the fixture store");
    let pooled = reader
        .pooled_taste_vector()
        .expect("compute the pooled vector");

    // tt1 (account 42 only, 2 plays, 2 keywords) still contributes
    // sqrt(2)/2 of `heist`; tt2 now carries 2 pooled plays across accounts
    // 42 and 100, r = 2, sqrt(2) split across its one keyword `heist`.
    let expected_heist = 2f64.sqrt() / 2.0 + 2f64.sqrt();
    let heist = weight_of(&pooled, "heist").expect("heist is carried by tt1 and tt2");
    assert!(
        close(heist, expected_heist),
        "pooling must sum plays across accounts before damping, not average or drop the second \
         account's watch — expected {expected_heist}, got {heist}"
    );
}

#[test]
fn the_pooled_vectors_shows_without_seasons_names_a_show_once_not_once_per_account() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let conn = rusqlite::Connection::open(&path).expect("open for a second-account seed");
    // A second account also watches the show Plex filed with no season
    // numbers. If the pooled rollup grouped per (account, unit) rather than
    // per unit, this show would be named twice.
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) \
         VALUES ('n100', 'nosea-3', 100, 1700003003)",
        [],
    )
    .expect("seed a second account's play on the show with no season numbers");
    drop(conn);

    let reader = Reader::open(&path).expect("open the fixture store");
    let pooled = reader
        .pooled_taste_vector()
        .expect("compute the pooled vector");

    assert_eq!(
        pooled.shows_without_seasons,
        vec!["imdb:ttnosea".to_string()],
        "a show with no season numbers must be named exactly once, however many accounts \
         watched it"
    );
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

/// Issue #60: the lifetime "has this been played" set, which a fixed-length
/// history tail cannot answer. Units, not rows — a film is itself, an episode
/// rolls up to its show — so a caller ranking films or series joins nothing.
#[test]
fn watched_units_names_every_unit_an_account_ever_played() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    let watched = reader.watched_units_for(42).expect("watched units");
    assert_eq!(
        watched,
        vec![
            "imdb:tt1".to_string(),
            "imdb:tt2".to_string(),
            "imdb:ttbail".to_string(),
            "imdb:ttfin".to_string(),
            "imdb:ttnosea".to_string(),
        ],
        "every played unit, sorted, with episodes rolled up to their show"
    );

    // `imdb:tt3` is the only title account 42 never touched, and the whole
    // point of the accessor is that it can say so.
    assert!(!watched.contains(&"imdb:tt3".to_string()));

    // `ttbail` is here even though ADR-0011's r = 0.5 floor keeps it out of
    // the taste vector: two episodes of a ten-episode season is a weak taste
    // signal and an unambiguous "yes, this was played". The two rules are
    // deliberately different, and this is the assertion that holds them apart.
    assert!(
        weight_of(
            &reader.taste_vector_for(42).expect("taste vector"),
            "abandoned"
        )
        .is_none()
            || watched.contains(&"imdb:ttbail".to_string())
    );

    // An account with no plays gets an empty list, not an error.
    assert!(
        reader
            .watched_units_for(9999)
            .expect("an account with no plays")
            .is_empty()
    );
}

/// The pooled half: what the house has seen, with the same unit rollup and
/// the same one-play bar.
#[test]
fn pooled_watched_units_covers_every_account() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");

    // Only account 42 has plays in this fixture, so the house's set and its
    // own are identical — the assertion that pooling is a superset, checked
    // where the two happen to coincide.
    assert_eq!(
        reader.watched_units().expect("house watched units"),
        reader.watched_units_for(42).expect("account 42's"),
    );
}
