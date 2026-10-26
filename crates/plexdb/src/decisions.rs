//! A Plex DB-EX decisions file: a person's decisions, waiting for a sweep to fold
//! them into the store (`plexdb/decisions.py`).
//!
//! Plex DB-EX never writes `plexdb.db` (ADR-0007, ADR-0017), so a decision goes
//! into a JSON list of entries beside its saved-queries file: a few text fields
//! naming what was decided, `decision`, any values the decision carries, and
//! `decided_at`. The whole file is checked before a fold changes any row.

use std::path::Path;

use plexdb_store::{Config, StoreError, open_store};
use rusqlite::Connection;
use serde_json::Value;
use thiserror::Error;

use crate::pyiso::is_isoformat;

/// A decisions file that is unreadable or breaks the documented shape. The fold
/// changes nothing.
#[derive(Debug, Error)]
#[error("{0}")]
pub struct DecisionsFileError(pub String);

/// The shape one decisions file takes (a `DecisionsFile` subclass).
pub struct Spec {
    /// The text fields naming what an entry decides, in key order.
    pub key: &'static [&'static str],
    pub decisions: &'static [&'static str],
    /// Text fields an entry carries unless its decision is `cleared`.
    pub values: &'static [&'static str],
    /// Why a key cannot be an entry's key, or `None`.
    pub key_problem: fn(&[String]) -> Option<String>,
    /// Why an entry's `values` cannot be stored, or `None`.
    pub values_problem: fn(&[String]) -> Option<String>,
}

/// A spec with no extra rule on its values.
pub fn no_values_problem(_values: &[String]) -> Option<String> {
    None
}

/// One checked entry: its key fields, decision, values and `decided_at`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub key: Vec<String>,
    pub decision: String,
    pub values: Vec<String>,
    pub decided_at: String,
}

/// What a fold did, for its one-line report (`FoldStats`).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct FoldStats {
    pub file_found: bool,
    pub entries: usize,
    /// Entries naming something the store has a row for.
    pub matched: usize,
    /// Entries naming nothing a row could be keyed on.
    pub unmatched: usize,
}

/// Python's `repr` of a string, for messages that quote a value.
pub fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (0x7f..=0xa0).contains(&(c as u32)) => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `repr` of a tuple of strings.
pub fn py_tuple_repr(items: &[&str]) -> String {
    let inner: Vec<String> = items.iter().map(|item| py_repr(item)).collect();
    if inner.len() == 1 {
        format!("({},)", inner[0])
    } else {
        format!("({})", inner.join(", "))
    }
}

/// The file's entries, or the first way one breaks `spec` (`DecisionsFile.parse`).
pub fn parse(spec: &Spec, path: &Path, text: &str) -> Result<Vec<Entry>, DecisionsFileError> {
    let path = path.display();
    let raw: Value = serde_json::from_str(text)
        .map_err(|err| DecisionsFileError(format!("{path} is not valid JSON: {err}")))?;
    let Value::Array(raw) = raw else {
        return Err(DecisionsFileError(format!(
            "{path} does not hold a list of decisions"
        )));
    };
    let fields: Vec<&str> = spec
        .key
        .iter()
        .copied()
        .chain(["decision", "decided_at"])
        .collect();
    let mut entries = Vec::with_capacity(raw.len());
    for (index, entry) in raw.iter().enumerate() {
        let at = format!("{path} entry {index}");
        let text_of = |field: &str| entry.get(field).and_then(Value::as_str).map(str::to_string);
        let texts: Option<Vec<String>> = fields.iter().map(|field| text_of(field)).collect();
        let (Value::Object(_), Some(texts)) = (entry, texts) else {
            let (last, rest) = fields.split_last().unwrap_or((&"decided_at", &[]));
            return Err(DecisionsFileError(format!(
                "{at} needs text {} and {last}",
                rest.join(", ")
            )));
        };
        let key = texts[..spec.key.len()].to_vec();
        let decision = texts[spec.key.len()].clone();
        let decided_at = texts[spec.key.len() + 1].clone();
        if !spec.decisions.contains(&decision.as_str()) {
            return Err(DecisionsFileError(format!(
                "{at} has decision {}, expected one of {}",
                py_repr(&decision),
                py_tuple_repr(spec.decisions)
            )));
        }
        if let Some(problem) = (spec.key_problem)(&key) {
            return Err(DecisionsFileError(format!("{at} {problem}")));
        }
        let mut values = Vec::new();
        if decision != "cleared" {
            let found: Vec<Option<String>> = spec.values.iter().map(|f| text_of(f)).collect();
            let missing: Vec<&str> = spec
                .values
                .iter()
                .zip(&found)
                .filter(|(_, value)| value.is_none())
                .map(|(field, _)| *field)
                .collect();
            if !missing.is_empty() {
                return Err(DecisionsFileError(format!(
                    "{at} needs text {}",
                    missing.join(", ")
                )));
            }
            values = found.into_iter().flatten().collect();
            if let Some(problem) = (spec.values_problem)(&values) {
                return Err(DecisionsFileError(format!("{at} {problem}")));
            }
            if !is_isoformat(&decided_at) {
                return Err(DecisionsFileError(format!(
                    "{at} has decided_at {}, expected an ISO 8601 time",
                    py_repr(&decided_at)
                )));
            }
        }
        entries.push(Entry {
            key,
            decision,
            values,
            decided_at,
        });
    }
    Ok(entries)
}

/// The entries in `path`, or `None` when there is no file (`DecisionsFile.load`).
pub fn load(spec: &Spec, path: &Path) -> Result<Option<Vec<Entry>>, DecisionsFileError> {
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
    let text = String::from_utf8(bytes).map_err(|err| {
        DecisionsFileError(format!("{} is not UTF-8 text: {err}", path.display()))
    })?;
    parse(spec, path, &text).map(Some)
}

/// `n` with thousands separators, as Python's `f"{n:,}"` writes it.
pub fn thousands(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (index, c) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Why a fold stopped: the file is wrong, the store would not open, or SQLite
/// refused a write. The file is checked before the first row changes.
#[derive(Debug, Error)]
pub enum FoldError {
    #[error(transparent)]
    File(#[from] DecisionsFileError),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
}

/// What a `fold-*-decisions` step does: read `Config`, open the store for
/// writing, fold the file `file_name` names, and print the one-line report.
pub fn run_fold(
    file_name: &str,
    fold: fn(&mut Connection, &Path) -> Result<FoldStats, FoldError>,
    describe: fn(&FoldStats, &Path) -> String,
) -> Result<(), FoldError> {
    let config = Config::from_env()?;
    let path = config.decisions_file(file_name);
    let mut store = open_store(&config.store_path)?;
    let stats = fold(&mut store.conn, &path)?;
    println!("{}", describe(&stats, &path));
    Ok(())
}
