//! The crowd-list harvests against a store built by the real `plexdb migrate`, with the
//! two sources replaced by in-memory fakes. The rules under test: rank is array
//! position, a list's facts are stored once, an entry outside the library is dropped
//! from membership but kept as an entry, a re-pull replaces rather than appends, and a
//! list is fetched only when it is due (ADR-0012, ADR-0027, ADR-0028).

use std::collections::HashMap;
use std::path::Path;

use plexdb::collections::{
    HarvestError, LETTERBOXD_SOURCE, MDBLIST_SOURCE, refresh_mdblist, wipe_source,
};
use plexdb::letterboxd::{FilmIds, LbError, LbSource, ListContents, ListFilm};
use plexdb::letterboxd_lists::refresh_letterboxd_lists;
use plexdb::list_search::{Options, refresh_list_search, wipe_searched};
use plexdb::mdblist::{Kind, MdbContents, MdbEntry, MdbError, MdbList, MdbSource};
use plexdb::migrate::migrate;
use plexdb::staleness::Now;
use rusqlite::Connection;

const DARK_KNIGHT: &str = "imdb:tt0468569";
const BATMAN_BEGINS: &str = "imdb:tt0372784";
const TOP: &str = "https://mdblist.com/lists/lina/top-watched";

fn store(dir: &Path) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open the fixture store");
    conn.pragma_update(None, "foreign_keys", "ON")
        .expect("pragma");
    conn
}

fn seed(conn: &Connection, item_id: &str, kind: &str, title: &str, ids: &[(&str, &str)]) {
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        (item_id, kind, title),
    )
    .expect("insert the item");
    for (ns, value) in ids {
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) \
             VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (item_id, ns, value, kind),
        )
        .expect("insert the id");
    }
}

fn movie(conn: &Connection, item_id: &str, title: &str, imdb: &str) {
    seed(conn, item_id, "movie", title, &[("imdb", imdb)]);
}

fn decide(conn: &Connection, url: &str, decision: &str, decided_at: &str, added: i64) {
    conn.execute(
        "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, ?)",
        (url, decision, decided_at, added),
    )
    .expect("insert the decision");
}

fn list(id: i64, name: &str, slug: &str, user: &str, size: i64, likes: i64) -> MdbList {
    MdbList {
        list_id: id,
        name: name.into(),
        slug: slug.into(),
        user_name: user.into(),
        size: Some(size),
        likes: Some(likes),
    }
}

fn entry(imdb: &str, kind: &str, title: &str) -> MdbEntry {
    MdbEntry {
        external_ids: vec![("imdb".into(), imdb.into())],
        media_type: kind.into(),
        title: Some(title.into()),
        year: Some(2008),
    }
}

#[derive(Default)]
struct FakeMdb {
    lists: Vec<MdbList>,
    entries: HashMap<i64, Vec<MdbEntry>>,
    named: HashMap<(String, String), MdbList>,
    failing: HashMap<i64, MdbError>,
    search: HashMap<String, Vec<MdbList>>,
    failing_searches: HashMap<String, MdbError>,
    entry_calls: Vec<i64>,
    find_calls: Vec<(String, String)>,
    search_calls: Vec<String>,
}

impl MdbSource for FakeMdb {
    fn top_lists(&mut self) -> Result<Vec<MdbList>, MdbError> {
        Ok(self.lists.clone())
    }

    fn find_list(&mut self, user: &str, slug: &str) -> Result<MdbList, MdbError> {
        self.find_calls.push((user.into(), slug.into()));
        self.named
            .get(&(user.to_string(), slug.to_string()))
            .cloned()
            .ok_or_else(|| {
                MdbError::new(
                    format!("MDBList returned 404 for /lists/{user}/{slug}"),
                    Some(404),
                )
            })
    }

    fn list_entries(&mut self, list_id: i64) -> Result<MdbContents, MdbError> {
        self.entry_calls.push(list_id);
        if let Some(err) = self.failing.get(&list_id) {
            return Err(err.clone());
        }
        Ok(MdbContents {
            status: 200,
            entries: self.entries.get(&list_id).cloned().unwrap_or_default(),
        })
    }

    fn search_lists(&mut self, query: &str) -> Result<Vec<MdbList>, MdbError> {
        self.search_calls.push(query.into());
        if let Some(err) = self.failing_searches.get(query) {
            return Err(err.clone());
        }
        Ok(self.search.get(query).cloned().unwrap_or_default())
    }
}

fn top_watched() -> MdbList {
    list(14, "Top Watched", "top-watched", "lina", 48, 961)
}

fn memberships(conn: &Connection) -> Vec<(String, String, i64)> {
    let mut stmt = conn
        .prepare("SELECT collection_id, item_id, rank FROM collection_membership ORDER BY collection_id, rank")
        .expect("prepare");
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
        .expect("count")
}

fn library() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().expect("tempdir");
    let conn = store(dir.path());
    movie(&conn, DARK_KNIGHT, "The Dark Knight", "tt0468569");
    movie(&conn, BATMAN_BEGINS, "Batman Begins", "tt0372784");
    (dir, conn)
}

#[test]
fn a_list_becomes_one_collection_row_and_ranked_memberships() {
    let (_dir, mut conn) = library();
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(
            14,
            vec![
                entry("tt0372784", "movie", "Batman Begins"),
                entry("tt9999999", "movie", "Not In Library"),
                entry("tt0468569", "movie", "The Dark Knight"),
            ],
        )]),
        ..FakeMdb::default()
    };
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");

    // Rank is array position, 1-indexed, with a title outside the library skipped over.
    assert_eq!(
        memberships(&conn),
        [
            ("mdblist:14".into(), BATMAN_BEGINS.into(), 1),
            ("mdblist:14".into(), DARK_KNIGHT.into(), 3),
        ]
    );
    assert_eq!(
        (
            stats.lists_seen,
            stats.lists_fetched,
            stats.entries_parsed,
            stats.entries_stored
        ),
        (1, 1, 3, 3)
    );
    assert_eq!(
        (stats.memberships_written, stats.entries_not_in_library),
        (2, 1)
    );
    let row: (String, String, i64, i64, String) = conn
        .query_row(
            "SELECT name, url, size, likes, provenance FROM collection WHERE collection_id = 'mdblist:14'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .expect("the collection row");
    assert_eq!(
        row,
        ("Top Watched".into(), TOP.into(), 48, 961, "feed".into())
    );
    // Every entry is stored, owned or not.
    assert_eq!(count(&conn, "collection_entry"), 3);
}

#[test]
fn a_movie_and_a_show_sharing_a_tmdb_number_are_not_fused() {
    let (_dir, mut conn) = library();
    seed(
        &conn,
        "tmdb:1678",
        "movie",
        "Some Movie",
        &[("tmdb", "1678")],
    );
    seed(
        &conn,
        "tmdb:1678-show",
        "show",
        "Some Show",
        &[("tmdb", "1678")],
    );
    let tmdb = |kind: &str| MdbEntry {
        external_ids: vec![("tmdb".into(), "1678".into())],
        media_type: kind.into(),
        title: None,
        year: None,
    };
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(14, vec![tmdb("show")])]),
        ..FakeMdb::default()
    };
    refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(
        memberships(&conn),
        [("mdblist:14".into(), "tmdb:1678-show".into(), 1)]
    );
}

#[test]
fn two_ids_for_one_title_keep_the_first_position() {
    let (_dir, mut conn) = library();
    seed(
        &conn,
        "both",
        "movie",
        "Both",
        &[("imdb", "tt1"), ("tmdb", "7")],
    );
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(
            14,
            vec![
                entry("tt1", "movie", "Both"),
                MdbEntry {
                    external_ids: vec![("tmdb".into(), "7".into())],
                    media_type: "movie".into(),
                    title: None,
                    year: None,
                },
            ],
        )]),
        ..FakeMdb::default()
    };
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(
        memberships(&conn),
        [("mdblist:14".into(), "both".into(), 1)]
    );
    assert_eq!((stats.entries_duplicate, stats.entries_stored), (1, 2));
}

#[test]
fn a_re_pull_replaces_what_the_list_held() {
    let (_dir, mut conn) = library();
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(
            14,
            vec![
                entry("tt0468569", "movie", "The Dark Knight"),
                entry("tt0372784", "movie", "Batman Begins"),
            ],
        )]),
        ..FakeMdb::default()
    };
    refresh_mdblist(&mut conn, &mut source, 45).expect("first harvest");
    assert_eq!(count(&conn, "collection_membership"), 2);

    // The list dropped a title and the fetch is due again.
    source
        .entries
        .insert(14, vec![entry("tt0468569", "movie", "The Dark Knight")]);
    conn.execute(
        "UPDATE collection SET observed_at = '2020-01-01T00:00:00+00:00'",
        [],
    )
    .expect("age the list");
    refresh_mdblist(&mut conn, &mut source, 45).expect("second harvest");
    assert_eq!(
        memberships(&conn),
        [("mdblist:14".into(), DARK_KNIGHT.into(), 1)]
    );
    assert_eq!(count(&conn, "collection_entry"), 1);
}

#[test]
fn a_fresh_list_is_not_fetched_again_unless_a_refetch_was_decided_after_it() {
    let (_dir, mut conn) = library();
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(14, vec![entry("tt0468569", "movie", "The Dark Knight")])]),
        ..FakeMdb::default()
    };
    refresh_mdblist(&mut conn, &mut source, 45).expect("first harvest");
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("second harvest");
    assert_eq!((stats.lists_cached, stats.lists_fetched), (1, 0));
    assert_eq!(source.entry_calls, [14]);

    decide(&conn, TOP, "refetch", "2099-01-01T00:00:00+00:00", 0);
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("third harvest");
    assert_eq!((stats.lists_cached, stats.lists_fetched), (0, 1));
    assert_eq!(source.entry_calls, [14, 14]);
}

#[test]
fn a_list_stored_without_entries_is_fetched_once_more() {
    let (_dir, mut conn) = library();
    let now = Now::read().iso();
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) \
         VALUES ('mdblist:14', 'mdblist', 'Top Watched', ?, 48, 961, ?)",
        (TOP, now),
    )
    .expect("a fresh row with no entries");
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(14, vec![entry("tt0468569", "movie", "The Dark Knight")])]),
        ..FakeMdb::default()
    };
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(stats.lists_fetched, 1);
}

#[test]
fn a_failing_list_is_recorded_stopped_after_three_and_retried_on_a_decision() {
    let (_dir, mut conn) = library();
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        failing: HashMap::from([(
            14,
            MdbError::new("MDBList returned 500 for /lists/14/items", Some(500)),
        )]),
        ..FakeMdb::default()
    };
    for attempt in 1..=3 {
        let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
        assert_eq!(stats.lists_failed, 1);
        let (attempts, reason): (i64, String) = conn
            .query_row(
                "SELECT attempts, reason FROM list_attempt WHERE list_key = ?",
                [TOP],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .expect("the attempt");
        assert_eq!(
            (attempts, reason.as_str()),
            (attempt, "MDBList returned 500 for /lists/14/items")
        );
    }
    assert_eq!(count(&conn, "collection"), 0);

    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!((stats.lists_stopped, stats.lists_failed), (1, 0));
    assert_eq!(source.entry_calls.len(), 3);

    // A retry decided after the last failure clears the count.
    decide(&conn, TOP, "retry", "2099-01-01T00:00:00+00:00", 0);
    source.failing.clear();
    source
        .entries
        .insert(14, vec![entry("tt0468569", "movie", "The Dark Knight")]);
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(stats.lists_fetched, 1);
    assert_eq!(count(&conn, "list_attempt"), 0);
}

#[test]
fn a_failure_that_says_nothing_about_one_list_stops_the_run() {
    let (_dir, mut conn) = library();
    let quota = MdbError {
        kind: Kind::Quota,
        status: Some(429),
        message: "MDBList returned 429".into(),
    };
    let mut source = FakeMdb {
        lists: vec![top_watched(), list(15, "Second", "second", "lina", 3, 1)],
        failing: HashMap::from([(14, quota)]),
        ..FakeMdb::default()
    };
    let err = refresh_mdblist(&mut conn, &mut source, 45).expect_err("the run stops");
    assert!(matches!(err, HarvestError::Mdb(ref e) if e.kind == Kind::Quota));
    assert_eq!(source.entry_calls, [14]);
    assert_eq!(count(&conn, "list_attempt"), 0);
}

#[test]
fn an_excluded_list_is_wiped_and_never_fetched_and_a_removed_one_goes_only_if_added() {
    let (_dir, mut conn) = library();
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(14, vec![entry("tt0468569", "movie", "The Dark Knight")])]),
        ..FakeMdb::default()
    };
    refresh_mdblist(&mut conn, &mut source, 45).expect("first harvest");
    assert_eq!(count(&conn, "collection"), 1);

    // A feed list stays on a `removed` decision: only a list a person added is dropped.
    decide(&conn, TOP, "removed", "2026-02-01T00:00:00+00:00", 0);
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!((stats.lists_removed, count(&conn, "collection")), (0, 1));

    conn.execute(
        "UPDATE list_decision SET decision = 'excluded', decided_at = '2026-03-01T00:00:00+00:00'",
        [],
    )
    .expect("exclude it");
    let before = source.entry_calls.len();
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!((stats.lists_removed, stats.lists_excluded), (1, 1));
    assert_eq!(source.entry_calls.len(), before);
    assert_eq!(
        (count(&conn, "collection"), count(&conn, "collection_entry")),
        (0, 0)
    );
}

#[test]
fn a_list_a_person_added_is_fetched_by_url_with_provenance_added() {
    let (_dir, mut conn) = library();
    decide(
        &conn,
        "https://mdblist.com/lists/ann/mine",
        "added",
        "2026-02-01T00:00:00+00:00",
        1,
    );
    let mut source = FakeMdb {
        named: HashMap::from([(
            ("ann".into(), "mine".into()),
            list(77, "Mine", "mine", "ann", 1, 0),
        )]),
        entries: HashMap::from([(77, vec![entry("tt0468569", "movie", "The Dark Knight")])]),
        ..FakeMdb::default()
    };
    let stats = refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(stats.lists_fetched, 1);
    assert_eq!(source.find_calls, [("ann".to_string(), "mine".to_string())]);
    let provenance: String = conn
        .query_row(
            "SELECT provenance FROM collection WHERE collection_id = 'mdblist:77'",
            [],
            |r| r.get(0),
        )
        .expect("the row");
    assert_eq!(provenance, "added");
}

#[test]
fn wiping_one_source_leaves_the_other() {
    let (_dir, mut conn) = library();
    let mut source = FakeMdb {
        lists: vec![top_watched()],
        entries: HashMap::from([(14, vec![entry("tt0468569", "movie", "The Dark Knight")])]),
        ..FakeMdb::default()
    };
    refresh_mdblist(&mut conn, &mut source, 45).expect("harvest");
    conn.execute(
        "INSERT INTO collection (collection_id, source, observed_at) \
         VALUES ('letterboxd:a/b', 'letterboxd', '2026-01-01T00:00:00+00:00')",
        [],
    )
    .expect("a letterboxd list");
    assert_eq!(
        wipe_source(&mut conn, LETTERBOXD_SOURCE).expect("wipe"),
        (0, 0, 1)
    );
    assert_eq!(count(&conn, "collection"), 1);
    assert_eq!(
        wipe_source(&mut conn, MDBLIST_SOURCE).expect("wipe"),
        (1, 1, 1)
    );
}

// --- the outward search ------------------------------------------------------------------

fn watched(conn: &Connection, item_id: &str, key: &str) {
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES (?, ?, 1, 1)",
        (key, item_id),
    )
    .expect("a play");
}

fn options(min_titles: i64) -> Options {
    Options {
        stale_days: 45,
        limit: 25,
        min_titles,
        max_lists: 10,
    }
}

#[test]
fn a_watched_title_is_searched_and_a_list_holding_enough_of_the_library_is_kept() {
    let (_dir, mut conn) = library();
    watched(&conn, DARK_KNIGHT, "h1");
    let found = list(31, "The Dark Knight Fans", "tdk-fans", "ann", 9, 5);
    let mut source = FakeMdb {
        search: HashMap::from([("The Dark Knight".into(), vec![found.clone()])]),
        named: HashMap::from([(("ann".into(), "tdk-fans".into()), found)]),
        entries: HashMap::from([(
            31,
            vec![
                entry("tt0468569", "movie", "The Dark Knight"),
                entry("tt0372784", "movie", "Batman Begins"),
            ],
        )]),
        ..FakeMdb::default()
    };
    let stats = refresh_list_search(&mut conn, &mut source, options(2)).expect("search");
    assert_eq!(source.search_calls, ["The Dark Knight"]);
    assert_eq!(
        (stats.seeds_asked, stats.lists_found, stats.lists_qualified),
        (1, 1, 1)
    );
    let (provenance, seeds): (String, i64) = conn
        .query_row(
            "SELECT provenance, seed_count FROM collection WHERE collection_id = 'mdblist:31'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("the kept list");
    // One of its two library titles is a seed, because only the Dark Knight was watched.
    assert_eq!((provenance.as_str(), seeds), ("search", 1));

    // The seed is fresh now: not asked again.
    let again = refresh_list_search(&mut conn, &mut source, options(2)).expect("search");
    assert_eq!((again.seeds_asked, again.seeds_cached), (0, 1));
    assert_eq!(source.search_calls.len(), 1);
}

#[test]
fn a_list_holding_too_little_is_rejected_and_not_fetched_again() {
    let (_dir, mut conn) = library();
    watched(&conn, DARK_KNIGHT, "h1");
    let thin = list(32, "Thin", "thin", "ann", 9, 5);
    let mut source = FakeMdb {
        search: HashMap::from([("The Dark Knight".into(), vec![thin.clone()])]),
        named: HashMap::from([(("ann".into(), "thin".into()), thin)]),
        entries: HashMap::from([(32, vec![entry("tt0468569", "movie", "The Dark Knight")])]),
        ..FakeMdb::default()
    };
    let stats = refresh_list_search(&mut conn, &mut source, options(2)).expect("search");
    assert_eq!((stats.lists_rejected, stats.lists_qualified), (1, 0));
    assert_eq!(count(&conn, "collection"), 0);
    let rejected: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM enrichment_cursor WHERE key LIKE 'rejected:%'",
            [],
            |r| r.get(0),
        )
        .expect("count");
    assert_eq!(rejected, 1);

    // Wipe the seed's own cursor so it is asked again; the rejected list is still not fetched.
    conn.execute("DELETE FROM enrichment_cursor WHERE key = 'fetched'", [])
        .expect("forget the seed");
    let again = refresh_list_search(&mut conn, &mut source, options(2)).expect("search");
    assert_eq!(again.harvest.lists_cached, 1);
    assert_eq!(source.entry_calls, [32]);
}

#[test]
fn a_failed_search_moves_the_seed_to_the_back_and_a_wipe_forgets_the_searches() {
    let (_dir, mut conn) = library();
    watched(&conn, DARK_KNIGHT, "h1");
    let mut source = FakeMdb {
        failing_searches: HashMap::from([(
            "The Dark Knight".into(),
            MdbError::new("MDBList returned 500", Some(500)),
        )]),
        ..FakeMdb::default()
    };
    let stats = refresh_list_search(&mut conn, &mut source, options(2)).expect("search");
    assert_eq!(stats.seeds_failed, 1);
    let attempted: String = conn
        .query_row(
            "SELECT fetched_at FROM enrichment_cursor WHERE key = 'attempted'",
            [],
            |r| r.get(0),
        )
        .expect("the attempted cursor");
    assert!(
        attempted.contains('.'),
        "attempted is stamped to the microsecond: {attempted}"
    );
    assert_eq!(wipe_searched(&mut conn).expect("wipe"), (0, 1));
    assert_eq!(count(&conn, "enrichment_cursor"), 0);
}

#[test]
fn the_search_stops_when_the_quota_is_spent() {
    let (_dir, mut conn) = library();
    watched(&conn, DARK_KNIGHT, "h1");
    let mut source = FakeMdb {
        failing_searches: HashMap::from([(
            "The Dark Knight".into(),
            MdbError {
                kind: Kind::Quota,
                status: Some(429),
                message: "quota".into(),
            },
        )]),
        ..FakeMdb::default()
    };
    assert!(refresh_list_search(&mut conn, &mut source, options(2)).is_err());
    assert_eq!(count(&conn, "enrichment_cursor"), 0);
}

// --- Letterboxd -------------------------------------------------------------------------

#[derive(Default)]
struct FakeLb {
    lists: HashMap<(String, String), Result<ListContents, LbError>>,
    films: HashMap<String, Result<FilmIds, LbError>>,
    film_calls: Vec<String>,
}

impl LbSource for FakeLb {
    fn list_contents(&mut self, user: &str, slug: &str) -> Result<ListContents, LbError> {
        self.lists
            .get(&(user.to_string(), slug.to_string()))
            .cloned()
            .unwrap_or_else(|| {
                Err(LbError(format!(
                    "Letterboxd returned 404 for /{user}/list/{slug}/"
                )))
            })
    }

    fn film_ids(&mut self, slug: &str) -> Result<FilmIds, LbError> {
        self.film_calls.push(slug.into());
        self.films.get(slug).cloned().unwrap_or_else(|| {
            Err(LbError(format!(
                "Letterboxd returned 404 for /film/{slug}/"
            )))
        })
    }
}

fn film(slug: &str, title: &str, year: i64) -> ListFilm {
    ListFilm {
        slug: slug.into(),
        title: title.into(),
        year: Some(year),
    }
}

fn contents(name: &str, size: Option<i64>, films: Vec<ListFilm>) -> ListContents {
    ListContents {
        name: name.into(),
        size,
        entries: films,
        pages: 1,
    }
}

fn ids(kind: &str, tmdb: Option<&str>) -> Result<FilmIds, LbError> {
    Ok(FilmIds {
        media_type: kind.into(),
        tmdb_id: tmdb.map(str::to_string),
    })
}

const LB_URL: &str = "https://letterboxd.com/ann/list/best";

#[test]
fn a_letterboxd_list_resolves_through_each_films_tmdb_id() {
    let (_dir, mut conn) = library();
    seed(&conn, "heat", "movie", "Heat", &[("tmdb", "949")]);
    seed(&conn, "show", "show", "A Show", &[("tmdb", "1396")]);
    decide(&conn, LB_URL, "added", "2026-02-01T00:00:00+00:00", 1);
    let mut source = FakeLb {
        lists: HashMap::from([(
            ("ann".into(), "best".into()),
            Ok(contents(
                "Best",
                Some(4),
                vec![
                    film("heat", "Heat", 1995),
                    film("show", "A Show", 2008),
                    film("obscure", "Obscure", 1999),
                    film("heat", "Heat", 1995),
                ],
            )),
        )]),
        films: HashMap::from([
            ("heat".into(), ids("movie", Some("949"))),
            ("show".into(), ids("show", Some("1396"))),
            ("obscure".into(), ids("movie", None)),
        ]),
        ..FakeLb::default()
    };
    let stats = refresh_letterboxd_lists(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(
        memberships(&conn),
        [
            ("letterboxd:ann/best".into(), "heat".into(), 1),
            ("letterboxd:ann/best".into(), "show".into(), 2),
        ]
    );
    // A film is read once per run however many lists name it.
    assert_eq!(source.film_calls, ["heat", "show", "obscure"]);
    assert_eq!(
        (
            stats.entries_parsed,
            stats.entries_stored,
            stats.entries_not_in_library
        ),
        (4, 3, 1)
    );
    assert_eq!(stats.entries_duplicate, 1);
    // A film with no TMDB match is stored under the letterboxd namespace.
    let unmatched: (String, String) = conn
        .query_row(
            "SELECT ns, value FROM collection_entry WHERE title = 'Obscure'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("the entry");
    assert_eq!(unmatched, ("letterboxd".into(), "obscure".into()));
    let (size, likes): (i64, Option<i64>) = conn
        .query_row(
            "SELECT size, likes FROM collection WHERE url = ?",
            [LB_URL],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("the list");
    assert_eq!((size, likes), (4, None));
}

#[test]
fn a_letterboxd_list_read_short_or_failing_writes_nothing() {
    let (_dir, mut conn) = library();
    decide(&conn, LB_URL, "added", "2026-02-01T00:00:00+00:00", 1);
    let mut source = FakeLb {
        lists: HashMap::from([(
            ("ann".into(), "best".into()),
            Ok(contents("Best", Some(50), vec![film("heat", "Heat", 1995)])),
        )]),
        ..FakeLb::default()
    };
    let stats = refresh_letterboxd_lists(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(stats.lists_failed, 1);
    let reason: String = conn
        .query_row(
            "SELECT reason FROM list_attempt WHERE list_key = ?",
            [LB_URL],
            |r| r.get(0),
        )
        .expect("the attempt");
    assert_eq!(reason, "read 1 of the 50 entries the list says it holds");
    assert_eq!(count(&conn, "collection"), 0);
    assert!(source.film_calls.is_empty());
}

#[test]
fn the_run_stops_after_three_lists_in_a_row_fail() {
    let (_dir, mut conn) = library();
    for name in ["a", "b", "c", "d"] {
        decide(
            &conn,
            &format!("https://letterboxd.com/ann/list/{name}"),
            "added",
            "2026-02-01T00:00:00+00:00",
            1,
        );
    }
    let mut source = FakeLb::default();
    let stats = refresh_letterboxd_lists(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!((stats.lists_seen, stats.lists_failed), (3, 3));
    assert_eq!(count(&conn, "list_attempt"), 3);
}

#[test]
fn a_stored_letterboxd_list_is_refreshed_without_a_decision_and_keeps_its_provenance() {
    let (_dir, mut conn) = library();
    conn.execute(
        "INSERT INTO collection (collection_id, source, name, url, observed_at, provenance) \
         VALUES ('letterboxd:ann/best', 'letterboxd', 'Old', ?, ?, 'added')",
        (LB_URL, "2020-01-01T00:00:00+00:00"),
    )
    .expect("a stored list");
    let mut source = FakeLb {
        lists: HashMap::from([(
            ("ann".into(), "best".into()),
            Ok(contents("New", None, vec![film("heat", "Heat", 1995)])),
        )]),
        films: HashMap::from([("heat".into(), ids("movie", None))]),
        ..FakeLb::default()
    };
    let stats = refresh_letterboxd_lists(&mut conn, &mut source, 45).expect("harvest");
    assert_eq!(stats.lists_fetched, 1);
    let (name, size): (String, i64) = conn
        .query_row(
            "SELECT name, size FROM collection WHERE url = ?",
            [LB_URL],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("the list");
    // A list that states no size is sized by what was read.
    assert_eq!((name.as_str(), size), ("New", 1));
}
