//! The two read-only routes consumers call for extras (ADR-0030): `GET /api/extras/items` and
//! `POST /api/extras/fields`. Both answer only items with `organized = 1`; an extra still in
//! the queue is never listed and its id is never answered.

use rusqlite::{Connection, params};

use crate::explore_views::ExploreError;
use crate::pyjson::Json;
use crate::staleness::parse_micros;

/// Most ids one `fields` call may ask for.
pub const MAX_IDS: usize = 1000;

/// Every moment something about an extra changed, joined by `|`: its first sighting, its
/// organizing, its primary file going missing, each file joining it, and each field or tag
/// value written for it. The writer stamps in the local offset and other writers in UTC, so the
/// latest is picked by instant in [`changed_at`], never by text.
/// An extra folded into another by the near-duplicate pass is that item's second file: it is
/// never listed or answered as an item of its own.
const LIVE: &str = "NOT EXISTS (SELECT 1 FROM extra_merge m WHERE m.merged_item_id = ei.item_id)";

const MOMENTS: &str = "ei.first_seen || '|' || COALESCE(ei.organized_at, '') || '|' || \
    COALESCE(pf.missing_since, '') || '|' || \
    COALESCE((SELECT group_concat(first_seen, '|') FROM extra_file f \
              WHERE f.item_id = ei.item_id), '') || '|' || \
    COALESCE((SELECT group_concat(merged_at, '|') FROM extra_merge m \
              WHERE m.kept_item_id = ei.item_id), '') || '|' || \
    COALESCE((SELECT group_concat(fetched_at, '|') FROM enrichment e \
              WHERE e.item_id = ei.item_id AND e.namespace IN ('extra', 'keywords')), '')";

/// The latest of `moments` as the writer spelled it, with its instant.
fn changed_at(moments: &str) -> Option<(i64, &str)> {
    moments
        .split('|')
        .filter_map(|text| parse_micros(text).map(|at| (at, text)))
        .max_by_key(|(at, _)| *at)
}

/// Organized extras changed after `since` (a cursor an earlier answer returned), or every
/// organized extra without it, oldest change first. `cursor` is the latest change returned, or
/// `since` again when nothing is newer. A change stamped before a cursor but committed after the
/// reader fetched it is not seen again: the writer stamps a tick once, before it commits.
pub fn extras_items(conn: &Connection, since: Option<&str>) -> Result<Json, ExploreError> {
    let floor = match since {
        None => None,
        Some(text) => Some(parse_micros(text).ok_or_else(|| {
            ExploreError::Invalid(format!(
                "since must be a cursor an earlier answer returned, not {text:?}"
            ))
        })?),
    };
    let sql = format!(
        "SELECT ei.item_id, ei.library, l.type, i.title, i.year, pf.duration_ms, pf.path, \
                pf.missing_since, ei.source_url, {MOMENTS} \
         FROM extra_item ei \
         JOIN items i ON i.item_id = ei.item_id \
         JOIN extra_library l ON l.name = ei.library \
         LEFT JOIN extra_file pf ON pf.item_id = ei.item_id AND pf.primary_file = 1 \
         WHERE ei.organized = 1 AND {LIVE}"
    );
    let mut found: Vec<(i64, String, Json)> = Vec::new();
    let mut statement = conn.prepare(&sql)?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let moments: String = row.get(9)?;
        let Some((at, stamp)) = changed_at(&moments) else {
            continue;
        };
        if floor.is_some_and(|floor| at <= floor) {
            continue;
        }
        let id: String = row.get(0)?;
        let path: Option<String> = row.get(6)?;
        let missing_since: Option<String> = row.get(7)?;
        let item = Json::object([
            ("item_id", Json::Str(id.clone())),
            ("library", Json::Str(row.get(1)?)),
            ("type", Json::Str(row.get(2)?)),
            ("title", Json::Str(row.get(3)?)),
            (
                "year",
                row.get::<_, Option<i64>>(4)?.map_or(Json::Null, Json::Int),
            ),
            (
                "duration_ms",
                row.get::<_, Option<i64>>(5)?.map_or(Json::Null, Json::Int),
            ),
            ("path", Json::opt_str(path.clone())),
            (
                "missing",
                Json::Bool(path.is_none() || missing_since.is_some()),
            ),
            ("source_url", Json::opt_str(row.get(8)?)),
            ("changed_at", Json::str(stamp)),
        ]);
        found.push((at, id, item));
    }
    found.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    let cursor = match found.last() {
        Some((_, _, Json::Object(pairs))) => pairs
            .iter()
            .find(|(name, _)| name == "changed_at")
            .and_then(|(_, value)| match value {
                Json::Str(text) => Some(text.clone()),
                _ => None,
            }),
        _ => since.map(str::to_string),
    };
    Ok(Json::object([
        (
            "items",
            Json::Array(found.into_iter().map(|f| f.2).collect()),
        ),
        ("cursor", Json::opt_str(cursor)),
    ]))
}

/// Where a value sits in an answer: a person's first, then each scraper by name, then the
/// filename parser's.
fn source_rank(source: &str) -> (u8, String) {
    match source {
        "person" => (0, String::new()),
        "path" => (2, String::new()),
        other => (1, other.to_string()),
    }
}

fn values(
    conn: &Connection,
    item_id: &str,
    namespace: &str,
    key: &str,
) -> Result<Json, ExploreError> {
    let mut statement = conn.prepare_cached(
        "SELECT value, source FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ?",
    )?;
    let mut found: Vec<(String, String)> = statement
        .query_map(params![item_id, namespace, key], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect::<Result<_, _>>()?;
    found.sort_by(|a, b| (source_rank(&a.1), &a.0).cmp(&(source_rank(&b.1), &b.0)));
    Ok(Json::Array(
        found
            .into_iter()
            .map(|(value, source)| {
                Json::object([("value", Json::Str(value)), ("source", Json::Str(source))])
            })
            .collect(),
    ))
}

/// Each organized id's declared fields and tags, values ordered person, scraper, path. An id
/// that is not an organized extra is left out of `items`.
pub fn extras_fields(conn: &Connection, ids: &[String]) -> Result<Json, ExploreError> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for id in ids {
        if !seen.insert(id) {
            continue;
        }
        let library: Option<String> = conn
            .query_row(
                "SELECT library FROM extra_item ei WHERE item_id = ? AND organized = 1 AND NOT EXISTS \
                 (SELECT 1 FROM extra_merge m WHERE m.merged_item_id = ei.item_id)",
                [id],
                |row| row.get(0),
            )
            .map(Some)
            .or_else(|err| match err {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        let Some(library) = library else { continue };
        let mut declared = conn.prepare_cached(
            "SELECT name, kind, required FROM extra_field WHERE library = ? ORDER BY name",
        )?;
        let declared: Vec<(String, String, i64)> = declared
            .query_map([&library], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })?
            .collect::<Result<_, _>>()?;
        let mut fields = Vec::new();
        for (name, kind, required) in declared {
            fields.push(Json::object([
                ("values", values(conn, id, "extra", &name)?),
                ("name", Json::Str(name)),
                ("kind", Json::Str(kind)),
                ("required", Json::Bool(required == 1)),
            ]));
        }
        out.push((
            id.clone(),
            Json::object([
                ("library", Json::Str(library)),
                ("fields", Json::Array(fields)),
                ("tags", values(conn, id, "keywords", "keyword")?),
            ]),
        ));
    }
    Ok(Json::object([("items", Json::Object(out))]))
}
