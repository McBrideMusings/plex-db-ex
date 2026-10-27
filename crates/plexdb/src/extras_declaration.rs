//! `fold-extras`: the extras libraries a person declared in `extras.json`, written into
//! `extra_library` and `extra_field` (ADR-0030).
//!
//! `extras.json` is a JSON list with one entry per library: `name`, `root`, `type`,
//! `fields` (each `name`, `kind`, optional `required`), an optional `template` and optional
//! `parsers`. Nothing here knows what a library holds. The whole file is checked before the
//! first row changes, so a refused file leaves the stored declaration as it was.

use std::collections::BTreeSet;
use std::path::Path;

use plexdb_store::StoreError;
use rusqlite::{Connection, TransactionBehavior, params};
use serde_json::Value;
use thiserror::Error;

pub const NAME: &str = "fold-extras";
pub const EXTRAS_FILE: &str = "extras.json";

/// The kinds a field may have.
pub const KINDS: [&str; 4] = ["text", "int", "year", "list"];

/// Every extra has these without declaring them, so a library may not.
pub const IMPLICIT_FIELDS: [&str; 2] = ["tags", "source_url"];

/// Why a fold stopped.
#[derive(Debug, Error)]
pub enum ExtrasError {
    /// `extras.json` breaks a rule. The stored declaration is untouched.
    #[error("{0}")]
    Refused(String),
    #[error(transparent)]
    Store(#[from] StoreError),
}

impl From<rusqlite::Error> for ExtrasError {
    fn from(err: rusqlite::Error) -> Self {
        ExtrasError::Store(err.into())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Field {
    pub name: String,
    pub kind: String,
    pub required: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Library {
    pub name: String,
    /// Absolute, without a trailing slash.
    pub root: String,
    pub kind: String,
    pub template: Option<String>,
    pub fields: Vec<Field>,
}

/// What a fold did.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct FoldStats {
    pub file_found: bool,
    pub declared: usize,
    pub changed: usize,
    pub removed: usize,
}

fn refused<T>(message: String) -> Result<T, ExtrasError> {
    Err(ExtrasError::Refused(message))
}

/// `path` is `root` or sits below it, compared at a path boundary.
pub fn is_under(path: &str, root: &str) -> bool {
    let root = root.trim_end_matches('/');
    path == root
        || path
            .strip_prefix(root)
            .is_some_and(|rest| rest.starts_with('/'))
}

/// The `{field}` names a template uses, or why it is not a template.
pub fn template_fields(template: &str) -> Result<Vec<String>, String> {
    let mut names = Vec::new();
    let mut open: Option<String> = None;
    for c in template.chars() {
        match (c, &mut open) {
            ('{', None) => open = Some(String::new()),
            ('{', Some(_)) => return Err("has a '{' inside a '{...}'".into()),
            ('}', Some(_)) => names.push(open.take().unwrap_or_default()),
            ('}', None) => return Err("has a '}' with no '{'".into()),
            (c, Some(name)) => name.push(c),
            (_, None) => {}
        }
    }
    if open.is_some() {
        return Err("has a '{' that never closes".into());
    }
    Ok(names)
}

fn text_field<'a>(entry: &'a Value, at: &str, field: &str) -> Result<&'a str, ExtrasError> {
    match entry.get(field).and_then(Value::as_str) {
        Some(text) if !text.trim().is_empty() => Ok(text),
        _ => refused(format!("{at} needs a non-empty text {field}")),
    }
}

fn parse_fields(entry: &Value, at: &str) -> Result<Vec<Field>, ExtrasError> {
    let Some(raw) = entry.get("fields").and_then(Value::as_array) else {
        return refused(format!("{at} needs a list of fields"));
    };
    let mut fields: Vec<Field> = Vec::new();
    for (index, field) in raw.iter().enumerate() {
        let at = format!("{at} field {index}");
        let name = text_field(field, &at, "name")?.to_string();
        if IMPLICIT_FIELDS.contains(&name.as_str()) {
            return refused(format!(
                "{at} is named {name:?}, which every extra has without declaring it"
            ));
        }
        if fields.iter().any(|seen| seen.name == name) {
            return refused(format!("{at} repeats the field name {name:?}"));
        }
        let kind = text_field(field, &at, "kind")?.to_string();
        if !KINDS.contains(&kind.as_str()) {
            return refused(format!(
                "{at} has kind {kind:?}, expected one of {}",
                KINDS.join(", ")
            ));
        }
        let required = match field.get("required") {
            None | Some(Value::Null) => false,
            Some(Value::Bool(flag)) => *flag,
            Some(_) => return refused(format!("{at} has a required that is not true or false")),
        };
        fields.push(Field {
            name,
            kind,
            required,
        });
    }
    Ok(fields)
}

/// The libraries `text` declares, or the first rule it breaks. `source_roots` is
/// `PLEX_SOURCE_ROOTS`; every root must sit under one of them and be a directory.
pub fn parse(
    text: &str,
    path: &Path,
    source_roots: &[String],
) -> Result<Vec<Library>, ExtrasError> {
    let path = path.display();
    let raw: Value = match serde_json::from_str(text) {
        Ok(raw) => raw,
        Err(err) => return refused(format!("{path} is not valid JSON: {err}")),
    };
    let Value::Array(raw) = raw else {
        return refused(format!("{path} does not hold a list of libraries"));
    };
    let mut libraries: Vec<Library> = Vec::new();
    for (index, entry) in raw.iter().enumerate() {
        let at = format!("{path} library {index}");
        let name = text_field(entry, &at, "name")?.to_string();
        let at = format!("{path} library {name:?}");
        if libraries.iter().any(|seen| seen.name == name) {
            return refused(format!("{at} repeats a library name"));
        }
        let kind = text_field(entry, &at, "type")?.to_string();
        let root = text_field(entry, &at, "root")?;
        let root = if root == "/" {
            root.to_string()
        } else {
            root.trim_end_matches('/').to_string()
        };
        if !root.starts_with('/') {
            return refused(format!("{at} has root {root:?}, which is not absolute"));
        }
        if !source_roots.iter().any(|base| is_under(&root, base)) {
            return refused(format!(
                "{at} has root {root:?}, which is not under PLEX_SOURCE_ROOTS ({})",
                source_roots.join(", ")
            ));
        }
        if !Path::new(&root).is_dir() {
            return refused(format!("{at} has root {root:?}, which is not a directory"));
        }
        let fields = parse_fields(entry, &at)?;
        let template = match entry.get("template") {
            None | Some(Value::Null) => None,
            Some(Value::String(template)) => Some(template.clone()),
            Some(_) => return refused(format!("{at} has a template that is not text")),
        };
        if let Some(template) = &template {
            let used = template_fields(template)
                .map_err(|why| ExtrasError::Refused(format!("{at} template {why}")))?;
            for name in used {
                if !fields.iter().any(|field| field.name == name) {
                    return refused(format!(
                        "{at} has a template naming {name:?}, which is not a declared field"
                    ));
                }
            }
        }
        if let Some(other) = libraries
            .iter()
            .find(|seen| is_under(&seen.root, &root) || is_under(&root, &seen.root))
        {
            return refused(format!(
                "{at} has root {root:?}, which overlaps library {:?} at {:?}",
                other.name, other.root
            ));
        }
        libraries.push(Library {
            name,
            root,
            kind,
            template,
            fields,
        });
    }
    Ok(libraries)
}

fn stored(conn: &Connection, name: &str) -> Result<Option<Library>, ExtrasError> {
    let mut stmt = conn.prepare("SELECT root, type, template FROM extra_library WHERE name = ?")?;
    let mut rows = stmt.query([name])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let (root, kind, template): (String, String, Option<String>) =
        (row.get(0)?, row.get(1)?, row.get(2)?);
    let mut stmt = conn
        .prepare("SELECT name, kind, required FROM extra_field WHERE library = ? ORDER BY rowid")?;
    let fields = stmt
        .query_map([name], |row| {
            Ok(Field {
                name: row.get(0)?,
                kind: row.get(1)?,
                required: row.get::<_, i64>(2)? == 1,
            })
        })?
        .collect::<Result<_, _>>()?;
    Ok(Some(Library {
        name: name.to_string(),
        root,
        kind,
        template,
        fields,
    }))
}

/// Write `libraries` as the stored declaration, in one transaction. A library no
/// longer declared is removed, unless it holds extras: that refuses the fold, because
/// dropping the entry would orphan their rows.
pub fn fold(
    conn: &mut Connection,
    libraries: &[Library],
    now: &str,
) -> Result<FoldStats, ExtrasError> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut stats = FoldStats {
        file_found: true,
        declared: libraries.len(),
        ..FoldStats::default()
    };
    let held: Vec<String> = {
        let mut stmt = tx.prepare("SELECT name FROM extra_library ORDER BY name")?;
        stmt.query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?
    };
    let declared: BTreeSet<&str> = libraries.iter().map(|lib| lib.name.as_str()).collect();
    for name in held.iter().filter(|name| !declared.contains(name.as_str())) {
        let items: i64 = tx.query_row(
            "SELECT COUNT(*) FROM extra_item WHERE library = ?",
            [name],
            |row| row.get(0),
        )?;
        if items > 0 {
            return refused(format!(
                "library {name:?} is no longer declared but holds {items} extra(s); \
                 declare it again, or delete its extras first"
            ));
        }
        tx.execute("DELETE FROM extra_library WHERE name = ?", [name])?;
        stats.removed += 1;
    }
    for library in libraries {
        if stored(&tx, &library.name)?.as_ref() == Some(library) {
            continue;
        }
        stats.changed += 1;
        tx.execute(
            "INSERT INTO extra_library (name, root, type, template, declared_at) \
             VALUES (?, ?, ?, ?, ?) \
             ON CONFLICT (name) DO UPDATE SET root = excluded.root, type = excluded.type, \
             template = excluded.template, declared_at = excluded.declared_at",
            params![
                library.name,
                library.root,
                library.kind,
                library.template,
                now
            ],
        )?;
        tx.execute("DELETE FROM extra_field WHERE library = ?", [&library.name])?;
        for field in &library.fields {
            tx.execute(
                "INSERT INTO extra_field (library, name, kind, required) VALUES (?, ?, ?, ?)",
                params![library.name, field.name, field.kind, field.required as i64],
            )?;
        }
    }
    tx.commit()?;
    Ok(stats)
}

/// Fold the file at `path`; a missing file is no change.
pub fn fold_file(
    conn: &mut Connection,
    path: &Path,
    source_roots: &[String],
    now: &str,
) -> Result<FoldStats, ExtrasError> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(FoldStats::default()),
        Err(err) => return refused(format!("cannot read {}: {err}", path.display())),
    };
    let libraries = parse(&text, path, source_roots)?;
    fold(conn, &libraries, now)
}

pub fn describe(stats: &FoldStats, path: &Path) -> String {
    let path = path.display();
    if !stats.file_found {
        return format!("extras: no file at {path}; the stored declaration stands");
    }
    format!(
        "extras: {} librar(ies) declared in {path}, {} written, {} removed",
        stats.declared, stats.changed, stats.removed
    )
}
