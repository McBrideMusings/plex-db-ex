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
         INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) VALUES
             ('imdb:ttlong', 'tmdb_keywords', 'keyword', 'longrun', '2026-01-01T00:00:00+00:00');",
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
fn a_fetchers_bookkeeping_sentinel_never_reaches_the_vector() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plexdb.db");
    common::build_fixture(&path);
    let reader = Reader::open(&path).expect("open the fixture store");
    let vector = reader.taste_vector_for(42).expect("taste vector");

    assert!(
        vector.attributes.iter().all(|a| !a.key.starts_with('_')),
        "`_fetched` and friends are a fetcher's note to itself about staleness, not a fact \
         about the title — on a real account they were 43.6% of the vector's weight"
    );

    // ttfin carries two real keywords plus one sentinel. The sentinel must not
    // count toward the split either: each keyword gets 1.0/2, not 1.0/3.
    let sitcom = weight_of(&vector, "sitcom").expect("ttfin carries `sitcom`");
    assert!(
        close(sitcom, 0.5),
        "a sentinel must not shrink the real keywords by inflating the attribute count — \
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
