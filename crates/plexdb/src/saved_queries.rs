//! The Query tab's saved queries (`SavedQueries` in `plexdb/explore.py`): named queries in one
//! JSON file beside the store, never inside it. The store has one writer (ADR-0001), so what
//! the Query tab saves lives in a file of its own, replaced by rename so a reader never sees
//! half of it. Plex DB-EX has no login, so [`upsert`] bounds how far an anonymous caller can
//! grow the file.

use std::fs;
use std::path::Path;

use serde_json::Value;

use plexdb_store::py_strip;

use crate::decisions::py_repr;
use crate::explore_views::ExploreError;
use crate::pyjson::{Json, dumps_indent2};

pub const FILE: &str = "explore-queries.json";

const NAME_MAX: usize = 200;
const MAX_ENTRIES: usize = 500;
const MAX_TOTAL_BYTES: usize = 1 << 20;

/// One saved query: its name, its SQL and a note.
#[derive(Debug, Clone, PartialEq)]
pub struct Saved {
    pub name: String,
    pub sql: String,
    pub note: String,
}

fn broken(message: String) -> ExploreError {
    ExploreError::Internal(format!("saved queries: {message}"))
}

fn read(path: &Path) -> Result<Vec<Saved>, ExploreError> {
    let body = match fs::read(path) {
        Ok(body) => body,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(err) => return Err(broken(err.to_string())),
    };
    let raw: Value = std::str::from_utf8(&body)
        .map_err(|err| err.to_string())
        .and_then(|body| serde_json::from_str(body).map_err(|err| err.to_string()))
        .map_err(|err| broken(format!("{} is not valid JSON: {err}", path.display())))?;
    let shape = || {
        broken(format!(
            "{} does not hold a list of name, sql and note entries",
            path.display()
        ))
    };
    let field =
        |entry: &Value, name: &str| entry.get(name).and_then(Value::as_str).map(str::to_string);
    raw.as_array()
        .ok_or_else(shape)?
        .iter()
        .map(|entry| {
            Ok(Saved {
                name: field(entry, "name").ok_or_else(shape)?,
                sql: field(entry, "sql").ok_or_else(shape)?,
                note: field(entry, "note").ok_or_else(shape)?,
            })
        })
        .collect()
}

fn body(queries: &[Saved]) -> String {
    format!("{}\n", dumps_indent2(&json(queries)))
}

fn write(path: &Path, queries: &[Saved]) -> Result<(), ExploreError> {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    let tmp = path.with_file_name(name);
    let written = fs::write(&tmp, body(queries)).and_then(|()| fs::rename(&tmp, path));
    if let Err(err) = written {
        let _ = fs::remove_file(&tmp);
        return Err(broken(err.to_string()));
    }
    Ok(())
}

/// The queries as the file and `/api/saved` hold them: `[{name, sql, note}]`.
pub fn json(queries: &[Saved]) -> Json {
    Json::Array(
        queries
            .iter()
            .map(|q| {
                Json::object([
                    ("name", Json::str(&q.name)),
                    ("sql", Json::str(&q.sql)),
                    ("note", Json::str(&q.note)),
                ])
            })
            .collect(),
    )
}

pub fn all(path: &Path) -> Result<Vec<Saved>, ExploreError> {
    read(path)
}

/// Save `name` (replacing a query of that name); returns every query, sorted by name the way
/// Python's `str.casefold` sorts them.
pub fn upsert(
    path: &Path,
    name: Option<&Value>,
    sql: Option<&Value>,
    note: Option<&Value>,
) -> Result<Vec<Saved>, ExploreError> {
    let invalid = |message: String| ExploreError::Invalid(message);
    let name = match name {
        Some(Value::String(name))
            if !py_strip(name).is_empty() && name.chars().count() <= NAME_MAX =>
        {
            py_strip(name).to_string()
        }
        _ => {
            return Err(invalid(format!(
                "name must be text of 1 to {NAME_MAX} characters"
            )));
        }
    };
    let sql = match sql {
        Some(Value::String(sql)) if !py_strip(sql).is_empty() => sql.clone(),
        _ => return Err(invalid("sql must be text".into())),
    };
    let note = match note {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(note)) => note.clone(),
        Some(_) => return Err(invalid("note must be text".into())),
    };
    let mut queries: Vec<Saved> = read(path)?.into_iter().filter(|q| q.name != name).collect();
    queries.push(Saved { name, sql, note });
    if queries.len() > MAX_ENTRIES {
        return Err(invalid(format!(
            "too many saved queries (max {MAX_ENTRIES})"
        )));
    }
    if body(&queries).len() > MAX_TOTAL_BYTES {
        return Err(invalid(format!(
            "saved queries would exceed {MAX_TOTAL_BYTES} bytes"
        )));
    }
    queries.sort_by_cached_key(|q| casefold(&q.name));
    write(path, &queries)?;
    Ok(queries)
}

/// Remove the query named `name`; returns the rest in the file's order.
pub fn delete(path: &Path, name: &str) -> Result<Vec<Saved>, ExploreError> {
    let queries = read(path)?;
    let before = queries.len();
    let kept: Vec<Saved> = queries.into_iter().filter(|q| q.name != name).collect();
    if kept.len() == before {
        return Err(ExploreError::NotFound(format!(
            "no saved query named {}",
            py_repr(name)
        )));
    }
    write(path, &kept)?;
    Ok(kept)
}

mod table {
    include!("casefold_table.rs");
}

/// The sort key Python's `str.casefold` gives: each character's lower case, except those
/// whose full case folding differs from it, which fold as the generated table says.
pub(crate) fn casefold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match table::FOLDS.binary_search_by_key(&c, |(from, _)| *from) {
            Ok(at) => out.push_str(table::FOLDS[at].1),
            Err(_) => out.extend(c.to_lowercase()),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn names(queries: &[Saved]) -> Vec<&str> {
        queries.iter().map(|q| q.name.as_str()).collect()
    }

    #[test]
    fn saving_replaces_by_name_and_sorts_as_casefold_does() {
        let dir = tempfile::tempdir().expect("dir");
        let path = dir.path().join(FILE);
        for name in ["b", "Straße", "Strasse2", "A"] {
            upsert(&path, Some(&json!(name)), Some(&json!("SELECT 1")), None).expect("saved");
        }
        let queries = upsert(
            &path,
            Some(&json!(" b ")),
            Some(&json!("SELECT 2")),
            Some(&json!("n")),
        )
        .expect("replaced");
        assert_eq!(names(&queries), ["A", "b", "Straße", "Strasse2"]);
        assert_eq!(queries[1].sql, "SELECT 2");
        assert_eq!(
            names(&delete(&path, "A").expect("deleted")),
            ["b", "Straße", "Strasse2"]
        );
        match delete(&path, "A") {
            Err(ExploreError::NotFound(message)) => assert_eq!(message, "no saved query named 'A'"),
            other => panic!("{other:?}"),
        }
        assert!(matches!(
            upsert(&path, Some(&json!("x")), Some(&json!("  ")), None),
            Err(ExploreError::Invalid(_))
        ));
    }
}
