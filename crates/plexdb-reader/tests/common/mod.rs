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

-- Account 42 watched tt1 twice (a rewatch, h1 and h3) and tt2 once. Account
-- 43 has never played anything, for the "no plays -> empty vector" case.
INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) VALUES
    ('h1', 'imdb:tt1', 42, 1700000000),
    ('h2', 'imdb:tt2', 42, 1700000100),
    ('h3', 'imdb:tt1', 42, 1700000200);
"#;
