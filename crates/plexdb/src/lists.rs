//! Reading crowd lists back out of the store: the list table and one list's detail
//! (`plexdb/lists.py`).
//!
//! Nothing here is stored (ADR-0012). Owned count, owned share, watched count and a
//! keyword's over-representation are computed from what the source said and what the
//! library holds, at the moment of the read; a title walked tomorrow joins its lists with
//! nothing rewritten (ADR-0027). An entry is owned when it resolves to an item: through
//! its stored `(ns, value, kind)` in `external_ids`, and failing that through the
//! membership the harvest wrote at the same position.
//!
//! Python's explorer calls the same procedure in-process until it moves
//! (plex-db-ex-f58.11); both give the same JSON, which `plexdb lists --json` prints.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use rusqlite::{Connection, Row, params};
use serde_json::{Map, Value, json};

use crate::clock::is_before;
use crate::collections::MAX_FAILURES;
use crate::decision_book::Latest;
use crate::list_decisions::{canonical_list_url, letterboxd_list_parts, list_source};
use crate::pyurl::urlparse;

/// A keyword is reported for a list when at least this many of the list's owned titles
/// carry it …
pub const MIN_KEYWORD_TITLES: i64 = 3;
/// … and the share of the list's owned titles carrying it is at least this many times
/// the share of the library's titles of that kind carrying it.
pub const MIN_KEYWORD_LIFT: f64 = 3.0;
/// The most keywords a list's detail names, strongest lift first.
pub const MAX_KEYWORDS: usize = 20;

const KEYWORD_NAMESPACE: &str = "keywords";
const KEYWORD_KEY: &str = "keyword";

pub type Obj = Map<String, Value>;

const RESOLVED: &str = "
WITH resolved AS (
    SELECT e.collection_id, e.rank, e.ns, e.value, e.kind, e.title, e.year,
           COALESCE(
               (SELECT x.item_id FROM external_ids x
                 WHERE x.ns = e.ns AND x.value = e.value AND x.kind = e.kind),
               (SELECT m.item_id FROM collection_membership m
                 WHERE m.collection_id = e.collection_id AND m.rank = e.rank)
           ) AS item_id
    FROM collection_entry e
)
";

/// A movie is watched by a play of itself, a show by a play of any of its episodes.
const WATCHED: &str = "(EXISTS (SELECT 1 FROM plays p WHERE p.item_id = r.item_id) \
    OR EXISTS (SELECT 1 FROM items ep JOIN plays p ON p.item_id = ep.item_id \
    WHERE ep.show_item_id = r.item_id))";

/// The list's owner as the URL names it, or `None` for a URL that follows neither shape.
fn author(url: Option<&str>) -> Option<String> {
    let url = url.filter(|u| !u.is_empty())?;
    if let Some((user, _)) = letterboxd_list_parts(url) {
        return Some(user);
    }
    let path = urlparse(url)?.path;
    let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty()).collect();
    (parts.len() >= 3 && parts[0] == "lists").then(|| parts[1].to_string())
}

fn opt_text(value: Option<String>) -> Value {
    value.map_or(Value::Null, Value::String)
}

fn facts(row: &Row<'_>) -> rusqlite::Result<Obj> {
    let url: Option<String> = row.get("url")?;
    let mut out = Obj::new();
    out.insert(
        "collection_id".into(),
        row.get::<_, String>("collection_id")?.into(),
    );
    out.insert("source".into(), row.get::<_, String>("source")?.into());
    out.insert("author".into(), opt_text(author(url.as_deref())));
    out.insert("name".into(), opt_text(row.get("name")?));
    out.insert("url".into(), opt_text(url));
    out.insert(
        "provenance".into(),
        row.get::<_, String>("provenance")?.into(),
    );
    out.insert(
        "seed_count".into(),
        row.get::<_, Option<i64>>("seed_count")?.into(),
    );
    out.insert("entry_count".into(), row.get::<_, i64>("entries")?.into());
    out.insert("owned".into(), row.get::<_, i64>("owned")?.into());
    out.insert("size".into(), row.get::<_, Option<i64>>("size")?.into());
    out.insert("watched".into(), row.get::<_, i64>("watched")?.into());
    out.insert("likes".into(), row.get::<_, Option<i64>>("likes")?.into());
    out.insert("fetched_at".into(), opt_text(row.get("observed_at")?));
    Ok(out)
}

fn has_table(conn: &Connection, name: &str) -> rusqlite::Result<bool> {
    let found: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            [name],
            |row| row.get(0),
        )
        .ok();
    Ok(found.is_some())
}

/// The key a list's decisions and failed fetches are filed under: its canonical URL,
/// else its `collection_id`.
fn key(row: &Obj) -> String {
    row.get("url")
        .and_then(Value::as_str)
        .filter(|u| !u.is_empty())
        .and_then(canonical_list_url)
        .unwrap_or_else(|| {
            row.get("collection_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        })
}

struct DecisionRow {
    decision: String,
    decided_at: String,
    added: bool,
}

struct AttemptRow {
    attempts: i64,
    attempted_at: String,
    reason: String,
}

fn failure(row: Option<&AttemptRow>) -> Value {
    match row {
        None => Value::Null,
        Some(row) => json!({
            "attempts": row.attempts,
            "attempted_at": row.attempted_at,
            "reason": row.reason,
            "stopped": row.attempts >= MAX_FAILURES,
        }),
    }
}

fn decision_value(row: Option<&DecisionRow>) -> Value {
    row.map_or(
        Value::Null,
        |d| json!({"decision": d.decision, "decided_at": d.decided_at}),
    )
}

/// The table row of a list a person added that the store holds nothing of yet.
fn unfetched_row(key: &str, decision: Value, failure: Value) -> Obj {
    let mut out = Obj::new();
    out.insert("collection_id".into(), Value::Null);
    out.insert(
        "source".into(),
        list_source(key).unwrap_or("mdblist").into(),
    );
    out.insert("author".into(), opt_text(author(Some(key))));
    out.insert("name".into(), Value::Null);
    out.insert("url".into(), key.into());
    out.insert("provenance".into(), "added".into());
    out.insert("seed_count".into(), Value::Null);
    out.insert("entry_count".into(), 0.into());
    out.insert("owned".into(), 0.into());
    out.insert("size".into(), Value::Null);
    out.insert("watched".into(), 0.into());
    out.insert("likes".into(), Value::Null);
    out.insert("fetched_at".into(), Value::Null);
    let state = if failure.is_null() {
        "waiting"
    } else {
        "failed"
    };
    out.insert("decision".into(), decision);
    out.insert("failure".into(), failure);
    out.insert("state".into(), state.into());
    out
}

/// Add what a person decided about each list and how its fetches are going. With
/// `with_unfetched`, a list that was added or failed before it was ever stored gets a row
/// of its own, with no `collection_id`.
fn annotate(conn: &Connection, rows: &mut Vec<Obj>, with_unfetched: bool) -> rusqlite::Result<()> {
    let mut decisions: BTreeMap<String, DecisionRow> = BTreeMap::new();
    let mut attempts: BTreeMap<String, AttemptRow> = BTreeMap::new();
    if has_table(conn, "list_decision")? {
        let mut stmt =
            conn.prepare("SELECT url, decision, decided_at, added FROM list_decision")?;
        for row in stmt.query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                DecisionRow {
                    decision: r.get(1)?,
                    decided_at: r.get(2)?,
                    added: r.get::<_, i64>(3)? != 0,
                },
            ))
        })? {
            let (url, row) = row?;
            decisions.insert(url, row);
        }
    }
    if has_table(conn, "list_attempt")? {
        let mut stmt =
            conn.prepare("SELECT list_key, attempts, attempted_at, reason FROM list_attempt")?;
        for row in stmt.query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                AttemptRow {
                    attempts: r.get(1)?,
                    attempted_at: r.get(2)?,
                    reason: r.get(3)?,
                },
            ))
        })? {
            let (key, row) = row?;
            attempts.insert(key, row);
        }
    }
    let mut stored: BTreeSet<String> = BTreeSet::new();
    for row in rows.iter_mut() {
        let key = key(row);
        stored.insert(key.clone());
        let (decided, failed) = (decisions.get(&key), attempts.get(&key));
        row.insert("decision".into(), decision_value(decided));
        row.insert("failure".into(), failure(failed));
        row.insert(
            "state".into(),
            if failed.is_some() { "failed" } else { "ok" }.into(),
        );
    }
    if with_unfetched {
        let mut keys: BTreeSet<&String> = attempts.keys().collect();
        keys.extend(decisions.iter().filter(|(_, d)| d.added).map(|(u, _)| u));
        for key in keys.into_iter().filter(|k| !stored.contains(*k)) {
            let (decided, failed) = (decisions.get(key), attempts.get(key));
            if decided.is_some_and(|d| d.decision == "excluded" || d.decision == "removed") {
                continue;
            }
            rows.push(unfetched_row(key, decision_value(decided), failure(failed)));
        }
    }
    Ok(())
}

fn table_rows(
    conn: &Connection,
    source: Option<&str>,
    collection_id: Option<&str>,
    with_unfetched: bool,
) -> rusqlite::Result<Vec<Obj>> {
    let sql = format!(
        "{RESOLVED}SELECT c.collection_id, c.source, c.name, c.url, c.provenance, c.seed_count, \
         c.size, c.likes, c.observed_at, \
         COUNT(r.ns) AS entries, COUNT(DISTINCT r.item_id) AS owned, \
         COUNT(DISTINCT CASE WHEN {WATCHED} THEN r.item_id END) AS watched \
         FROM collection c LEFT JOIN resolved r ON r.collection_id = c.collection_id \
         WHERE (?1 IS NULL OR c.source = ?1) AND (?2 IS NULL OR c.collection_id = ?2) \
         GROUP BY c.collection_id ORDER BY c.source, c.likes DESC, c.collection_id"
    );
    let mut stmt = conn.prepare(&sql)?;
    let mut rows: Vec<Obj> = stmt
        .query_map(params![source, collection_id], facts)?
        .collect::<rusqlite::Result<_>>()?;
    annotate(conn, &mut rows, with_unfetched)?;
    Ok(rows)
}

/// One row per crowd list: where it came from, how it arrived, how much of it the library
/// owns and has watched, and what the source said about it. A list added or failing before
/// it was ever stored comes last, with no `collection_id`.
pub fn list_table(conn: &Connection, source: Option<&str>) -> rusqlite::Result<Vec<Obj>> {
    let rows = table_rows(conn, source, None, true)?;
    Ok(match source {
        None => rows,
        Some(source) => rows
            .into_iter()
            .filter(|row| row.get("source").and_then(Value::as_str) == Some(source))
            .collect(),
    })
}

/// Lay `list_decisions.json` over the table. Each row's `decision` becomes the file's entry
/// when there is one, and `pending` says in words what the next sweep will do about it, or is
/// null when it has nothing left to do. A list the file adds that the store knows nothing of
/// gets a row of its own (`lists.with_pending`).
pub fn with_pending(mut rows: Vec<Obj>, latest: &Latest) -> Vec<Obj> {
    let mut known: BTreeSet<String> = BTreeSet::new();
    for row in &mut rows {
        let key = key(row);
        if let Some(entry) = latest.get(std::slice::from_ref(&key)) {
            row.insert(
                "decision".into(),
                json!({"decision": entry.decision, "decided_at": entry.decided_at}),
            );
        }
        known.insert(key);
        let pending = pending(row);
        row.insert("pending".into(), pending);
    }
    for (key, entry) in &latest.entries {
        if known.contains(&key[0]) || entry.decision != "added" {
            continue;
        }
        let decision = json!({"decision": entry.decision, "decided_at": entry.decided_at});
        let mut row = unfetched_row(&key[0], decision, Value::Null);
        let pending = pending(&row);
        row.insert("pending".into(), pending);
        rows.push(row);
    }
    rows
}

fn pending(row: &Obj) -> Value {
    let Some(decided) = row.get("decision").filter(|d| !d.is_null()) else {
        return Value::Null;
    };
    let text =
        |value: &Value, name: &str| value.get(name).and_then(Value::as_str).map(String::from);
    let (Some(verdict), Some(when)) = (text(decided, "decision"), text(decided, "decided_at"))
    else {
        return Value::Null;
    };
    let failure = row.get("failure").filter(|f| !f.is_null());
    let waiting = match verdict.as_str() {
        "excluded" => return "Excluded · next sweep removes it".into(),
        "removed" => {
            let stored = !row["collection_id"].is_null() || failure.is_some();
            return if row["provenance"] == "added" && stored {
                "Removed · next sweep drops it".into()
            } else {
                Value::Null
            };
        }
        "added" => row["collection_id"].is_null(),
        "refetch" => row["fetched_at"]
            .as_str()
            .is_none_or(|fetched| is_before(fetched, &when)),
        "retry" => failure
            .and_then(|f| f.get("attempted_at").and_then(Value::as_str))
            .is_some_and(|attempted| is_before(attempted, &when)),
        _ => false,
    };
    if waiting {
        "Waiting for sweep".into()
    } else {
        Value::Null
    }
}

/// The `collection_id` a reference names: an id, or the list's URL with or without a
/// trailing slash.
pub fn find_list(conn: &Connection, reference: &str) -> rusqlite::Result<Option<String>> {
    let mut stmt = conn.prepare(
        "SELECT collection_id FROM collection WHERE collection_id = ?1 \
         OR rtrim(url, '/') = rtrim(?1, '/') LIMIT 1",
    )?;
    let mut rows = stmt.query([reference])?;
    rows.next()?.map(|row| row.get(0)).transpose()
}

/// One list's facts as [`list_table`] gives them, every entry in list order, and the
/// keywords its owned titles carry far more often than the library does. `None` where
/// the reference matches no list.
pub fn list_detail(conn: &Connection, reference: &str) -> rusqlite::Result<Option<Obj>> {
    let Some(collection_id) = find_list(conn, reference)? else {
        return Ok(None);
    };
    let Some(mut detail) = table_rows(conn, None, Some(&collection_id), false)?
        .into_iter()
        .next()
    else {
        return Ok(None);
    };
    let sql = format!(
        "{RESOLVED}SELECT r.rank, r.ns, r.value, r.kind, r.title AS source_title, \
         r.year AS source_year, r.item_id, i.title AS item_title, i.year AS item_year, \
         i.type AS item_type, {WATCHED} AS watched FROM resolved r \
         LEFT JOIN items i ON i.item_id = r.item_id \
         WHERE r.collection_id = ? ORDER BY r.rank IS NULL, r.rank, r.ns, r.value"
    );
    let mut stmt = conn.prepare(&sql)?;
    struct Entry {
        rank: Option<i64>,
        ns: String,
        value: String,
        kind: String,
        source_title: Option<String>,
        source_year: Option<i64>,
        item_id: Option<String>,
        item_title: Option<String>,
        item_year: Option<i64>,
        item_type: Option<String>,
        watched: bool,
    }
    let rows: Vec<Entry> = stmt
        .query_map([&collection_id], |r| {
            Ok(Entry {
                rank: r.get("rank")?,
                ns: r.get("ns")?,
                value: r.get("value")?,
                kind: r.get("kind")?,
                source_title: r.get("source_title")?,
                source_year: r.get("source_year")?,
                item_id: r.get("item_id")?,
                item_title: r.get("item_title")?,
                item_year: r.get("item_year")?,
                item_type: r.get("item_type")?,
                watched: r.get::<_, i64>("watched")? != 0,
            })
        })?
        .collect::<rusqlite::Result<_>>()?;
    let entries: Vec<Value> = rows
        .iter()
        .map(|row| {
            let owned = row.item_id.is_some();
            json!({
                "rank": row.rank,
                "owned": owned,
                "item_id": row.item_id,
                "title": if owned { &row.item_title } else { &row.source_title },
                "year": if owned { row.item_year } else { row.source_year },
                "ns": row.ns,
                "value": row.value,
                "kind": row.kind,
                "watched": row.watched,
            })
        })
        .collect();
    detail.insert("entries".into(), Value::Array(entries));
    let owned: BTreeMap<&str, Option<&str>> = rows
        .iter()
        .filter_map(|row| Some((row.item_id.as_deref()?, row.item_type.as_deref())))
        .collect();
    with_keywords(conn, &mut detail, &owned)?;
    Ok(Some(detail))
}

fn with_keywords(
    conn: &Connection,
    detail: &mut Obj,
    owned: &BTreeMap<&str, Option<&str>>,
) -> rusqlite::Result<()> {
    detail.insert("keywords".into(), Value::Array(Vec::new()));
    detail.insert("keyword_kind".into(), Value::Null);
    detail.insert("keyword_titles_in_list".into(), 0.into());
    detail.insert("keyword_titles_in_library".into(), 0.into());
    if owned.is_empty() {
        return Ok(());
    }
    let mut by_kind: BTreeMap<Option<&str>, i64> = BTreeMap::new();
    for kind in owned.values() {
        *by_kind.entry(*kind).or_default() += 1;
    }
    // The most common type, ties to the smaller name.
    let (kind, _) = by_kind
        .iter()
        .min_by_key(|(kind, count)| (-**count, **kind))
        .map(|(k, c)| (*k, *c))
        .unwrap_or((None, 0));
    let mut in_list_ids: Vec<&str> = owned
        .iter()
        .filter(|(_, item_type)| **item_type == kind)
        .map(|(id, _)| *id)
        .collect();
    in_list_ids.sort_unstable();
    let library_titles: i64 =
        conn.query_row("SELECT COUNT(*) FROM items WHERE type = ?", [kind], |r| {
            r.get(0)
        })?;
    detail.insert("keyword_kind".into(), kind.map(str::to_string).into());
    detail.insert("keyword_titles_in_list".into(), in_list_ids.len().into());
    detail.insert("keyword_titles_in_library".into(), library_titles.into());

    let mut counts: HashMap<String, i64> = HashMap::new();
    for chunk in in_list_ids.chunks(500) {
        let marks = vec!["?"; chunk.len()].join(", ");
        let sql = format!(
            "SELECT value, COUNT(DISTINCT item_id) FROM enrichment \
             WHERE namespace = ? AND key = ? AND item_id IN ({marks}) GROUP BY value"
        );
        let mut stmt = conn.prepare(&sql)?;
        let mut args: Vec<&str> = vec![KEYWORD_NAMESPACE, KEYWORD_KEY];
        args.extend(chunk);
        let rows = stmt.query_map(rusqlite::params_from_iter(args), |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
        })?;
        for row in rows {
            let (value, count) = row?;
            *counts.entry(value).or_default() += count;
        }
    }
    struct Found {
        keyword: String,
        in_list: i64,
        in_library: i64,
        lift: f64,
    }
    let mut found = Vec::new();
    for (keyword, in_list) in counts {
        if in_list < MIN_KEYWORD_TITLES {
            continue;
        }
        let in_library: i64 = conn.query_row(
            "SELECT COUNT(DISTINCT e.item_id) FROM enrichment e \
             JOIN items i ON i.item_id = e.item_id \
             WHERE e.namespace = ? AND e.key = ? AND e.value = ? AND i.type = ?",
            params![KEYWORD_NAMESPACE, KEYWORD_KEY, keyword, kind],
            |r| r.get(0),
        )?;
        let lift = (in_list as f64 / in_list_ids.len() as f64)
            / (in_library as f64 / library_titles as f64);
        if lift >= MIN_KEYWORD_LIFT {
            found.push(Found {
                keyword,
                in_list,
                in_library,
                lift,
            });
        }
    }
    found.sort_by(|a, b| {
        b.lift
            .total_cmp(&a.lift)
            .then(b.in_list.cmp(&a.in_list))
            .then(a.keyword.cmp(&b.keyword))
    });
    found.truncate(MAX_KEYWORDS);
    detail.insert(
        "keywords".into(),
        found
            .iter()
            .map(|f| {
                json!({
                    "keyword": f.keyword, "in_list": f.in_list,
                    "in_library": f.in_library, "lift": f.lift
                })
            })
            .collect(),
    );
    Ok(())
}
