//! The four optional-source sweeps (AniList, Wikidata, MDBList ratings, Letterboxd themes)
//! against a store built by the real `plexdb migrate` and a source that answers from memory.
//! The caching discipline is the substance of them: a fetched title is never asked of its
//! source again inside the window, and a failed request leaves no cursor that vouches for it.

use std::collections::BTreeMap;
use std::path::Path;

use plexdb::anilist::{AniListError, AniListSource, Tag};
use plexdb::enrich_anilist::{self, AniListStats};
use plexdb::enrich_letterboxd::{self, LetterboxdStats};
use plexdb::enrich_mdblist_ratings::{self, RatingsStats};
use plexdb::enrich_wikidata::{self, BATCH_SIZE};
use plexdb::letterboxd::{LbError, LbThemesSource, Lookup, Outcome};
use plexdb::mdblist::{Kind, MdbError, MdbRating, MdbRatingsSource, MdbTitleRatings};
use plexdb::migrate::migrate;
use plexdb::sweep::SweepError;
use plexdb::wikidata::{Statement, WikidataError, WikidataSource};
use rusqlite::Connection;
use serde_json::{Value, json};

fn store(dir: &Path) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open the fixture store");
    conn.execute_batch("PRAGMA foreign_keys = ON")
        .expect("pragma");
    conn
}

fn seed(conn: &Connection, item_id: &str, item_type: &str, ids: &[(&str, &str)]) {
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        (item_id, item_type, item_id),
    )
    .expect("insert the title");
    for (ns, value) in ids {
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) \
             VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (item_id, ns, value, item_type),
        )
        .expect("insert the id");
    }
}

fn column(conn: &Connection, sql: &str) -> Vec<String> {
    conn.prepare(sql)
        .expect("prepare")
        .query_map([], |row| row.get::<_, String>(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn count(conn: &Connection, sql: &str) -> i64 {
    conn.query_row(sql, [], |row| row.get(0)).expect("count")
}

const FRESH: i64 = 45;

// --- AniList ---------------------------------------------------------------------------

#[derive(Default)]
struct Anilist {
    tags: BTreeMap<i64, Vec<Tag>>,
    failing: bool,
    asked: Vec<Vec<i64>>,
}

impl AniListSource for Anilist {
    fn mapping(&mut self) -> Result<Vec<Value>, AniListError> {
        Ok(vec![
            json!({"anilist_id": 10, "themoviedb_id": {"tv": 1396}}),
            json!({"anilist_id": 11, "themoviedb_id": {"tv": [1396]}}),
            json!({"anilist_id": 20, "imdb_id": "tt2"}),
        ])
    }
    fn tags(&mut self, ids: &[i64]) -> Result<BTreeMap<i64, Vec<Tag>>, AniListError> {
        self.asked.push(ids.to_vec());
        if self.failing {
            return Err(AniListError("AniList returned 500".into()));
        }
        Ok(ids
            .iter()
            .filter_map(|id| self.tags.get(id).map(|tags| (*id, tags.clone())))
            .collect())
    }
}

fn tag(name: &str, rank: i64, category: Option<&str>, spoiler: bool) -> Tag {
    Tag {
        name: name.into(),
        rank: Some(rank),
        category: category.map(str::to_string),
        spoiler,
    }
}

#[test]
fn anilist_merges_a_shows_seasons_flags_spoilers_states_roles_and_is_asked_once() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "tmdb:1", "show", &[("tmdb", "1396")]);
    seed(&conn, "imdb:tt2", "movie", &[("imdb", "tt2")]);
    seed(&conn, "plain", "movie", &[("tmdb", "9")]);
    let mut source = Anilist::default();
    source.tags.insert(
        10,
        vec![
            tag("Time Travel", 60, Some("Theme-Fantasy"), false),
            tag("Heists", 90, Some("Setting-Time"), true),
        ],
    );
    source.tags.insert(
        11,
        vec![
            tag("time-travel", 80, None, false),
            tag("Heists", 20, None, false),
        ],
    );

    let stats = enrich_anilist::enrich_anilist(&mut conn, &mut source, FRESH).expect("sweep");

    assert_eq!(
        (stats.titles_seen, stats.titles_anime, stats.titles_fetched),
        (3, 2, 2)
    );
    // A title the mapping does not place costs no request and gets no cursor.
    assert_eq!(source.asked, [vec![10, 11, 20]]);
    assert_eq!(
        column(
            &conn,
            "SELECT key || ':' || value || ':' || rank FROM enrichment \
             WHERE source = 'anilist' ORDER BY value"
        ),
        ["spoiler_keyword:heist:90", "keyword:time travel:80"]
    );
    assert_eq!(
        column(
            &conn,
            "SELECT keyword || ':' || role FROM keyword_roles WHERE source = 'anilist' \
             ORDER BY keyword"
        ),
        ["heist:era", "time travel:theme"]
    );
    assert_eq!(
        (
            stats.keywords_written,
            stats.spoiler_keywords_written,
            stats.roles_stated
        ),
        (1, 1, 2)
    );
    assert_eq!(
        count(
            &conn,
            "SELECT COUNT(*) FROM enrichment_cursor WHERE source = 'anilist'"
        ),
        2
    );

    let again = enrich_anilist::enrich_anilist(&mut conn, &mut source, FRESH).expect("sweep");
    assert_eq!(again.titles_cached, 2);
    assert_eq!(source.asked.len(), 1, "a fresh title is never asked again");
}

#[test]
fn an_anilist_batch_that_fails_writes_nothing_and_its_titles_are_retried_next_run() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "tmdb:1", "show", &[("tmdb", "1396")]);
    let mut source = Anilist {
        failing: true,
        ..Anilist::default()
    };
    let stats: AniListStats =
        enrich_anilist::enrich_anilist(&mut conn, &mut source, FRESH).expect("one failed batch");
    assert_eq!((stats.titles_failed, stats.titles_fetched), (1, 0));
    assert_eq!(count(&conn, "SELECT COUNT(*) FROM enrichment_cursor"), 0);
}

// --- Wikidata --------------------------------------------------------------------------

#[derive(Default)]
struct Wikidata {
    rows: Vec<Statement>,
    fail: bool,
    asked: Vec<Vec<String>>,
}

impl WikidataSource for Wikidata {
    fn statements(&mut self, ids: &[String]) -> Result<Vec<Statement>, WikidataError> {
        self.asked.push(ids.to_vec());
        if self.fail {
            return Err(WikidataError("Wikidata returned 500".into()));
        }
        Ok(self
            .rows
            .iter()
            .filter(|(imdb, _, _)| ids.contains(imdb))
            .cloned()
            .collect())
    }
}

fn statement(imdb: &str, prop: &str, label: &str) -> Statement {
    (imdb.into(), prop.into(), label.into())
}

#[test]
fn wikidata_stores_keywords_roles_and_verbatim_awards_and_prunes_roles_nobody_states() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "imdb:tt1", "movie", &[("imdb", "tt1")]);
    seed(&conn, "imdb:tt2", "movie", &[("imdb", "tt2")]);
    seed(&conn, "bad", "movie", &[("imdb", "nm1")]);
    seed(&conn, "none", "show", &[]);
    let mut source = Wikidata {
        rows: vec![
            statement("tt1", "P840", "Paris"),
            statement("tt1", "P136", "Heist Films"),
            statement("tt1", "P166", "Academy Award for Best Sound"),
            statement("tt1", "P166", "Academy Award for Best Sound"),
            statement("tt1", "P921", "   "),
            statement("tt2", "P2408", "1920s"),
        ],
        ..Wikidata::default()
    };

    let stats =
        enrich_wikidata::enrich_wikidata(&mut conn, &mut source, FRESH, BATCH_SIZE).expect("sweep");

    assert_eq!(
        (
            stats.titles_seen,
            stats.titles_fetched,
            stats.titles_matched,
            stats.titles_skipped_no_imdb_id
        ),
        (4, 2, 2, 2)
    );
    assert_eq!(source.asked, [vec!["tt1".to_string(), "tt2".to_string()]]);
    assert_eq!(
        column(
            &conn,
            "SELECT namespace || ':' || value FROM enrichment WHERE source = 'wikidata' \
             ORDER BY namespace, value"
        ),
        [
            "awards:Academy Award for Best Sound",
            "keywords:1920s",
            "keywords:heist film",
            "keywords:paris",
        ]
    );
    assert_eq!(
        column(
            &conn,
            "SELECT keyword || ':' || role FROM keyword_roles WHERE source = 'wikidata' \
             ORDER BY keyword"
        ),
        ["1920s:era", "paris:region"]
    );
    assert_eq!(stats.awards_written, 1);

    // Paris stops being a narrative location; the role no title states is deleted.
    conn.execute(
        "UPDATE enrichment_cursor SET fetched_at = '2020-01-01T00:00:00+00:00'",
        [],
    )
    .expect("age the cursors");
    source.rows.retain(|(_, prop, _)| prop != "P840");
    let again =
        enrich_wikidata::enrich_wikidata(&mut conn, &mut source, FRESH, BATCH_SIZE).expect("sweep");
    assert_eq!(again.roles_pruned, 1);
    assert_eq!(
        column(
            &conn,
            "SELECT keyword FROM keyword_roles WHERE source = 'wikidata'"
        ),
        ["1920s"]
    );
}

#[test]
fn wikidata_aborts_after_three_failed_queries_and_still_prunes() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    for n in 0..5 {
        seed(
            &conn,
            &format!("m{n}"),
            "movie",
            &[("imdb", &format!("tt{n}"))],
        );
    }
    let mut source = Wikidata {
        fail: true,
        ..Wikidata::default()
    };
    let outcome = enrich_wikidata::enrich_wikidata(&mut conn, &mut source, FRESH, 1);
    let Err(SweepError::Source(message)) = outcome else {
        panic!("three failed queries in a row abort the sweep");
    };
    assert!(
        message.starts_with("aborting after 3 consecutive failed queries"),
        "{message}"
    );
    assert_eq!(source.asked.len(), 3);
    assert_eq!(count(&conn, "SELECT COUNT(*) FROM enrichment_cursor"), 0);
}

// --- MDBList ratings -------------------------------------------------------------------

#[derive(Default)]
struct Ratings {
    answers: Vec<MdbTitleRatings>,
    fail_first: usize,
    quota_at: Option<usize>,
    asked: Vec<(String, Vec<String>)>,
}

impl MdbRatingsSource for Ratings {
    fn ratings(
        &mut self,
        media_type: &str,
        ids: &[String],
    ) -> Result<Vec<MdbTitleRatings>, MdbError> {
        self.asked.push((media_type.into(), ids.to_vec()));
        if self.quota_at == Some(self.asked.len()) {
            return Err(MdbError {
                kind: Kind::Quota,
                status: Some(429),
                message: "MDBList returned 429".into(),
            });
        }
        if self.asked.len() <= self.fail_first {
            return Err(MdbError::new("MDBList returned 500", Some(500)));
        }
        Ok(self
            .answers
            .iter()
            .filter(|t| ids.contains(&t.imdb_id))
            .cloned()
            .collect())
    }
}

fn rating(site: &str, value: Option<Value>, votes: Option<i64>) -> MdbRating {
    MdbRating {
        site: site.into(),
        value,
        votes,
    }
}

#[test]
fn ratings_keep_each_sites_own_number_skip_null_values_and_cursor_every_asked_title() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "a", "movie", &[("imdb", "tt1")]);
    seed(&conn, "b", "movie", &[("imdb", "tt2")]);
    seed(&conn, "c", "show", &[("imdb", "tt3")]);
    seed(&conn, "d", "movie", &[]);
    let mut source = Ratings {
        answers: vec![MdbTitleRatings {
            imdb_id: "tt1".into(),
            ratings: vec![
                rating("imdb", Some(json!(7.9)), Some(17303)),
                rating("rogerebert", Some(json!(4.0)), None),
                rating("metacritic", None, Some(1)),
                rating("tmdb", Some(json!(79)), Some(5)),
            ],
        }],
        ..Ratings::default()
    };

    let stats: RatingsStats =
        enrich_mdblist_ratings::enrich_mdblist_ratings(&mut conn, &mut source, FRESH, 100)
            .expect("sweep");

    assert_eq!(
        source.asked,
        [
            (
                "movie".to_string(),
                vec!["tt1".to_string(), "tt2".to_string()]
            ),
            ("show".to_string(), vec!["tt3".to_string()]),
        ]
    );
    assert_eq!(
        column(
            &conn,
            "SELECT key || '=' || value FROM enrichment WHERE namespace = 'ratings' \
             AND item_id = 'a' ORDER BY key"
        ),
        [
            "imdb=7.9",
            "imdb_votes=17303",
            "rogerebert=4.0",
            "tmdb=79",
            "tmdb_votes=5"
        ]
    );
    assert_eq!(
        (
            stats.titles_fetched,
            stats.titles_rated,
            stats.titles_not_found,
            stats.titles_skipped_no_imdb_id,
            stats.ratings_written,
            stats.votes_written
        ),
        (3, 1, 2, 1, 3, 2)
    );
    assert_eq!(
        count(
            &conn,
            "SELECT COUNT(*) FROM enrichment_cursor WHERE key = 'fetched'"
        ),
        3
    );

    let again = enrich_mdblist_ratings::enrich_mdblist_ratings(&mut conn, &mut source, FRESH, 100)
        .expect("sweep");
    assert_eq!((again.titles_cached, again.requests_sent), (3, 0));
}

#[test]
fn a_failed_request_only_marks_attempted_and_a_spent_quota_ends_the_run_marking_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "a", "movie", &[("imdb", "tt1")]);
    seed(&conn, "c", "show", &[("imdb", "tt3")]);
    let mut source = Ratings {
        fail_first: 1,
        ..Ratings::default()
    };
    let stats = enrich_mdblist_ratings::enrich_mdblist_ratings(&mut conn, &mut source, FRESH, 100)
        .expect("sweep");
    assert_eq!((stats.requests_failed, stats.titles_failed), (1, 1));
    assert_eq!(
        column(
            &conn,
            "SELECT item_id || ':' || key FROM enrichment_cursor ORDER BY item_id"
        ),
        ["a:attempted", "c:fetched"]
    );

    // The failed title now waits behind the one never attempted; the quota then runs out.
    conn.execute("DELETE FROM enrichment_cursor", [])
        .expect("clear");
    let mut spent = Ratings {
        quota_at: Some(1),
        ..Ratings::default()
    };
    let stats = enrich_mdblist_ratings::enrich_mdblist_ratings(&mut conn, &mut spent, FRESH, 100)
        .expect("sweep");
    assert!(stats.quota_spent.is_some());
    assert_eq!((stats.requests_sent, stats.titles_capped), (1, 2));
    assert_eq!(count(&conn, "SELECT COUNT(*) FROM enrichment_cursor"), 0);
}

#[test]
fn the_request_cap_leaves_the_rest_for_the_next_run() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "a", "movie", &[("imdb", "tt1")]);
    seed(&conn, "c", "show", &[("imdb", "tt3")]);
    let mut source = Ratings::default();
    let stats = enrich_mdblist_ratings::enrich_mdblist_ratings(&mut conn, &mut source, FRESH, 1)
        .expect("sweep");
    assert_eq!(
        (
            stats.requests_sent,
            stats.titles_capped,
            stats.titles_fetched
        ),
        (1, 1, 1)
    );
}

// --- Letterboxd ------------------------------------------------------------------------

#[derive(Default)]
struct Themes {
    pages: BTreeMap<String, Result<Lookup, String>>,
    asked: Vec<String>,
}

impl LbThemesSource for Themes {
    fn lookup(&mut self, tmdb_id: &str) -> Result<Lookup, LbError> {
        self.asked.push(tmdb_id.into());
        match self.pages.get(tmdb_id) {
            Some(Ok(found)) => Ok(found.clone()),
            Some(Err(message)) => Err(LbError(message.clone())),
            None => Ok(Lookup {
                outcome: Outcome::NotListed,
                themes: Vec::new(),
            }),
        }
    }
}

fn listed(themes: &[&str]) -> Result<Lookup, String> {
    Ok(Lookup {
        outcome: Outcome::Listed,
        themes: themes.iter().map(ToString::to_string).collect(),
    })
}

#[test]
fn letterboxd_themes_become_keywords_and_a_page_without_the_marker_is_asked_again() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    seed(&conn, "a", "movie", &[("tmdb", "1")]);
    seed(&conn, "b", "movie", &[("tmdb", "2")]);
    seed(&conn, "c", "movie", &[("tmdb", "3")]);
    seed(&conn, "d", "movie", &[("tmdb", "x4")]);
    seed(&conn, "e", "show", &[("tmdb", "5")]);
    let mut source = Themes::default();
    source
        .pages
        .insert("1".into(), listed(&["Heists and robbery", "Time travel"]));
    source.pages.insert(
        "2".into(),
        Ok(Lookup {
            outcome: Outcome::Unparsed,
            themes: Vec::new(),
        }),
    );

    let stats: LetterboxdStats =
        enrich_letterboxd::enrich_letterboxd(&mut conn, &mut source, FRESH, 1000).expect("run");

    assert_eq!(source.asked, ["1", "2", "3"]);
    assert_eq!(
        column(
            &conn,
            "SELECT value FROM enrichment WHERE source = 'letterboxd' ORDER BY value"
        ),
        ["heist and robbery", "time travel"]
    );
    assert_eq!(
        column(
            &conn,
            "SELECT item_id || ':' || key FROM enrichment_cursor ORDER BY item_id"
        ),
        ["a:fetched", "b:attempted", "c:fetched"]
    );
    assert_eq!(
        (
            stats.titles_fetched,
            stats.titles_matched,
            stats.titles_not_listed,
            stats.parse_failures,
            stats.titles_skipped_no_tmdb_id
        ),
        (2, 1, 1, 1, 1)
    );
    // One unparsed page of two loaded pages is over the 10% limit.
    assert_eq!(stats.pages_fetched(), 2);
    assert!(stats.parse_failed());

    // The unparsed title goes to the back of the next run's order; the cap cuts what remains.
    let mut next = Themes::default();
    let again =
        enrich_letterboxd::enrich_letterboxd(&mut conn, &mut next, FRESH, 1000).expect("run");
    assert_eq!(next.asked, ["2"]);
    assert_eq!(again.titles_cached, 2);
}

#[test]
fn letterboxd_runs_at_most_the_limit_and_aborts_after_three_failed_titles() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut conn = store(dir.path());
    for n in 0..5 {
        seed(
            &conn,
            &format!("m{n}"),
            "movie",
            &[("tmdb", &n.to_string())],
        );
    }
    let mut capped = Themes::default();
    let stats =
        enrich_letterboxd::enrich_letterboxd(&mut conn, &mut capped, FRESH, 2).expect("run");
    assert_eq!((capped.asked.len(), stats.titles_capped), (2, 3));

    conn.execute("DELETE FROM enrichment_cursor", [])
        .expect("clear");
    let mut failing = Themes::default();
    for n in 0..5 {
        failing
            .pages
            .insert(n.to_string(), Err("Letterboxd answered 403".into()));
    }
    let outcome = enrich_letterboxd::enrich_letterboxd(&mut conn, &mut failing, FRESH, 1000);
    let Err(SweepError::Source(message)) = outcome else {
        panic!("three failed titles in a row abort the run");
    };
    assert!(
        message.starts_with("aborting after 3 consecutive failed titles"),
        "{message}"
    );
    assert_eq!(
        count(
            &conn,
            "SELECT COUNT(*) FROM enrichment_cursor WHERE key = 'attempted'"
        ),
        3
    );
}

// --- The pages and answers the live services returned --------------------------------------

fn recorded(name: &str) -> String {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("run the tests through cargo");
    std::fs::read_to_string(format!("{manifest}/../../tests/fixtures/{name}")).expect("fixture")
}

#[test]
fn the_recorded_inception_page_gives_its_two_themes_and_five_mini_themes_in_page_order() {
    let found =
        plexdb::letterboxd::parse_film_page(&recorded("letterboxd/film_inception.html"), "27205");
    assert_eq!(found.outcome, Outcome::Listed);
    assert_eq!(found.themes.len(), 7);
    assert_eq!(found.themes[0], "High speed and special ops");
    assert_eq!(found.themes[1], "Humanity and the world around us");
    assert_eq!(
        found.themes[2],
        "Dreamlike, quirky, and surreal storytelling"
    );
    let other =
        plexdb::letterboxd::parse_film_page(&recorded("letterboxd/film_inception.html"), "1");
    assert_eq!(other.outcome, Outcome::Unparsed);
}

#[test]
fn the_recorded_anilist_page_and_wikidata_answer_parse_to_the_counts_they_hold() {
    let page: Value = serde_json::from_str(&recorded("anilist/page_batch.json")).expect("json");
    let media = plexdb::anilist::parse_media(&page);
    let counts: Vec<(i64, usize)> = media.iter().map(|(id, tags)| (*id, tags.len())).collect();
    assert_eq!(counts, [(300, 16), (1225, 9), (5114, 50)]);

    let answer: Value =
        serde_json::from_str(&recorded("wikidata/sparql_batch.json")).expect("json");
    let statements = plexdb::wikidata::parse_results(&answer);
    assert_eq!(statements.len(), 44);
    assert!(statements.iter().any(|(imdb, prop, label)| {
        (imdb.as_str(), prop.as_str(), label.as_str()) == ("tt1375666", "P840", "Los Angeles")
    }));
}
