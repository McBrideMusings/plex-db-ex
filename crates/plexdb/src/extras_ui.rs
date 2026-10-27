//! What Plex DB-EX's Extras tab and the Doctor's Files list read and write (ADR-0030).
//!
//! The reads answer from the store: the libraries and their counts, the queue of extras no
//! person has saved, and one extra with its files. The writes go only to
//! `extra_decisions.json`, through the same [`decisions::record`] the `plexdb extras` commands
//! use, so the next tick folds them like any other decision. Whether a save may organize an
//! extra is [`extras_organize::plan`]'s answer, asked with the values the form holds laid over
//! the file, so the page never re-derives a template or a required-field rule.

use std::collections::HashMap;
use std::path::Path;

use rusqlite::{Connection, OptionalExtension, params};

use crate::explore_views::ExploreError;
use crate::extras_decisions::{self as decisions, Decision, Entry, NAMESPACE, PERSON, SOURCE_URL};
use crate::extras_organize::{self, Action, Overlay};
use crate::extras_parsers;
use crate::identity::{canonical_path, fnv1a_64};
use crate::pyjson::Json;
use crate::staleness::{Now, parse_micros};

/// Most queue cards one `queue` call returns.
pub const MAX_QUEUE: usize = 500;
/// Most extras one `save` call may name.
pub const MAX_SAVES: usize = 200;
const MAX_TEXT: usize = 2000;
const MAX_TAGS: usize = 50;

type Declared = Vec<(String, String, bool)>;

fn declared_fields(conn: &Connection, library: &str) -> Result<Declared, ExploreError> {
    let mut stmt = conn.prepare_cached(
        "SELECT name, kind, required FROM extra_field WHERE library = ? ORDER BY rowid",
    )?;
    let rows = stmt
        .query_map([library], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get::<_, i64>(2)? == 1))
        })?
        .collect::<Result<_, _>>()?;
    Ok(rows)
}

fn fields_json(fields: &Declared) -> Json {
    Json::Array(
        fields
            .iter()
            .map(|(name, kind, required)| {
                Json::object([
                    ("name", Json::str(name)),
                    ("kind", Json::str(kind)),
                    ("required", Json::Bool(*required)),
                ])
            })
            .collect(),
    )
}

fn entries(path: &Path) -> Result<Vec<Entry>, ExploreError> {
    Ok(decisions::load(path)?.unwrap_or_default())
}

/// Each library with its counts, the moment of its latest scan and how often a tick runs.
pub fn status(conn: &Connection, every_minutes: u32) -> Result<Json, ExploreError> {
    let libraries: Vec<(String, String, String, Option<String>)> = conn
        .prepare("SELECT name, root, type, template FROM extra_library ORDER BY rowid")?
        .query_map([], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })?
        .collect::<Result<_, _>>()?;
    let mut out = Vec::new();
    for (name, root, kind, template) in libraries {
        let count = |sql: &str| -> Result<i64, ExploreError> {
            Ok(conn.query_row(sql, [&name], |row| row.get(0))?)
        };
        let organized = count(
            "SELECT COUNT(*) FROM extra_item WHERE library = ?1 AND organized = 1 \
             AND item_id NOT IN (SELECT merged_item_id FROM extra_merge)",
        )?;
        let queue = count(
            "SELECT COUNT(*) FROM extra_item WHERE library = ?1 AND organized = 0 \
             AND item_id NOT IN (SELECT merged_item_id FROM extra_merge)",
        )?;
        let files = count(
            "SELECT COUNT(*) FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
             WHERE i.library = ?1 AND f.missing_since IS NULL",
        )?;
        let seen: Vec<String> = conn
            .prepare(
                "SELECT f.last_seen FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
                 WHERE i.library = ?",
            )?
            .query_map([&name], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        let last_scan = seen
            .iter()
            .filter_map(|text| parse_micros(text).map(|at| (at, text)))
            .max_by_key(|(at, _)| *at)
            .map(|(_, text)| text.clone());
        let fields = declared_fields(conn, &name)?;
        out.push(Json::object([
            ("name", Json::str(&name)),
            ("type", Json::str(kind)),
            ("root", Json::str(root)),
            ("template", Json::opt_str(template)),
            ("fields", fields_json(&fields)),
            ("organized", Json::Int(organized)),
            ("queue", Json::Int(queue)),
            ("files", Json::Int(files)),
            ("last_scan", Json::opt_str(last_scan)),
        ]));
    }
    Ok(Json::object([
        ("libraries", Json::Array(out)),
        ("every_minutes", Json::Int(i64::from(every_minutes))),
    ]))
}

/// The ids of extras a person has saved and the tick has not organized yet, with when.
fn saving(conn: &Connection, all: &[Entry]) -> Result<HashMap<String, String>, ExploreError> {
    let mut out = HashMap::new();
    for item_id in extras_organize::pending(conn, all)? {
        if let Some(entry) = all
            .iter()
            .rev()
            .find(|e| matches!(&e.decision, Decision::Organize { item_id: id } if *id == item_id))
        {
            out.insert(item_id, entry.decided_at.clone());
        }
    }
    Ok(out)
}

/// A person's value for each (item, field) in the file, as the values a fold would store.
fn pending_sets(
    conn: &Connection,
    all: &[Entry],
) -> Result<HashMap<(String, String), Vec<String>>, ExploreError> {
    let mut out = HashMap::new();
    for entry in all {
        let Decision::Set {
            item_id,
            field,
            value,
        } = &entry.decision
        else {
            continue;
        };
        if field == SOURCE_URL {
            out.insert(
                (item_id.clone(), field.clone()),
                vec![value.trim().to_string()],
            );
            continue;
        }
        let Some(library) = decisions::library_of(conn, item_id)? else {
            continue;
        };
        let kind: Option<String> = conn
            .query_row(
                "SELECT kind FROM extra_field WHERE library = ? AND name = ?",
                [&library, field],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(kind) = kind
            && let Ok(values) = decisions::field_values(&kind, field, value)
        {
            out.insert((item_id.clone(), field.clone()), values);
        }
    }
    Ok(out)
}

fn source_rank(source: &str) -> (u8, &str) {
    match source {
        "person" => (0, ""),
        "path" => (2, ""),
        other => (1, other),
    }
}

/// A key's values for one extra: a person's, what another source proposed, and who that was.
type Layers = (Vec<String>, Vec<String>, Option<String>);

/// An extra's values for `key` as `(person values, proposed values, who proposed them)`: the
/// proposer is the best-ranked source that is not a person, scrapers ahead of the path parser.
fn layered(
    conn: &Connection,
    item_id: &str,
    namespace: &str,
    key: &str,
) -> Result<Layers, ExploreError> {
    let rows: Vec<(String, String)> = conn
        .prepare_cached(
            "SELECT value, source FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ? \
             ORDER BY rank, value",
        )?
        .query_map(params![item_id, namespace, key], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect::<Result<_, _>>()?;
    let person: Vec<String> = rows
        .iter()
        .filter(|(_, s)| s == PERSON)
        .map(|(v, _)| v.clone())
        .collect();
    let best = rows
        .iter()
        .filter(|(_, s)| s != PERSON)
        .map(|(_, s)| s.as_str())
        .min_by(|a, b| source_rank(a).cmp(&source_rank(b)));
    let proposed: Vec<String> = match best {
        Some(best) => rows
            .iter()
            .filter(|(_, s)| s == best)
            .map(|(v, _)| v.clone())
            .collect(),
        None => Vec::new(),
    };
    Ok((person, proposed, best.map(str::to_string)))
}

fn strings(values: &[String]) -> Json {
    Json::Array(values.iter().map(Json::str).collect())
}

fn relative(root: &str, path: &str) -> String {
    path.strip_prefix(root)
        .map_or(path, |rest| rest.trim_start_matches('/'))
        .to_string()
}

/// What organizing would do, as the card shows it.
fn plan_json(root: &str, plan: &extras_organize::Plan) -> Json {
    match &plan.action {
        Action::Move { to } => Json::object([
            ("status", Json::str("move")),
            ("to", Json::str(relative(root, to))),
        ]),
        Action::InPlace => Json::object([
            ("status", Json::str("in_place")),
            ("to", Json::str(relative(root, &plan.from))),
        ]),
        Action::DeleteDuplicate { to } => Json::object([
            ("status", Json::str("duplicate")),
            ("to", Json::str(relative(root, to))),
        ]),
        Action::Refused(why) => {
            Json::object([("status", Json::str("refused")), ("why", Json::str(why))])
        }
    }
}

struct FileRow {
    path: String,
    primary: bool,
    size: Option<i64>,
    height: Option<i64>,
    duration_ms: Option<i64>,
    sha256: Option<String>,
    missing: bool,
}

fn files_of(conn: &Connection, item_id: &str) -> Result<Vec<FileRow>, ExploreError> {
    let rows = conn
        .prepare_cached(
            "SELECT path, primary_file, size, height, duration_ms, sha256, missing_since \
             FROM extra_file WHERE item_id = ? ORDER BY primary_file DESC, path",
        )?
        .query_map([item_id], |row| {
            Ok(FileRow {
                path: row.get(0)?,
                primary: row.get::<_, i64>(1)? == 1,
                size: row.get(2)?,
                height: row.get(3)?,
                duration_ms: row.get(4)?,
                sha256: row.get(5)?,
                missing: row.get::<_, Option<String>>(6)?.is_some(),
            })
        })?
        .collect::<Result<_, _>>()?;
    Ok(rows)
}

fn opt_int(value: Option<i64>) -> Json {
    value.map_or(Json::Null, Json::Int)
}

/// The extras of `library` no person has saved, oldest first, `limit` from `offset`. Each card
/// carries the library's declared fields with the person's values (a save the tick has not
/// folded counts) and what a scraper or the filename parser proposed, the file's facts, and
/// what saving it would do now.
pub fn queue(
    conn: &Connection,
    decisions_path: &Path,
    library: &str,
    offset: usize,
    limit: usize,
) -> Result<Json, ExploreError> {
    let root_and_parsers: Option<(String, Option<String>)> = conn
        .query_row(
            "SELECT root, parsers FROM extra_library WHERE name = ?",
            [library],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let Some((root, parsers)) = root_and_parsers else {
        return Err(ExploreError::NotFound(format!(
            "no extras library is named {library}"
        )));
    };
    let rules = extras_parsers::from_json(parsers.as_deref())
        .map(|parsers| extras_parsers::compile(&parsers))
        .unwrap_or_default();
    let fields = declared_fields(conn, library)?;
    let all = entries(decisions_path)?;
    let sets = pending_sets(conn, &all)?;
    let saved = saving(conn, &all)?;
    let overlay = Overlay::of(conn, &all)?;
    let total: i64 = conn.query_row(
        "SELECT COUNT(*) FROM extra_item WHERE library = ? AND organized = 0 \
         AND item_id NOT IN (SELECT merged_item_id FROM extra_merge)",
        [library],
        |row| row.get(0),
    )?;
    let queued: Vec<(String, String, Option<String>, String)> = conn
        .prepare(
            "SELECT ei.item_id, i.title, ei.source_url, ei.first_seen FROM extra_item ei \
             JOIN items i ON i.item_id = ei.item_id \
             WHERE ei.library = ?1 AND ei.organized = 0 \
               AND ei.item_id NOT IN (SELECT merged_item_id FROM extra_merge) \
             ORDER BY ei.first_seen, ei.item_id LIMIT ?2 OFFSET ?3",
        )?
        .query_map(
            params![library, limit.min(MAX_QUEUE) as i64, offset as i64],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )?
        .collect::<Result<_, _>>()?;
    let mut cards = Vec::new();
    for (item_id, title, source_url, first_seen) in queued {
        let files = files_of(conn, &item_id)?;
        let primary = files.iter().find(|f| f.primary).or(files.first());
        let stem = primary
            .and_then(|f| Path::new(&f.path).file_stem())
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        let proposals = extras_parsers::propose(&rules, &stem);
        let mut field_cards = Vec::new();
        for (name, kind, required) in &fields {
            let (stored, proposed, by) = layered(conn, &item_id, NAMESPACE, name)?;
            let person = sets
                .get(&(item_id.clone(), name.clone()))
                .cloned()
                .unwrap_or(stored);
            let proposed = if person.is_empty() {
                proposed
            } else {
                Vec::new()
            };
            field_cards.push(Json::object([
                ("name", Json::str(name)),
                ("kind", Json::str(kind)),
                ("required", Json::Bool(*required)),
                ("value", strings(&person)),
                ("proposed", strings(&proposed)),
                (
                    "proposed_by",
                    Json::opt_str(by.filter(|_| !proposed.is_empty())),
                ),
            ]));
        }
        let url_set = sets
            .get(&(item_id.clone(), SOURCE_URL.to_string()))
            .and_then(|v| v.first().cloned());
        let url = url_set.clone().or(source_url);
        let url_proposed = url_set.is_none()
            && url.as_deref().is_some_and(|url| {
                proposals
                    .iter()
                    .any(|(key, value)| key == SOURCE_URL && value == url)
            });
        let (person_tags, proposed_tags, _) = layered(conn, &item_id, "keywords", "keyword")?;
        let mut tags = person_tags;
        for entry in &all {
            if let Decision::Tag { item_id: id, tag } = &entry.decision
                && *id == item_id
                && !tags.contains(tag)
            {
                tags.push(tag.clone());
            }
        }
        let plan = extras_organize::plan(conn, &item_id, &overlay)?;
        let saving_at = saved
            .get(&item_id)
            .filter(|_| !matches!(plan.action, Action::Refused(_)));
        let refused = match (&plan.action, saved.contains_key(&item_id)) {
            (Action::Refused(why), true) => Some(why.clone()),
            _ => None,
        };
        let proposer = field_cards
            .iter()
            .filter_map(|field| match field {
                Json::Object(pairs) => {
                    pairs
                        .iter()
                        .find(|(k, _)| k == "proposed_by")
                        .and_then(|(_, v)| match v {
                            Json::Str(text) if text != "path" => Some(text.clone()),
                            _ => None,
                        })
                }
                _ => None,
            })
            .next();
        cards.push(Json::object([
            ("item_id", Json::str(&item_id)),
            ("title", Json::str(title)),
            ("found_at", Json::str(first_seen)),
            ("file_count", Json::Int(files.len() as i64)),
            (
                "file",
                match primary {
                    Some(f) => Json::object([
                        ("path", Json::str(&f.path)),
                        ("size", opt_int(f.size)),
                        ("height", opt_int(f.height)),
                        ("duration_ms", opt_int(f.duration_ms)),
                        ("missing", Json::Bool(f.missing)),
                    ]),
                    None => Json::Null,
                },
            ),
            ("fields", Json::Array(field_cards)),
            ("source_url", Json::opt_str(url)),
            ("source_url_proposed", Json::Bool(url_proposed)),
            ("tags", strings(&tags)),
            ("proposed_tags", strings(&proposed_tags)),
            ("proposer", Json::opt_str(proposer)),
            ("plan", plan_json(&root, &plan)),
            (
                "saving",
                match saving_at {
                    Some(at) => Json::object([
                        ("decided_at", Json::str(at)),
                        ("to", plan_json(&root, &plan)),
                    ]),
                    None => Json::Null,
                },
            ),
            ("refused", Json::opt_str(refused)),
        ]));
    }
    Ok(Json::object([
        ("library", Json::str(library)),
        ("root", Json::str(root)),
        ("fields", fields_json(&fields)),
        ("total", Json::Int(total)),
        ("offset", Json::Int(offset as i64)),
        ("items", Json::Array(cards)),
    ]))
}

/// One extra the Doctor tab opens: its header, declared fields and files. `NotFound` when
/// `item_id` is not an extra, which tells the page to ask the tag doctor instead.
pub fn item(
    conn: &Connection,
    decisions_path: &Path,
    source_roots: &[String],
    item_id: &str,
) -> Result<Json, ExploreError> {
    type Head = (String, String, i64, Option<String>, String, Option<i64>);
    let head: Option<Head> = conn
        .query_row(
            "SELECT ei.library, i.title, ei.organized, ei.source_url, l.type, i.year \
             FROM extra_item ei JOIN items i ON i.item_id = ei.item_id \
             JOIN extra_library l ON l.name = ei.library WHERE ei.item_id = ?",
            [item_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .optional()?;
    let Some((library, title, organized, source_url, kind, year)) = head else {
        return Err(ExploreError::NotFound(format!("{item_id} is not an extra")));
    };
    let all = entries(decisions_path)?;
    let mut fields = Vec::new();
    for (name, field_kind, required) in declared_fields(conn, &library)? {
        let mut stmt = conn.prepare_cached(
            "SELECT value, source FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ?",
        )?;
        let mut values: Vec<(String, String)> = stmt
            .query_map(params![item_id, NAMESPACE, name], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })?
            .collect::<Result<_, _>>()?;
        values.sort_by(|a, b| (source_rank(&a.1), &a.0).cmp(&(source_rank(&b.1), &b.0)));
        fields.push(Json::object([
            ("name", Json::str(name)),
            ("kind", Json::str(field_kind)),
            ("required", Json::Bool(required)),
            (
                "values",
                Json::Array(
                    values
                        .into_iter()
                        .map(|(value, source)| {
                            Json::object([
                                ("value", Json::Str(value)),
                                ("source", Json::Str(source)),
                            ])
                        })
                        .collect(),
                ),
            ),
        ]));
    }
    let merges: Vec<(String, f64)> = conn
        .prepare("SELECT merged_item_id, score FROM extra_merge WHERE kept_item_id = ?")?
        .query_map([item_id], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    let files = files_of(conn, item_id)?;
    let primary_sha = files
        .iter()
        .find(|f| f.primary)
        .and_then(|f| f.sha256.clone());
    let mut rows = Vec::new();
    for file in &files {
        let origin = format!(
            "fs:{:016x}",
            fnv1a_64(&canonical_path(&file.path, source_roots))
        );
        let merge = merges.iter().find(|(id, _)| *id == origin);
        let pending = all.iter().rev().find_map(|entry| match &entry.decision {
            Decision::Delete { path } if *path == file.path => Some("delete"),
            Decision::Split { merged_item_id } if *merged_item_id == origin && merge.is_some() => {
                Some("split")
            }
            _ => None,
        });
        let identical = !file.primary && file.sha256.is_some() && file.sha256 == primary_sha;
        rows.push(Json::object([
            ("path", Json::str(&file.path)),
            ("primary", Json::Bool(file.primary)),
            ("size", opt_int(file.size)),
            ("height", opt_int(file.height)),
            ("duration_ms", opt_int(file.duration_ms)),
            ("sha256", Json::opt_str(file.sha256.clone())),
            ("missing", Json::Bool(file.missing)),
            ("identical", Json::Bool(identical)),
            ("score", merge.map_or(Json::Null, |(_, s)| Json::Float(*s))),
            ("splittable", Json::Bool(!file.primary && merge.is_some())),
            (
                "merged_item_id",
                Json::opt_str(merge.map(|(id, _)| id.clone())),
            ),
            ("pending", Json::opt_str(pending.map(str::to_string))),
        ]));
    }
    let (person_tags, proposed_tags, _) = layered(conn, item_id, "keywords", "keyword")?;
    Ok(Json::object([
        ("item_id", Json::str(item_id)),
        ("title", Json::str(title)),
        ("year", opt_int(year)),
        ("library", Json::str(library)),
        ("type", Json::str(kind)),
        ("organized", Json::Bool(organized == 1)),
        ("source_url", Json::opt_str(source_url)),
        ("fields", Json::Array(fields)),
        ("tags", strings(&person_tags)),
        ("proposed_tags", strings(&proposed_tags)),
        ("files", Json::Array(rows)),
    ]))
}

fn text_of(value: &serde_json::Value, what: &str) -> Result<String, ExploreError> {
    let text = value
        .as_str()
        .ok_or_else(|| ExploreError::Invalid(format!("{what} must be text")))?;
    if text.chars().count() > MAX_TEXT {
        return Err(ExploreError::Invalid(format!(
            "{what} is over {MAX_TEXT} characters"
        )));
    }
    Ok(text.to_string())
}

/// One extra's save as the page sent it.
struct Save {
    item_id: String,
    fields: Vec<(String, String)>,
    tags: Vec<String>,
}

fn parse_saves(
    body: &serde_json::Map<String, serde_json::Value>,
) -> Result<Vec<Save>, ExploreError> {
    let invalid = |m: &str| ExploreError::Invalid(m.to_string());
    let list = body
        .get("saves")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| invalid("saves must be a list"))?;
    if list.is_empty() || list.len() > MAX_SAVES {
        return Err(ExploreError::Invalid(format!(
            "saves must hold 1 to {MAX_SAVES} extras"
        )));
    }
    let mut saves = Vec::new();
    for save in list {
        let object = save
            .as_object()
            .ok_or_else(|| invalid("a save must be an object"))?;
        let item_id = text_of(
            object.get("item_id").unwrap_or(&serde_json::Value::Null),
            "item_id",
        )?;
        let fields = match object.get("fields") {
            None => Vec::new(),
            Some(serde_json::Value::Object(map)) => map
                .iter()
                .map(|(name, value)| Ok((name.clone(), text_of(value, name)?)))
                .collect::<Result<_, ExploreError>>()?,
            Some(_) => return Err(invalid("fields must be an object of text")),
        };
        let tags = match object.get("tags") {
            None => Vec::new(),
            Some(serde_json::Value::Array(list)) => {
                if list.len() > MAX_TAGS {
                    return Err(ExploreError::Invalid(format!(
                        "an extra takes at most {MAX_TAGS} tags"
                    )));
                }
                list.iter()
                    .map(|tag| text_of(tag, "a tag"))
                    .collect::<Result<_, _>>()?
            }
            Some(_) => return Err(invalid("tags must be a list of text")),
        };
        saves.push(Save {
            item_id,
            fields,
            tags,
        });
    }
    Ok(saves)
}

/// Save the queue cards in `body`: one `set` per field sent, one `tag` per new tag, and an
/// `organize`, recorded in `extra_decisions.json` only when every save is one the writer would
/// accept. With `dry_run` nothing is recorded and each save answers what it would do.
pub fn save(
    conn: &Connection,
    decisions_path: &Path,
    body: &serde_json::Map<String, serde_json::Value>,
) -> Result<Json, ExploreError> {
    let saves = parse_saves(body)?;
    let dry_run = match body.get("dry_run") {
        None => false,
        Some(serde_json::Value::Bool(flag)) => *flag,
        Some(_) => {
            return Err(ExploreError::Invalid(
                "dry_run must be true or false".into(),
            ));
        }
    };
    let now = Now::read().iso();
    let mut planned = entries(decisions_path)?;
    let mut fresh: Vec<Entry> = Vec::new();
    for save in &saves {
        let Some(library) = decisions::library_of(conn, &save.item_id)? else {
            return Err(ExploreError::NotFound(format!(
                "{} is not an extra in the store",
                save.item_id
            )));
        };
        let organized: i64 = conn.query_row(
            "SELECT organized FROM extra_item WHERE item_id = ?",
            [&save.item_id],
            |row| row.get(0),
        )?;
        let folded: bool = conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM extra_merge WHERE merged_item_id = ?)",
            [&save.item_id],
            |row| row.get(0),
        )?;
        if folded {
            return Err(ExploreError::Invalid(format!(
                "{} was folded into another extra",
                save.item_id
            )));
        }
        if organized == 1 {
            return Err(ExploreError::Invalid(format!(
                "{} is already organized",
                save.item_id
            )));
        }
        for (field, value) in &save.fields {
            if field != SOURCE_URL {
                let kind: Option<String> = conn
                    .query_row(
                        "SELECT kind FROM extra_field WHERE library = ? AND name = ?",
                        [&library, field],
                        |row| row.get(0),
                    )
                    .optional()?;
                let Some(kind) = kind else {
                    return Err(ExploreError::Invalid(format!(
                        "{library} declares no field {field}"
                    )));
                };
                decisions::field_values(&kind, field, value).map_err(ExploreError::Invalid)?;
            }
            fresh.push(Entry {
                decision: Decision::Set {
                    item_id: save.item_id.clone(),
                    field: field.clone(),
                    value: value.clone(),
                },
                decided_at: now.clone(),
            });
        }
        for tag in &save.tags {
            if tag.trim().is_empty() {
                continue;
            }
            fresh.push(Entry {
                decision: Decision::Tag {
                    item_id: save.item_id.clone(),
                    tag: tag.clone(),
                },
                decided_at: now.clone(),
            });
        }
    }
    planned.extend(fresh.iter().cloned());
    let overlay = Overlay::of(conn, &planned)?;
    let mut answers = Vec::new();
    for save in &saves {
        let root: String = conn.query_row(
            "SELECT l.root FROM extra_item i JOIN extra_library l ON l.name = i.library \
             WHERE i.item_id = ?",
            [&save.item_id],
            |row| row.get(0),
        )?;
        let plan = extras_organize::plan(conn, &save.item_id, &overlay)?;
        if let Action::Refused(why) = &plan.action
            && !dry_run
        {
            return Err(ExploreError::Invalid(format!(
                "{}: {why}; nothing was recorded",
                save.item_id
            )));
        }
        if !dry_run {
            fresh.push(Entry {
                decision: Decision::Organize {
                    item_id: save.item_id.clone(),
                },
                decided_at: now.clone(),
            });
        }
        answers.push(Json::object([
            ("item_id", Json::str(&save.item_id)),
            ("plan", plan_json(&root, &plan)),
        ]));
    }
    if !dry_run {
        for entry in &fresh {
            decisions::record(decisions_path, entry)?;
        }
    }
    Ok(Json::object([
        ("saved", Json::Array(answers)),
        ("recorded", Json::Bool(!dry_run)),
        ("decided_at", Json::str(now)),
        ("file", Json::str(decisions_path.display().to_string())),
    ]))
}

/// Record a Delete or a Split for one file of an extra the Doctor tab shows. Delete is refused
/// for a primary file, Split for any file that is not a near duplicate folded into the extra.
pub fn file_decision(
    conn: &Connection,
    decisions_path: &Path,
    source_roots: &[String],
    body: &serde_json::Map<String, serde_json::Value>,
) -> Result<Json, ExploreError> {
    let get = |name: &str| text_of(body.get(name).unwrap_or(&serde_json::Value::Null), name);
    let action = get("action")?;
    let item_id = get("item_id")?;
    let path = get("path")?;
    let primary: Option<i64> = conn
        .query_row(
            "SELECT primary_file FROM extra_file WHERE path = ? AND item_id = ?",
            [&path, &item_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(primary) = primary else {
        return Err(ExploreError::NotFound(format!(
            "{item_id} has no file at {path}"
        )));
    };
    if primary == 1 {
        return Err(ExploreError::Invalid(
            "the primary file is neither deleted nor split".into(),
        ));
    }
    let decision = match action.as_str() {
        "delete" => Decision::Delete { path: path.clone() },
        "split" => {
            let origin = format!("fs:{:016x}", fnv1a_64(&canonical_path(&path, source_roots)));
            let folded: bool = conn.query_row(
                "SELECT EXISTS (SELECT 1 FROM extra_merge WHERE merged_item_id = ?1 \
                 AND kept_item_id = ?2)",
                params![origin, item_id],
                |row| row.get(0),
            )?;
            if !folded {
                return Err(ExploreError::Invalid(format!(
                    "{path} was not folded into {item_id} as a near duplicate, so there is \
                     nothing to split"
                )));
            }
            Decision::Split {
                merged_item_id: origin,
            }
        }
        other => {
            return Err(ExploreError::Invalid(format!(
                "action must be delete or split, not {other}"
            )));
        }
    };
    let decided_at = Now::read().iso();
    decisions::record(
        decisions_path,
        &Entry {
            decision,
            decided_at: decided_at.clone(),
        },
    )?;
    Ok(Json::object([
        ("action", Json::str(action)),
        ("path", Json::str(path)),
        ("decided_at", Json::str(decided_at)),
        ("file", Json::str(decisions_path.display().to_string())),
    ]))
}

/// Ask the writer for a scan on its next poll: the tick loop watches `extra_decisions.json`'s
/// modification time, so rewriting the file as it is (or creating an empty list) is the request.
pub fn request_scan(decisions_path: &Path) -> Result<Json, ExploreError> {
    let body = match std::fs::read(decisions_path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => b"[]\n".to_vec(),
        Err(err) => {
            return Err(ExploreError::Internal(format!(
                "cannot read {}: {err}",
                decisions_path.display()
            )));
        }
    };
    let mut name = decisions_path
        .file_name()
        .unwrap_or_default()
        .to_os_string();
    name.push(format!(".{}.tmp", std::process::id()));
    let tmp = decisions_path.with_file_name(name);
    let written = std::fs::write(&tmp, body).and_then(|()| std::fs::rename(&tmp, decisions_path));
    if let Err(err) = written {
        let _ = std::fs::remove_file(&tmp);
        return Err(ExploreError::Internal(format!(
            "cannot write {}: {err}",
            decisions_path.display()
        )));
    }
    Ok(Json::object([
        ("requested_at", Json::str(Now::read().iso())),
        ("file", Json::str(decisions_path.display().to_string())),
    ]))
}
