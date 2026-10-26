//! Versions 10 and 17, the two steps that re-normalize stored keywords.
//!
//! Version 10 (ADR-0016) folds `tmdb_keywords` into one `keywords` namespace with
//! a `source` column, re-normalizing every keyword value and filling
//! `keyword_forms` from the raw spellings. Version 17 replaces Snowball stems with
//! folded plurals, recovering each row's raw spelling from `keyword_forms` into
//! `keyword_surfaces` and re-deriving every keyword row from it.
//!
//! Both normalize in Rust with `plexdb-keywords`, the one normalizer the writer
//! has, and both may merge rows that normalize to one fact, so each declares the
//! exact `enrichment` row count it lands at ([`crate::Shrinks`]): `plexdb migrate`
//! accepts a drop in a guarded table only down to that number.
//!
//! The `CREATE TABLE` text below is spaced exactly as the step that shipped
//! wrote it, because SQLite keeps that text in `sqlite_master` and a store built
//! here should read the same as one built then.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use plexdb_keywords::{
    KEYWORD_KEY, NAMESPACE, SPOILER_KEY, normalize_keyword, rederive_keywords, upsert_keyword_form,
};
use rusqlite::{Transaction, params};

use crate::{SchemaError, Shrinks};

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

/// Version 10. `tmdb_keywords` rows move to the source-agnostic `keywords`
/// namespace with `source='tmdb'`, each value re-normalized and its raw spelling
/// recorded in `keyword_forms`; where two rows collide after normalization (`heists`
/// and `heist` on one title, from one source) the newer `fetched_at` wins. Every
/// other `enrichment` row takes its own namespace as its `source`, there being no
/// other writer of `enrichment` when it shipped to name a truer one.
/// `enrichment_cursor` gains the same column, but both of its known pre-migration
/// namespaces came from TMDB: the keyword cursor (`tmdb_keywords`, renamed to
/// `keywords`) and the edge cursor (`tmdb_edges`) both take `source='tmdb'`, and
/// only a namespace the step cannot name uses itself. No two cursor rows collide
/// once `source` is added, so that part is a plain copy.
///
/// **The one migration that can legitimately shrink `enrichment`**: it returns the
/// exact row count it landed at as a declared shrink, so a drop to that number is
/// accepted and a `v10` that also lost an unrelated row lands on another number and
/// is rolled back.
pub(crate) fn v10(tx: &Transaction) -> Result<Shrinks, SchemaError> {
    tx.execute(
        "CREATE TABLE keyword_forms (surface TEXT PRIMARY KEY, keyword TEXT NOT NULL)",
        [],
    )?;
    tx.execute(V10_ENRICHMENT, [])?;

    type Row = (String, String, String, String, String);
    let rows: Vec<Row> = tx
        .prepare("SELECT item_id, namespace, key, value, fetched_at FROM enrichment")?
        .query_map([], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })?
        .collect::<Result<_, _>>()?;
    // Keyed on the *post*-migration primary key, so two pre-migration rows that
    // normalize to one value merge here rather than failing on insert.
    let mut order: Vec<(String, String, String, String, String)> = Vec::new();
    let mut merged: HashMap<(String, String, String, String, String), String> = HashMap::new();
    for (item_id, namespace, key, value, fetched_at) in rows {
        let (namespace, source, value) = if namespace == "tmdb_keywords" {
            let stored = upsert_keyword_form(tx, &value)?;
            (NAMESPACE.to_string(), "tmdb".to_string(), stored)
        } else {
            (namespace.clone(), namespace, value)
        };
        let row_key = (item_id, namespace, source, key, value);
        match merged.get_mut(&row_key) {
            None => {
                order.push(row_key.clone());
                merged.insert(row_key, fetched_at);
            }
            Some(held) if fetched_at > *held => *held = fetched_at,
            Some(_) => {}
        }
    }
    {
        let mut insert = tx.prepare(
            "INSERT INTO enrichment_v10 (item_id, namespace, source, key, value, fetched_at) \
             VALUES (?, ?, ?, ?, ?, ?)",
        )?;
        for row_key in &order {
            let (item_id, namespace, source, key, value) = row_key;
            insert.execute(params![
                item_id,
                namespace,
                source,
                key,
                value,
                merged[row_key]
            ])?;
        }
    }
    tx.execute_batch(
        "DROP TABLE enrichment;
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
    Ok(vec![("enrichment", merged.len() as i64)])
}

/// Version 17. Keywords are folded plurals, not Snowball stems, derived from the raw
/// spellings `keyword_surfaces` keeps (ADR-0016).
///
/// `enrichment` held only stems, so each row's raw spelling is recovered from
/// `keyword_forms`. A stem whose spellings all normalize to one new form
/// (`christma` ← Christmas) takes that form's shortest spelling as its surface. A
/// stem whose spellings now normalize apart (`anim` ← animal, animation, anime)
/// cannot say which one each row came from, and neither can a stem with no spelling
/// recorded at all: its rows keep the stem itself as their surface, and the
/// source's cursor for that title is deleted, so the next sweep fetches the title
/// again and replaces them with what the source really sent. Then every keyword
/// row is re-derived, and the rows keyed on a keyword — pair, role, role-decision
/// and role-statement verdicts — move to the new form where the stem maps to
/// exactly one; the rest stay until the prune step finds their keyword gone.
///
/// Re-deriving can merge two stems into one form on a title, so `enrichment` may
/// shrink, by exactly the count this declares.
///
/// `idx_enrichment_ns_key` widens to `(namespace, key, value)`, so finding or
/// counting the titles that carry a keyword reads the index instead of every
/// keyword row, and `keyword_surfaces` holds each keyword row's raw spelling from
/// here on.
pub(crate) fn v17(tx: &Transaction) -> Result<Shrinks, SchemaError> {
    tx.execute_batch(
        "DROP INDEX idx_enrichment_ns_key;
         CREATE INDEX idx_enrichment_ns_key ON enrichment(namespace, key, value);",
    )?;
    tx.execute(V17_SURFACES, [])?;

    let mut forms: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for pair in tx
        .prepare("SELECT surface, keyword FROM keyword_forms")?
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
    {
        let (surface, stem) = pair?;
        forms.entry(stem).or_default().push(surface);
    }
    // Each stem's new form when every spelling of it lands on one, with the shortest
    // such spelling to stand in as the surface; absent when they split.
    let mut moved: HashMap<&str, (String, &str)> = HashMap::new();
    for (stem, surfaces) in &forms {
        let targets: BTreeSet<String> = surfaces
            .iter()
            .map(|surface| normalize_keyword(surface))
            .filter(|target| !target.is_empty())
            .collect();
        if targets.len() == 1 {
            let shortest = surfaces
                .iter()
                .min_by_key(|surface| (surface.chars().count(), surface.as_str()))
                .expect("a stem with spellings has a shortest");
            moved.insert(
                stem,
                (targets.into_iter().next().expect("one target"), shortest),
            );
        }
    }

    type KeywordRow = (String, String, String, String, Option<i64>, String);
    let keyword_rows: Vec<KeywordRow> = tx
        .prepare(
            "SELECT item_id, source, key, value, rank, fetched_at FROM enrichment \
             WHERE namespace = ? AND key IN (?, ?)",
        )?
        .query_map(params![NAMESPACE, KEYWORD_KEY, SPOILER_KEY], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        })?
        .collect::<Result<_, _>>()?;
    let mut refetch: BTreeSet<(&str, &str)> = BTreeSet::new();
    {
        let mut insert = tx.prepare(
            "INSERT INTO keyword_surfaces (item_id, source, surface, rank, spoiler, fetched_at) \
             VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (item_id, source, surface) DO UPDATE SET \
             rank = max(coalesce(rank, excluded.rank), coalesce(excluded.rank, rank)), \
             spoiler = max(spoiler, excluded.spoiler), \
             fetched_at = max(fetched_at, excluded.fetched_at)",
        )?;
        for (item_id, source, key, stem, rank, fetched_at) in &keyword_rows {
            let surface = match moved.get(stem.as_str()) {
                Some((_, surface)) => *surface,
                None => {
                    refetch.insert((item_id, source));
                    stem
                }
            };
            insert.execute(params![
                item_id,
                source,
                surface,
                rank,
                i64::from(key == SPOILER_KEY),
                fetched_at
            ])?;
        }
    }
    {
        let mut delete = tx.prepare(
            "DELETE FROM enrichment_cursor WHERE item_id = ? AND namespace = ? AND source = ?",
        )?;
        for (item_id, source) in &refetch {
            delete.execute(params![item_id, NAMESPACE, source])?;
        }
    }

    let total: i64 = tx.query_row("SELECT COUNT(*) FROM enrichment", [], |row| row.get(0))?;
    let other_rows = total - keyword_rows.len() as i64;
    let written = rederive_keywords(tx)? as i64;

    let rename: BTreeMap<&str, &str> = moved
        .iter()
        .filter(|(stem, (target, _))| target.as_str() != **stem)
        .map(|(stem, (target, _))| (*stem, target.as_str()))
        .collect();
    rekey(tx, &rename)?;
    Ok(vec![("enrichment", other_rows + written)])
}

fn columns(tx: &Transaction, table: &str) -> Result<Vec<String>, SchemaError> {
    Ok(tx
        .prepare(&format!("PRAGMA table_info({table})"))?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<_, _>>()?)
}

/// Move every verdict row keyed on a renamed keyword to its new name. Where two rows
/// land on one key, a row carrying a person's decision wins over one without, then
/// the newer; a pair whose two keywords land on one name is no pair and is dropped.
fn rekey(tx: &Transaction, rename: &BTreeMap<&str, &str>) -> Result<(), SchemaError> {
    tx.execute(
        "CREATE TEMP TABLE v17_rename (old TEXT PRIMARY KEY, new TEXT NOT NULL)",
        [],
    )?;
    {
        let mut insert = tx.prepare("INSERT INTO v17_rename VALUES (?, ?)")?;
        for (old, new) in rename {
            insert.execute([old, new])?;
        }
    }
    let renamed =
        |col: &str| format!("coalesce((SELECT new FROM v17_rename WHERE old = {col}), {col})");
    for table in [
        "keyword_roles",
        "keyword_role_decisions",
        "keyword_role_statements",
    ] {
        let all = columns(tx, table)?;
        let select = all
            .iter()
            .map(|col| {
                if col == "keyword" {
                    renamed(col)
                } else {
                    col.clone()
                }
            })
            .collect::<Vec<_>>()
            .join(", ");
        tx.execute(
            &format!("CREATE TEMP TABLE v17_{table} AS SELECT {select} FROM {table}"),
            [],
        )?;
        tx.execute(&format!("DELETE FROM {table}"), [])?;
        let newest = if all.iter().any(|col| col == "decided_at") {
            "decided_at"
        } else {
            "stated_at"
        };
        tx.execute(
            &format!(
                "INSERT OR IGNORE INTO {table} SELECT * FROM v17_{table} ORDER BY {newest} DESC"
            ),
            [],
        )?;
        tx.execute(&format!("DROP TABLE v17_{table}"), [])?;
    }
    let all = columns(tx, "keyword_pairs")?;
    let (a, b) = (renamed("keyword_a"), renamed("keyword_b"));
    let rest = all
        .iter()
        .filter(|col| *col != "keyword_a" && *col != "keyword_b")
        .cloned()
        .collect::<Vec<_>>()
        .join(", ");
    tx.execute(
        &format!(
            "CREATE TEMP TABLE v17_pairs AS SELECT min({a}, {b}) AS keyword_a, \
             max({a}, {b}) AS keyword_b, {rest} FROM keyword_pairs"
        ),
        [],
    )?;
    tx.execute("DELETE FROM keyword_pairs", [])?;
    tx.execute(
        &format!(
            "INSERT OR IGNORE INTO keyword_pairs (keyword_a, keyword_b, {rest}) \
             SELECT keyword_a, keyword_b, {rest} FROM v17_pairs WHERE keyword_a < keyword_b \
             ORDER BY decision IS NULL, coalesce(decided_at, judged_at) DESC"
        ),
        [],
    )?;
    tx.execute("DROP TABLE v17_pairs", [])?;
    tx.execute("DROP TABLE v17_rename", [])?;
    Ok(())
}
