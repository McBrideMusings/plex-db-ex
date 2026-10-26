//! The three TMDB sweeps against a store built by the real `plexdb migrate` and a
//! source that answers from memory. The caching discipline is the substance of
//! them: `Source` panics the instant a title is asked for twice.

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::Path;

use plexdb::enrich_tmdb::{self, SweepError};
use plexdb::enrich_tmdb_language;
use plexdb::migrate::migrate;
use plexdb::tmdb::{TmdbError, TmdbSource};
use plexdb::tmdb_edges::{self, RECOMMENDATIONS_EDGE_TYPE, SIMILAR_EDGE_TYPE};
use rusqlite::Connection;

fn store(dir: &Path) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open the fixture store");
    conn.execute_batch("PRAGMA foreign_keys = ON")
        .expect("pragma");
    conn
}

fn seed(conn: &Connection, item_id: &str, item_type: &str, tmdb_id: Option<&str>) {
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        (item_id, item_type, item_id),
    )
    .expect("insert the title");
    if let Some(id) = tmdb_id {
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) \
             VALUES (?, 'tmdb', ?, ?, '2026-01-01T00:00:00+00:00')",
            (item_id, id, item_type),
        )
        .expect("insert the id");
    }
}

/// Answers by `(tmdb_id, media_type)`; an id in `fail` errors; every ask is logged
/// and a repeat ask of one method for one title panics.
#[derive(Default)]
struct Source {
    keywords: HashMap<(String, String), Vec<String>>,
    language: HashMap<(String, String), Option<String>>,
    recommendations: HashMap<(String, String), Vec<String>>,
    similar: HashMap<(String, String), Vec<String>>,
    fail: Vec<String>,
    asked: RefCell<Vec<String>>,
}

impl Source {
    fn ask(&self, method: &str, id: &str, media: &str) -> Result<(), TmdbError> {
        let call = format!("{method} {id} {media}");
        assert!(!self.asked.borrow().contains(&call), "asked twice: {call}");
        self.asked.borrow_mut().push(call);
        if self.fail.iter().any(|f| f == id) {
            return Err(TmdbError(format!("TMDB returned 503 for {id}")));
        }
        Ok(())
    }
    fn key(id: &str, media: &str) -> (String, String) {
        (id.to_string(), media.to_string())
    }
}

impl TmdbSource for Source {
    fn keywords(&self, id: &str, media: &str) -> Result<Vec<String>, TmdbError> {
        self.ask("keywords", id, media)?;
        Ok(self
            .keywords
            .get(&Self::key(id, media))
            .cloned()
            .unwrap_or_default())
    }
    fn original_language(&self, id: &str, media: &str) -> Result<Option<String>, TmdbError> {
        self.ask("language", id, media)?;
        Ok(self.language.get(&Self::key(id, media)).cloned().flatten())
    }
    fn recommendations(&self, id: &str, media: &str) -> Result<Vec<String>, TmdbError> {
        self.ask("recommendations", id, media)?;
        Ok(self
            .recommendations
            .get(&Self::key(id, media))
            .cloned()
            .unwrap_or_default())
    }
    fn similar(&self, id: &str, media: &str) -> Result<Vec<String>, TmdbError> {
        self.ask("similar", id, media)?;
        Ok(self
            .similar
            .get(&Self::key(id, media))
            .cloned()
            .unwrap_or_default())
    }
}

fn strings(values: &[&str]) -> Vec<String> {
    values.iter().map(ToString::to_string).collect()
}

fn column(conn: &Connection, sql: &str) -> Vec<String> {
    conn.prepare(sql)
        .expect("prepare")
        .query_map([], |row| row.get::<_, String>(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

#[test]
fn keywords_are_normalized_kept_as_spelled_and_never_asked_again_inside_the_window() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "imdb:tt1", "movie", Some("155"));
    seed(&conn, "imdb:tt2", "show", Some("1396"));
    seed(&conn, "imdb:tt3", "movie", None);
    seed(&conn, "imdb:tt4", "movie", Some("9"));
    seed(&conn, "plex:ep1", "episode", Some("77"));
    let mut source = Source::default();
    source.keywords.insert(
        Source::key("155", "movie"),
        strings(&["Heists", "bank-heist", "heist", "&"]),
    );
    source
        .keywords
        .insert(Source::key("1396", "tv"), strings(&["Time Travel"]));

    let stats = enrich_tmdb::enrich_tmdb_keywords(&mut conn, &source, 45).expect("sweep");

    assert_eq!(
        (
            stats.titles_seen,
            stats.titles_fetched,
            stats.titles_cached,
            stats.titles_skipped_no_tmdb_id,
            stats.titles_failed,
            stats.keywords_written
        ),
        (4, 3, 0, 1, 0, 3)
    );
    assert_eq!(
        column(
            &conn,
            "SELECT item_id || ' ' || value FROM enrichment \
             WHERE namespace = 'keywords' AND source = 'tmdb' ORDER BY 1"
        ),
        [
            "imdb:tt1 bank heist",
            "imdb:tt1 heist",
            "imdb:tt2 time travel"
        ]
    );
    // Every raw spelling is kept, the ones that normalize to nothing too.
    assert_eq!(
        column(
            &conn,
            "SELECT surface FROM keyword_surfaces WHERE item_id = 'imdb:tt1' ORDER BY 1"
        ),
        ["&", "Heists", "bank-heist", "heist"]
    );
    assert_eq!(
        column(
            &conn,
            "SELECT surface || '>' || keyword FROM keyword_forms ORDER BY 1"
        ),
        [
            "Heists>heist",
            "Time Travel>time travel",
            "bank-heist>bank heist",
            "heist>heist"
        ]
    );
    // A title with no keywords is cached as surely as one with twenty.
    assert_eq!(
        column(
            &conn,
            "SELECT item_id FROM enrichment_cursor WHERE namespace = 'keywords' ORDER BY 1"
        ),
        ["imdb:tt1", "imdb:tt2", "imdb:tt4"]
    );

    let again = enrich_tmdb::enrich_tmdb_keywords(&mut conn, &Source::default(), 45)
        .expect("a repeat run asks nothing: the fake panics on any ask it has no answer for");
    assert_eq!((again.titles_fetched, again.titles_cached), (0, 3));
}

#[test]
fn a_stale_title_is_fetched_again_and_its_keyword_set_replaced_whole() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "imdb:tt1", "movie", Some("155"));
    let mut source = Source::default();
    source
        .keywords
        .insert(Source::key("155", "movie"), strings(&["heist", "robbery"]));
    enrich_tmdb::enrich_tmdb_keywords(&mut conn, &source, 45).expect("first sweep");
    conn.execute(
        "UPDATE enrichment_cursor SET fetched_at = '2026-01-01T00:00:00+00:00'",
        [],
    )
    .expect("age the cursor");
    // Another source's keyword on the same title must survive.
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
         VALUES ('imdb:tt1', 'keywords', 'anilist', 'keyword', 'mecha', '2026-01-01T00:00:00+00:00')",
        [],
    )
    .expect("another source");
    let mut second = Source::default();
    second
        .keywords
        .insert(Source::key("155", "movie"), strings(&["robbery", "vault"]));

    let stats = enrich_tmdb::enrich_tmdb_keywords(&mut conn, &second, 45).expect("second sweep");

    assert_eq!((stats.titles_fetched, stats.titles_cached), (1, 0));
    assert_eq!(
        column(
            &conn,
            "SELECT source || ' ' || value FROM enrichment WHERE namespace = 'keywords' ORDER BY 1"
        ),
        ["anilist mecha", "tmdb robbery", "tmdb vault"]
    );
    let cursor = column(&conn, "SELECT fetched_at FROM enrichment_cursor");
    assert!(
        cursor[0].as_str() > "2026-10-01",
        "the cursor moved: {cursor:?}"
    );
}

#[test]
fn one_failed_title_is_counted_and_retried_and_three_in_a_row_abort_the_sweep() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    for (n, id) in ["a", "b", "c", "d"].iter().enumerate() {
        seed(
            &conn,
            &format!("imdb:{id}"),
            "movie",
            Some(&format!("{}", 10 + n)),
        );
    }
    let mut source = Source {
        fail: strings(&["11"]),
        ..Source::default()
    };
    source
        .keywords
        .insert(Source::key("12", "movie"), strings(&["heist"]));

    let stats = enrich_tmdb::enrich_tmdb_keywords(&mut conn, &source, 45).expect("sweep");

    assert_eq!((stats.titles_fetched, stats.titles_failed), (3, 1));
    // The failed title wrote nothing, so a re-run asks for it again.
    assert_eq!(
        column(&conn, "SELECT item_id FROM enrichment_cursor ORDER BY 1"),
        ["imdb:a", "imdb:c", "imdb:d"]
    );

    let dead = Source {
        fail: strings(&["10", "11", "12", "13"]),
        ..Source::default()
    };
    let other = tempfile::tempdir().expect("tempdir");
    let mut fresh = store(other.path());
    for (n, id) in ["a", "b", "c", "d"].iter().enumerate() {
        seed(
            &fresh,
            &format!("imdb:{id}"),
            "movie",
            Some(&format!("{}", 10 + n)),
        );
    }
    let err = enrich_tmdb::enrich_tmdb_keywords(&mut fresh, &dead, 45).expect_err("aborts");
    let SweepError::Tmdb(err) = err else {
        panic!("{err}")
    };
    assert_eq!(
        err.0,
        "aborting after 3 consecutive failures: 3 title(s) processed, 3 failed; \
         tripping error: TMDB returned 503 for 12"
    );
}

#[test]
fn rewipe_removes_the_tmdb_keywords_and_cursors_and_nothing_a_neighbour_wrote() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "imdb:tt1", "movie", Some("155"));
    let mut source = Source::default();
    source
        .keywords
        .insert(Source::key("155", "movie"), strings(&["heist"]));
    enrich_tmdb::enrich_tmdb_keywords(&mut conn, &source, 45).expect("sweep");
    conn.execute_batch(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
         VALUES ('imdb:tt1', 'keywords', 'anilist', 'keyword', 'mecha', '2026-01-01T00:00:00+00:00');
         INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
         VALUES ('imdb:tt1', 'keywords', 'anilist', 'fetched', '2026-01-01T00:00:00+00:00');",
    )
    .expect("a neighbour");

    let removed = enrich_tmdb::wipe_namespace(&mut conn).expect("wipe");

    assert_eq!(removed, 2); // the keyword row and the cursor
    assert_eq!(
        column(&conn, "SELECT surface FROM keyword_surfaces"),
        Vec::<String>::new()
    );
    assert_eq!(column(&conn, "SELECT source FROM enrichment"), ["anilist"]);
    assert_eq!(
        column(&conn, "SELECT source FROM enrichment_cursor"),
        ["anilist"]
    );
}

#[test]
fn the_original_language_is_one_row_per_title_and_a_title_without_one_is_still_cached() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "imdb:tt1", "movie", Some("155"));
    seed(&conn, "imdb:tt2", "show", Some("1396"));
    seed(&conn, "imdb:tt3", "movie", Some("9"));
    let mut source = Source::default();
    source
        .language
        .insert(Source::key("155", "movie"), Some("en".into()));
    source
        .language
        .insert(Source::key("1396", "tv"), Some("ja".into()));
    source
        .language
        .insert(Source::key("9", "movie"), Some(String::new()));

    let stats = enrich_tmdb_language::enrich_tmdb_language(&mut conn, &source, 45).expect("sweep");

    assert_eq!(
        (
            stats.titles_fetched,
            stats.languages_written,
            stats.titles_without_language
        ),
        (3, 2, 1)
    );
    assert_eq!(
        column(
            &conn,
            "SELECT item_id || ' ' || value FROM enrichment \
             WHERE namespace = 'tmdb' AND key = 'original_language' ORDER BY 1"
        ),
        ["imdb:tt1 en", "imdb:tt2 ja"]
    );
    let again = enrich_tmdb_language::enrich_tmdb_language(&mut conn, &Source::default(), 45)
        .expect("repeat");
    assert_eq!((again.titles_fetched, again.titles_cached), (0, 3));

    let removed = enrich_tmdb_language::wipe(&mut conn).expect("wipe");
    assert_eq!(removed, 5); // two languages and three cursors
}

#[test]
fn edges_keep_tmdbs_rank_drop_what_the_library_lacks_and_replace_the_whole_set() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "imdb:tt1", "movie", Some("155"));
    seed(&conn, "imdb:tt2", "movie", Some("2"));
    seed(&conn, "imdb:tt3", "show", Some("3"));
    seed(&conn, "imdb:tt4", "movie", Some("4"));
    // A second TMDB id that names an item already named: only the better rank stays.
    conn.execute(
        "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) \
         VALUES ('imdb:tt2', 'tmdb', '22', 'movie', '2026-01-01T00:00:00+00:00')",
        [],
    )
    .expect("second id");
    let mut source = Source::default();
    source.recommendations.insert(
        Source::key("155", "movie"),
        strings(&["999", "3", "155", "2", "22", "4"]),
    );
    source
        .similar
        .insert(Source::key("155", "movie"), strings(&["4"]));

    let by_type = tmdb_edges::refresh_tmdb_edges(&mut conn, &source, 45).expect("sweep");

    let (recommended, similar) = (&by_type[0].1, &by_type[1].1);
    assert_eq!(by_type[0].0, RECOMMENDATIONS_EDGE_TYPE);
    assert_eq!(by_type[1].0, SIMILAR_EDGE_TYPE);
    assert_eq!(
        (recommended.titles_fetched, recommended.edges_written),
        (4, 3)
    );
    assert_eq!(recommended.edges_skipped_not_in_library, 2); // 999, and itself
    assert_eq!((similar.titles_fetched, similar.edges_written), (4, 1));
    assert_eq!(
        column(
            &conn,
            "SELECT edge_type || ' ' || from_id || '>' || to_id || ' #' || rank FROM edges ORDER BY 1"
        ),
        [
            "tmdb_recommendations imdb:tt1>imdb:tt2 #4",
            "tmdb_recommendations imdb:tt1>imdb:tt3 #2",
            "tmdb_recommendations imdb:tt1>imdb:tt4 #6",
            "tmdb_similar imdb:tt1>imdb:tt4 #1",
        ]
    );
    // Four titles per edge type have a cursor, whatever they returned.
    assert_eq!(
        column(
            &conn,
            "SELECT key || ' ' || count(*) FROM enrichment_cursor GROUP BY key ORDER BY 1"
        ),
        ["fetched_recommendations 4", "fetched_similar 4"]
    );

    // A re-pull replaces the set: age the cursors and let TMDB drop an edge.
    conn.execute(
        "UPDATE enrichment_cursor SET fetched_at = '2026-01-01T00:00:00+00:00'",
        [],
    )
    .expect("age");
    let mut later = Source::default();
    later
        .recommendations
        .insert(Source::key("155", "movie"), strings(&["4"]));
    tmdb_edges::refresh_tmdb_edges(&mut conn, &later, 45).expect("second sweep");
    assert_eq!(
        column(
            &conn,
            "SELECT from_id || '>' || to_id || ' #' || rank FROM edges \
             WHERE edge_type = 'tmdb_recommendations'"
        ),
        ["imdb:tt1>imdb:tt4 #1"]
    );

    let (edges, cursors) = tmdb_edges::wipe_edge_type(&mut conn, SIMILAR_EDGE_TYPE).expect("wipe");
    assert_eq!((edges, cursors), (0, 4));
}
