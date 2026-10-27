//! `fold-extra-decisions`: a person's decisions about extras, from `extra_decisions.json`
//! into the store (ADR-0030).
//!
//! The file is a JSON list. Each entry has a `decision`, a `decided_at`, and the text
//! fields its decision names:
//!
//! | decision   | fields                         | folded as                                        |
//! | ---------- | ------------------------------ | ------------------------------------------------ |
//! | `set`      | `item_id`, `field`, `value`    | a `person` row in `enrichment` namespace `extra` |
//! | `tag`      | `item_id`, `tag`               | a `person` keyword through `keyword_surfaces`    |
//! | `organize` | `item_id`                      | acted on by `extras-organize`, not here          |
//! | `delete`   | `path`                         | the file and its row, never a primary file       |
//! | `split`    | `merged_item_id`               | the merged item's file gets its own item back    |
//!
//! The newest entry for the same subject (item and field, item and tag, item, path,
//! merged item) replaces the older, so the file holds one entry per subject. A fold
//! applies the whole file every time and each entry is idempotent: a re-run changes no
//! row. The whole file is checked before the first row changes; an entry that names
//! nothing the store has, or that the store's declaration refuses, is counted and
//! reported and the rest are applied.

use std::path::Path;

use plexdb_keywords::{RawKeyword, write_title_keywords};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::{Map, Value};

use crate::decisions::{DecisionsFileError, FoldError, thousands};
use crate::extras_walk::{self, ITEM_TYPE};
use crate::identity::{canonical_path, fnv1a_64};
use crate::pyiso::{is_isoformat, isoformat_micros};

pub const NAME: &str = "fold-extra-decisions";
pub const FILE: &str = "extra_decisions.json";

/// The `enrichment` namespace an extra's field values live in.
pub const NAMESPACE: &str = "extra";
/// Who said it, for a value a person typed.
pub const PERSON: &str = "person";
/// The implicit field that lives on `extra_item`, not in `enrichment`.
pub const SOURCE_URL: &str = "source_url";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    Set {
        item_id: String,
        field: String,
        value: String,
    },
    Tag {
        item_id: String,
        tag: String,
    },
    Organize {
        item_id: String,
    },
    Delete {
        path: String,
    },
    Split {
        merged_item_id: String,
    },
}

impl Decision {
    fn name(&self) -> &'static str {
        match self {
            Decision::Set { .. } => "set",
            Decision::Tag { .. } => "tag",
            Decision::Organize { .. } => "organize",
            Decision::Delete { .. } => "delete",
            Decision::Split { .. } => "split",
        }
    }

    /// What the entry is about; a newer entry with the same subject replaces an older one.
    fn subject(&self) -> Vec<&str> {
        match self {
            Decision::Set { item_id, field, .. } => vec![self.name(), item_id, field],
            Decision::Tag { item_id, tag } => vec![self.name(), item_id, tag],
            Decision::Organize { item_id } => vec![self.name(), item_id],
            Decision::Delete { path } => vec![self.name(), path],
            Decision::Split { merged_item_id } => vec![self.name(), merged_item_id],
        }
    }

    fn fields(&self) -> Vec<(&'static str, &str)> {
        match self {
            Decision::Set {
                item_id,
                field,
                value,
            } => vec![("item_id", item_id), ("field", field), ("value", value)],
            Decision::Tag { item_id, tag } => vec![("item_id", item_id), ("tag", tag)],
            Decision::Organize { item_id } => vec![("item_id", item_id)],
            Decision::Delete { path } => vec![("path", path)],
            Decision::Split { merged_item_id } => vec![("merged_item_id", merged_item_id)],
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub decision: Decision,
    pub decided_at: String,
}

/// What a fold did, for its report.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct FoldStats {
    pub file_found: bool,
    pub entries: usize,
    pub applied: usize,
    /// Entries naming something the store has no row for.
    pub unmatched: usize,
    /// Entries the store's declaration or the filesystem refused, each with why.
    pub refused: Vec<String>,
    /// Entries left for `extras-organize`.
    pub organize: usize,
}

fn text(entry: &Value, field: &str) -> Option<String> {
    entry.get(field).and_then(Value::as_str).map(str::to_string)
}

/// The entries in `text`, or the first way one breaks the shape.
pub fn parse(path: &Path, text_in: &str) -> Result<Vec<Entry>, DecisionsFileError> {
    let path = path.display();
    let raw: Value = serde_json::from_str(text_in)
        .map_err(|err| DecisionsFileError(format!("{path} is not valid JSON: {err}")))?;
    let Value::Array(raw) = raw else {
        return Err(DecisionsFileError(format!(
            "{path} does not hold a list of decisions"
        )));
    };
    let mut entries = Vec::with_capacity(raw.len());
    for (index, entry) in raw.iter().enumerate() {
        let at = format!("{path} entry {index}");
        if !entry.is_object() {
            return Err(DecisionsFileError(format!("{at} is not an object")));
        }
        let need = |names: &[&str]| -> Result<Vec<String>, DecisionsFileError> {
            names
                .iter()
                .map(|name| text(entry, name))
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| DecisionsFileError(format!("{at} needs text {}", names.join(", "))))
        };
        let decision = text(entry, "decision")
            .ok_or_else(|| DecisionsFileError(format!("{at} needs text decision")))?;
        let decision = match decision.as_str() {
            "set" => {
                let [item_id, field, value] = need(&["item_id", "field", "value"])?
                    .try_into()
                    .expect("three names");
                Decision::Set {
                    item_id,
                    field,
                    value,
                }
            }
            "tag" => {
                let [item_id, tag] = need(&["item_id", "tag"])?.try_into().expect("two names");
                Decision::Tag { item_id, tag }
            }
            "organize" => Decision::Organize {
                item_id: need(&["item_id"])?.remove(0),
            },
            "delete" => Decision::Delete {
                path: need(&["path"])?.remove(0),
            },
            "split" => Decision::Split {
                merged_item_id: need(&["merged_item_id"])?.remove(0),
            },
            other => {
                return Err(DecisionsFileError(format!(
                    "{at} has decision '{other}', expected one of set, tag, organize, delete, split"
                )));
            }
        };
        let decided_at = need(&["decided_at"])?.remove(0);
        if !is_isoformat(&decided_at) {
            return Err(DecisionsFileError(format!(
                "{at} has decided_at '{decided_at}', expected an ISO 8601 time"
            )));
        }
        entries.push(Entry {
            decision,
            decided_at,
        });
    }
    Ok(entries)
}

/// The entries in `path`, or `None` when there is no file.
pub fn load(path: &Path) -> Result<Option<Vec<Entry>>, DecisionsFileError> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => {
            return Err(DecisionsFileError(format!(
                "cannot read {}: {err}",
                path.display()
            )));
        }
    };
    let body = String::from_utf8(bytes).map_err(|err| {
        DecisionsFileError(format!("{} is not UTF-8 text: {err}", path.display()))
    })?;
    parse(path, &body).map(Some)
}

fn to_value(entry: &Entry) -> Value {
    let mut object = Map::new();
    object.insert("decision".into(), entry.decision.name().into());
    for (name, value) in entry.decision.fields() {
        object.insert(name.into(), value.into());
    }
    object.insert("decided_at".into(), entry.decided_at.clone().into());
    Value::Object(object)
}

/// Append `entry` to the file at `path`, dropping every earlier entry with the same subject.
/// The file is replaced by rename, so a reader never sees half of it.
pub fn record(path: &Path, entry: &Entry) -> Result<(), DecisionsFileError> {
    let mut kept: Vec<Value> = load(path)?
        .unwrap_or_default()
        .iter()
        .filter(|old| old.decision.subject() != entry.decision.subject())
        .map(to_value)
        .collect();
    kept.push(to_value(entry));
    let mut body = serde_json::to_string_pretty(&Value::Array(kept)).expect("json");
    body.push('\n');
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".{}.tmp", std::process::id()));
    let tmp = path.with_file_name(name);
    let written = std::fs::write(&tmp, body).and_then(|()| std::fs::rename(&tmp, path));
    if written.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    written.map_err(|err| DecisionsFileError(format!("cannot write {}: {err}", path.display())))
}

/// The library an extra belongs to.
pub fn library_of(conn: &Connection, item_id: &str) -> rusqlite::Result<Option<String>> {
    conn.query_row(
        "SELECT library FROM extra_item WHERE item_id = ?",
        [item_id],
        |row| row.get(0),
    )
    .optional()
}

/// The values a `set` stores for `value` under a field of `kind`, or why it is refused.
/// An empty value clears the field.
pub fn field_values(kind: &str, field: &str, value: &str) -> Result<Vec<String>, String> {
    let value = value.trim();
    if value.is_empty() {
        return Ok(Vec::new());
    }
    match kind {
        "text" => Ok(vec![value.to_string()]),
        "int" => value
            .parse::<i64>()
            .map(|n| vec![n.to_string()])
            .map_err(|_| format!("field {field} is an int, got '{value}'")),
        "year" => match value.parse::<i64>() {
            Ok(n) if (1000..=9999).contains(&n) && value.len() == 4 => Ok(vec![n.to_string()]),
            _ => Err(format!("field {field} is a year, got '{value}'")),
        },
        _ => Ok(value
            .split(';')
            .map(str::trim)
            .filter(|part| !part.is_empty())
            .map(str::to_string)
            .collect()),
    }
}

/// The kind of `field` in `library`, if declared.
fn kind_of(conn: &Connection, library: &str, field: &str) -> rusqlite::Result<Option<String>> {
    conn.query_row(
        "SELECT kind FROM extra_field WHERE library = ? AND name = ?",
        params![library, field],
        |row| row.get(0),
    )
    .optional()
}

enum Outcome {
    Applied,
    Unmatched,
    Refused(String),
}

fn apply_set(
    tx: &Connection,
    item_id: &str,
    field: &str,
    value: &str,
    decided_at: &str,
) -> Result<Outcome, FoldError> {
    let Some(library) = library_of(tx, item_id)? else {
        return Ok(Outcome::Unmatched);
    };
    if field == SOURCE_URL {
        let url = value.trim();
        tx.execute(
            "UPDATE extra_item SET source_url = ? WHERE item_id = ?",
            params![(!url.is_empty()).then_some(url), item_id],
        )?;
        return Ok(Outcome::Applied);
    }
    let Some(kind) = kind_of(tx, &library, field)? else {
        return Ok(Outcome::Refused(format!(
            "set {field} on {item_id}: {library} declares no field {field}"
        )));
    };
    let values = match field_values(&kind, field, value) {
        Ok(values) => values,
        Err(why) => return Ok(Outcome::Refused(format!("set on {item_id}: {why}"))),
    };
    tx.execute(
        "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ? AND source = ?",
        params![item_id, NAMESPACE, field, PERSON],
    )?;
    for (rank, value) in values.iter().enumerate() {
        tx.execute(
            "INSERT OR REPLACE INTO enrichment \
             (item_id, namespace, source, key, value, fetched_at, rank) \
             VALUES (?, ?, ?, ?, ?, ?, ?)",
            params![
                item_id,
                NAMESPACE,
                PERSON,
                field,
                value,
                decided_at,
                rank as i64
            ],
        )?;
    }
    if field == "title"
        && let Some(title) = values.first()
    {
        tx.execute(
            "UPDATE items SET title = ? WHERE item_id = ?",
            params![title, item_id],
        )?;
    }
    Ok(Outcome::Applied)
}

fn apply_tag(
    tx: &Connection,
    item_id: &str,
    tag: &str,
    decided_at: &str,
) -> Result<Outcome, FoldError> {
    if library_of(tx, item_id)?.is_none() {
        return Ok(Outcome::Unmatched);
    }
    let tag = tag.trim();
    if tag.is_empty() {
        return Ok(Outcome::Refused(format!(
            "tag on {item_id}: the tag is empty"
        )));
    }
    let mut surfaces: Vec<String> = {
        let mut stmt = tx.prepare(
            "SELECT surface FROM keyword_surfaces WHERE item_id = ? AND source = ? ORDER BY surface",
        )?;
        stmt.query_map(params![item_id, PERSON], |row| row.get(0))?
            .collect::<Result<_, _>>()?
    };
    if !surfaces.iter().any(|surface| surface == tag) {
        surfaces.push(tag.to_string());
        write_title_keywords(
            tx,
            item_id,
            PERSON,
            surfaces.into_iter().map(RawKeyword::plain),
            decided_at,
        )?;
    }
    Ok(Outcome::Applied)
}

fn apply_delete(tx: &Connection, path: &str, decided_at: &str) -> Result<Outcome, FoldError> {
    let row: Option<(i64, String, String)> = tx
        .query_row(
            "SELECT f.primary_file, l.root, f.first_seen FROM extra_file f \
             JOIN extra_item i ON i.item_id = f.item_id \
             JOIN extra_library l ON l.name = i.library WHERE f.path = ?",
            [path],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;
    let Some((primary, root, first_seen)) = row else {
        return Ok(Outcome::Unmatched);
    };
    // The decision stays in the file and is read every tick: it names the file that was at
    // this path when it was made, never one the walk found there later.
    let later = match (isoformat_micros(&first_seen), isoformat_micros(decided_at)) {
        (Some(seen), Some(decided)) => seen > decided,
        _ => false,
    };
    if later {
        return Ok(Outcome::Unmatched);
    }
    if primary == 1 {
        return Ok(Outcome::Refused(format!(
            "delete {path}: it is the primary file of its item"
        )));
    }
    if !crate::extras_declaration::is_under(path, &root) {
        return Ok(Outcome::Refused(format!(
            "delete {path}: it is not under its library root {root}"
        )));
    }
    match std::fs::remove_file(path) {
        Ok(()) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => return Ok(Outcome::Refused(format!("delete {path}: {err}"))),
    }
    tx.execute("DELETE FROM extra_file WHERE path = ?", [path])?;
    Ok(Outcome::Applied)
}

fn apply_split(
    tx: &Connection,
    merged_item_id: &str,
    source_roots: &[String],
    now: &str,
) -> Result<Outcome, FoldError> {
    let kept: Option<String> = tx
        .query_row(
            "SELECT kept_item_id FROM extra_merge WHERE merged_item_id = ?",
            [merged_item_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(kept) = kept else {
        return Ok(Outcome::Unmatched);
    };
    let files: Vec<String> = {
        let mut stmt = tx.prepare("SELECT path FROM extra_file WHERE item_id = ? ORDER BY path")?;
        stmt.query_map([&kept], |row| row.get(0))?
            .collect::<Result<_, _>>()?
    };
    let Some(path) = files.iter().find(|path| {
        format!("fs:{:016x}", fnv1a_64(&canonical_path(path, source_roots))) == merged_item_id
    }) else {
        return Ok(Outcome::Refused(format!(
            "split {merged_item_id}: no file of {kept} has a path that hashes to it"
        )));
    };
    let (library, organized): (String, i64) = tx.query_row(
        "SELECT library, organized FROM extra_item WHERE item_id = ?",
        [&kept],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    let stem = std::path::Path::new(path)
        .file_stem()
        .map_or_else(String::new, |stem| stem.to_string_lossy().into_owned());
    tx.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
        params![merged_item_id, ITEM_TYPE, stem],
    )?;
    tx.execute(
        "INSERT INTO extra_item (item_id, library, organized, first_seen) VALUES (?, ?, ?, ?)",
        params![merged_item_id, library, organized, now],
    )?;
    tx.execute(
        "UPDATE extra_file SET item_id = ?, primary_file = 1 WHERE path = ?",
        params![merged_item_id, path],
    )?;
    tx.execute(
        "DELETE FROM extra_merge WHERE merged_item_id = ?",
        [merged_item_id],
    )?;
    // Kept apart by a person: `extras-near` never folds this item again.
    tx.execute(
        "INSERT OR REPLACE INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
         VALUES (?, ?, ?, ?, ?)",
        params![
            merged_item_id,
            crate::extras_near::CURSOR_NAMESPACE,
            crate::extras_near::CURSOR_SOURCE,
            crate::extras_near::SPLIT,
            now
        ],
    )?;
    extras_walk::choose_primaries(tx, &library)?;
    Ok(Outcome::Applied)
}

/// Apply `path` to the store; a missing file is no decisions.
pub fn fold(
    conn: &mut Connection,
    path: &Path,
    source_roots: &[String],
    now: &str,
) -> Result<FoldStats, FoldError> {
    let mut stats = FoldStats::default();
    let Some(entries) = load(path)? else {
        return Ok(stats);
    };
    stats.file_found = true;
    stats.entries = entries.len();
    for entry in &entries {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let outcome = match &entry.decision {
            Decision::Set {
                item_id,
                field,
                value,
            } => apply_set(&tx, item_id, field, value, &entry.decided_at)?,
            Decision::Tag { item_id, tag } => apply_tag(&tx, item_id, tag, &entry.decided_at)?,
            Decision::Organize { .. } => {
                stats.organize += 1;
                continue;
            }
            Decision::Delete { path } => apply_delete(&tx, path, &entry.decided_at)?,
            Decision::Split { merged_item_id } => {
                apply_split(&tx, merged_item_id, source_roots, now)?
            }
        };
        tx.commit()?;
        match outcome {
            Outcome::Applied => stats.applied += 1,
            Outcome::Unmatched => stats.unmatched += 1,
            Outcome::Refused(why) => stats.refused.push(why),
        }
    }
    Ok(stats)
}

pub fn describe(stats: &FoldStats, path: &Path) -> Vec<String> {
    let path = path.display();
    if !stats.file_found {
        return vec![format!("extra decisions: no file at {path}")];
    }
    let mut lines = vec![format!(
        "extra decisions: {} entr(ies) in {path}, {} applied, {} named nothing the store has, \
         {} refused, {} organize left for extras-organize",
        thousands(stats.entries),
        thousands(stats.applied),
        thousands(stats.unmatched),
        thousands(stats.refused.len()),
        thousands(stats.organize),
    )];
    lines.extend(stats.refused.iter().map(|why| format!("  refused: {why}")));
    lines
}
