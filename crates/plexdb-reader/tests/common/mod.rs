//! Fixture-store builder shared by this crate's integration tests.
//!
//! Per issue #11, the crate is "tested against a fixture store built by
//! this repository's own writer" — so schema creation goes through the real
//! `plexdb init` CLI (via `uv run`), never a hand-copied version of
//! `plexdb/schema.py`'s DDL that could drift out of sync with it. Seed rows
//! beyond the bare schema are plain `INSERT`s against that same schema,
//! exactly as `plexdb`'s own Python test suite builds its fixtures
//! (`tests/test_schema.py`).

use std::path::{Path, PathBuf};
use std::process::Command;

use rusqlite::Connection;

/// The plex-db-ex repo root, two levels above this crate's manifest.
pub fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crates/plexdb-reader sits two levels under the repo root")
        .to_path_buf()
}

/// Build a fixture store at `path`: create the real schema by driving
/// `plexdb init`, then seed a small, hand-written dataset. Returns an open,
/// writable connection to it (closed when dropped) in case a test needs to
/// mutate the fixture further, e.g. to force a schema-version mismatch.
pub fn build_fixture(path: &Path) -> Connection {
    let status = Command::new("uv")
        .args(["run", "plexdb", "init"])
        .env("PLEXDB_PATH", path)
        .current_dir(repo_root())
        .status()
        .expect("failed to run `uv run plexdb init` — is uv on PATH?");
    assert!(
        status.success(),
        "plexdb init failed to build the fixture schema"
    );

    let conn = Connection::open(path).expect("open the freshly-created fixture for seeding");
    conn.execute_batch(SEED).expect("seed the fixture store");
    seed_episodes(&conn);
    conn.execute_batch(MOVIE_PLAYS)
        .expect("seed the movie plays");
    conn
}

/// Three titles, two Plex-only plays (one a rewatch), a handful of
/// enrichment facts, and edges in both directions between them — enough to
/// exercise every accessor's filtering, direction, and dedup behaviour.
const SEED: &str = r#"
INSERT INTO items (item_id, type, title, year) VALUES
    ('imdb:tt1', 'movie', 'Alpha', 2001),
    ('imdb:tt2', 'movie', 'Beta',  2002),
    ('imdb:tt3', 'movie', 'Gamma', 2003);

INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) VALUES
    ('imdb:tt1', 'tmdb_keywords', 'keyword', 'heist',         '2026-01-01T00:00:00+00:00'),
    ('imdb:tt1', 'tmdb_keywords', 'keyword', 'ensemble cast', '2026-01-01T00:00:00+00:00'),
    ('imdb:tt2', 'tmdb_keywords', 'keyword', 'heist',         '2026-01-01T00:00:00+00:00'),
    ('imdb:tt3', 'tmdb_keywords', 'keyword', 'space',         '2026-01-01T00:00:00+00:00');

INSERT INTO edges (from_id, to_id, edge_type, rank, fetched_at) VALUES
    ('imdb:tt1', 'imdb:tt2', 'tmdb_similar',         1, '2026-01-01T00:00:00+00:00'),
    ('imdb:tt1', 'imdb:tt3', 'tmdb_similar',         2, '2026-01-01T00:00:00+00:00'),
    ('imdb:tt2', 'imdb:tt1', 'tmdb_recommendations', 1, '2026-01-01T00:00:00+00:00');

-- Two crowd lists from different sources, one fully populated (MDBList-shaped:
-- rank and the list's own size/likes), one carrying only what a mentions-only
-- source like a subreddit would have. tt1 sits on both, so its membership
-- rows exercise both nullable shapes at once; tt2 sits on the ranked list
-- with neither rank nor mentions recorded (an unordered entry).
INSERT INTO collection (collection_id, source, name, url, size, likes, observed_at) VALUES
    ('mdblist:100', 'mdblist', 'Best Heists', 'https://mdblist.com/lists/100', 50, 1200, '2026-01-01T00:00:00+00:00'),
    ('reddit:heist', 'reddit', NULL, NULL, NULL, NULL, '2026-01-01T00:00:00+00:00');

INSERT INTO collection_membership (collection_id, item_id, rank, mentions, observed_at) VALUES
    ('mdblist:100',  'imdb:tt1', 3,    NULL, '2026-01-01T00:00:00+00:00'),
    ('reddit:heist', 'imdb:tt1', NULL, 7,    '2026-01-02T00:00:00+00:00'),
    ('mdblist:100',  'imdb:tt2', NULL, NULL, '2026-01-01T00:00:00+00:00');

-- Three shows, to exercise ADR-0011's season unit:
--   ttfin   two seasons of 5, watched exactly one season   -> r = 1.0, kept
--   ttbail  one season of 10, watched twice                -> r = 0.2, dropped
--   ttnosea no season numbers at all                       -> reported, not silently weighed
INSERT INTO items (item_id, type, title, year) VALUES
    ('imdb:ttfin',   'show', 'Finished',   2010),
    ('imdb:ttbail',  'show', 'Abandoned',  2011),
    ('imdb:ttnosea', 'show', 'No Seasons', 2012);

INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) VALUES
    ('imdb:ttfin',   'tmdb_keywords', 'keyword', 'sitcom',    '2026-01-01T00:00:00+00:00'),
    ('imdb:ttfin',   'tmdb_keywords', 'keyword', 'workplace', '2026-01-01T00:00:00+00:00'),
    ('imdb:ttbail',  'tmdb_keywords', 'keyword', 'bailed',    '2026-01-01T00:00:00+00:00'),
    ('imdb:ttnosea', 'tmdb_keywords', 'keyword', 'seasonless','2026-01-01T00:00:00+00:00');

-- Bookkeeping, exactly as `plexdb enrich-tmdb-keywords` writes it: rows in
-- `enrichment_cursor`, never in `enrichment` (ADR-0013). Seeded here so the
-- rollup is exercised against a store that has bookkeeping in it — a fixture
-- with none would pass whether or not the rollup could tell the difference.
INSERT INTO enrichment_cursor (item_id, namespace, key, fetched_at) VALUES
    ('imdb:ttfin', 'tmdb_keywords', 'fetched', '2026-01-01T00:00:00+00:00'),
    ('imdb:tt2',   'tmdb_keywords', 'fetched', '2026-01-01T00:00:00+00:00'),
    ('imdb:ttfin', 'tmdb_edges',    'fetched_recommendations', '2026-01-01T00:00:00+00:00'),
    ('imdb:ttfin', 'tmdb_edges',    'fetched_similar', '2026-01-01T00:00:00+00:00');
"#;

/// Episodes and the plays over them, built in code because a two-season show
/// is 10 near-identical rows and a literal block hides the shape.
pub fn seed_episodes(conn: &Connection) {
    let mut sql = String::new();
    for season in 1..=2 {
        for episode in 1..=5 {
            sql.push_str(&format!(
                "INSERT INTO items (item_id, type, title, show_item_id, season, episode) \
                 VALUES ('fin-s{season}e{episode}', 'episode', 'Finished S{season}E{episode}', \
                 'imdb:ttfin', {season}, {episode});\n"
            ));
        }
    }
    for episode in 1..=10 {
        sql.push_str(&format!(
            "INSERT INTO items (item_id, type, title, show_item_id, season, episode) \
             VALUES ('bail-s1e{episode}', 'episode', 'Abandoned S1E{episode}', \
             'imdb:ttbail', 1, {episode});\n"
        ));
    }
    // Season deliberately NULL — the case ADR-0011 says must be reported.
    for episode in 1..=4 {
        sql.push_str(&format!(
            "INSERT INTO items (item_id, type, title, show_item_id, season, episode) \
             VALUES ('nosea-{episode}', 'episode', 'No Seasons {episode}', \
             'imdb:ttnosea', NULL, {episode});\n"
        ));
    }

    // Account 42: one full season of ttfin (5 of a median-5 season, r = 1.0),
    // two episodes of ttbail (r = 0.2, under the half-season floor), and two
    // of ttnosea (no season length to divide by).
    for episode in 1..=5 {
        sql.push_str(&format!(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) \
             VALUES ('f{episode}', 'fin-s1e{episode}', 42, 17001000{episode:02});\n"
        ));
    }
    for episode in 1..=2 {
        sql.push_str(&format!(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) \
             VALUES ('b{episode}', 'bail-s1e{episode}', 42, 17002000{episode:02});\n"
        ));
    }
    for episode in 1..=2 {
        sql.push_str(&format!(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) \
             VALUES ('n{episode}', 'nosea-{episode}', 42, 17003000{episode:02});\n"
        ));
    }
    conn.execute_batch(&sql)
        .expect("seed episodes and their plays");
}

const MOVIE_PLAYS: &str = r#"
-- Account 42 watched tt1 twice (a rewatch, h1 and h3) and tt2 once. Account
-- 43 has never played anything, for the "no plays -> empty vector" case.
INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES
    ('h1', 'imdb:tt1', 42, 1700000000),
    ('h2', 'imdb:tt2', 42, 1700000100),
    ('h3', 'imdb:tt1', 42, 1700000200);
"#;
