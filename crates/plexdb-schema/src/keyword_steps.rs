//! Versions 10 and 17, the two steps that re-normalize stored keywords.
//!
//! Version 10 (ADR-0016) folds `tmdb_keywords` into one `keywords` namespace with
//! a `source` column, re-normalizing every keyword value and filling
//! `keyword_forms` from the raw spellings. Version 17 replaces Snowball stems with
//! folded plurals, recovering each row's raw spelling from `keyword_forms` into
//! `keyword_surfaces` and re-deriving every keyword row from it.
//!
//! Both need the writer's keyword normalizer, which has not moved to Rust yet. So
//! each runs in full only on a store holding no keyword rows — every store built
//! from nothing passes through both while empty — and [`crate::preflight`] refuses
//! a store below v17 that holds any before a step runs. Every store at version 17
//! or later is past both and never meets this.
//!
//! The `CREATE TABLE` text below is spaced exactly as the step that shipped
//! wrote it, because SQLite keeps that text in `sqlite_master` and a store built
//! here should read the same as one built then.

use rusqlite::{Connection, Transaction};

use crate::SchemaError;

const V10_ENRICHMENT: &str = "CREATE TABLE enrichment_v10 (
            item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
            namespace  TEXT NOT NULL,
            source     TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (item_id, namespace, source, key, value)
        )";

const V10_CURSOR: &str = "CREATE TABLE enrichment_cursor_v10 (
            item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
            namespace  TEXT NOT NULL,
            source     TEXT NOT NULL,
            key        TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (item_id, namespace, source, key)
        )";

const V17_SURFACES: &str = "CREATE TABLE keyword_surfaces (
            item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
            source     TEXT NOT NULL,
            surface    TEXT NOT NULL,
            rank       INTEGER,
            spoiler    INTEGER NOT NULL CHECK (spoiler IN (0, 1)),
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (item_id, source, surface)
        )";

/// Keyword rows a store below v17 holds: `tmdb_keywords` and `keywords` facts,
/// and `keyword_forms` spellings once version 10 has made the table. Either step
/// would have to re-normalize every one of them.
pub(crate) fn keyword_rows(conn: &Connection) -> Result<i64, SchemaError> {
    let forms: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'keyword_forms')",
        [],
        |row| row.get(0),
    )?;
    let facts: i64 = conn.query_row(
        "SELECT COUNT(*) FROM enrichment WHERE namespace IN ('tmdb_keywords', 'keywords') \
         AND key IN ('keyword', 'spoiler_keyword')",
        [],
        |row| row.get(0),
    )?;
    let spellings: i64 = if forms {
        conn.query_row("SELECT COUNT(*) FROM keyword_forms", [], |row| row.get(0))?
    } else {
        0
    };
    Ok(facts + spellings)
}

/// Version 10. Every non-keyword `enrichment` row takes its own namespace as its
/// `source`, there being no other writer of `enrichment` when it shipped to name a
/// truer one. `enrichment_cursor` gains the same column, but both of its known
/// pre-migration namespaces came from TMDB: the keyword cursor (`tmdb_keywords`,
/// renamed to `keywords`) and the edge cursor (`tmdb_edges`) both take
/// `source='tmdb'`, and only a namespace the step cannot name uses itself. No two
/// cursor rows collide once `source` is added, so that part is a plain copy.
pub(crate) fn v10(tx: &Transaction) -> Result<(), SchemaError> {
    tx.execute(
        "CREATE TABLE keyword_forms (surface TEXT PRIMARY KEY, keyword TEXT NOT NULL)",
        [],
    )?;
    tx.execute(V10_ENRICHMENT, [])?;
    tx.execute_batch(
        "INSERT INTO enrichment_v10 (item_id, namespace, source, key, value, fetched_at)
             SELECT item_id, namespace, namespace, key, value, fetched_at FROM enrichment;
         DROP TABLE enrichment;
         ALTER TABLE enrichment_v10 RENAME TO enrichment;
         CREATE INDEX idx_enrichment_ns_key ON enrichment(namespace, key);
         CREATE INDEX idx_enrichment_item_ns ON enrichment(item_id, namespace);",
    )?;
    tx.execute(V10_CURSOR, [])?;
    tx.execute_batch(
        "INSERT INTO enrichment_cursor_v10 (item_id, namespace, source, key, fetched_at)
             SELECT item_id,
                    CASE namespace WHEN 'tmdb_keywords' THEN 'keywords' ELSE namespace END,
                    CASE WHEN namespace IN ('tmdb_keywords', 'tmdb_edges') THEN 'tmdb'
                         ELSE namespace END,
                    key, fetched_at
             FROM enrichment_cursor;
         DROP TABLE enrichment_cursor;
         ALTER TABLE enrichment_cursor_v10 RENAME TO enrichment_cursor;",
    )?;
    Ok(())
}

/// Version 17. `idx_enrichment_ns_key` widens to `(namespace, key, value)`, so
/// finding or counting the titles that carry a keyword reads the index instead
/// of every keyword row, and `keyword_surfaces` holds each keyword row's raw
/// spelling from here on.
pub(crate) fn v17(tx: &Transaction) -> Result<(), SchemaError> {
    tx.execute_batch(
        "DROP INDEX idx_enrichment_ns_key;
         CREATE INDEX idx_enrichment_ns_key ON enrichment(namespace, key, value);",
    )?;
    tx.execute(V17_SURFACES, [])?;
    Ok(())
}
