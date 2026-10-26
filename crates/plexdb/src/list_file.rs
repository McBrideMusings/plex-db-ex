//! Writing `list_decisions.json`: the file Plex DB-EX's Lists tab and `plexdb
//! list-decide` append a person's decision about a crowd list to
//! (`plexdb/list_decisions.py`, `plexdb/decisions.py` `DecisionsFile.record`).
//!
//! The file keeps one entry per canonical list URL, written whole by rename so a reader
//! never sees half of it. Python's explorer writes the same file in-process until it
//! moves (plex-db-ex-f58.11); both spell it identically, with `json.dumps(indent=2)`'s
//! ASCII-only escapes.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value};

use crate::decisions::{self, DecisionsFileError};
use crate::list_decisions::{SPEC, canonical_list_url};
use crate::staleness::Now;

pub const URL_MAX: usize = 300;
pub const MAX_ENTRIES: usize = 50_000;
pub const MAX_TOTAL_BYTES: usize = 8 << 20;
pub const DECISIONS: [&str; 5] = ["added", "excluded", "refetch", "retry", "removed"];

#[derive(Debug, thiserror::Error)]
pub enum DecideError {
    /// A bad field, or a write that would break a limit: `ValueError`.
    #[error("{0}")]
    Invalid(String),
    /// A refetch, retry or removal named a list nothing knows: `UnknownList`.
    #[error("{0}")]
    Unknown(String),
    #[error(transparent)]
    File(#[from] DecisionsFileError),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
}

pub struct ListFile {
    path: PathBuf,
}

/// One entry of the file as written: `url`, `decision`, `decided_at`.
pub type Latest = HashMap<String, (String, String)>;

impl ListFile {
    pub fn new(path: PathBuf) -> Self {
        ListFile { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The file's entries as written, or `None` when there is no file. The file is
    /// checked first, so a malformed one is refused whole.
    fn load_raw(&self) -> Result<Option<Vec<Value>>, DecisionsFileError> {
        let Some(_) = decisions::load(&SPEC, &self.path)? else {
            return Ok(None);
        };
        let text = std::fs::read_to_string(&self.path).map_err(|err| {
            DecisionsFileError(format!("cannot read {}: {err}", self.path.display()))
        })?;
        match serde_json::from_str(&text) {
            Ok(Value::Array(entries)) => Ok(Some(entries)),
            _ => Err(DecisionsFileError(format!(
                "{} does not hold a list of decisions",
                self.path.display()
            ))),
        }
    }

    /// The last entry for each canonical URL, as `(decision, decided_at)`.
    pub fn latest(&self) -> Result<Latest, DecisionsFileError> {
        let mut out = Latest::new();
        for entry in self.load_raw()?.unwrap_or_default() {
            let text = |field: &str| entry.get(field).and_then(Value::as_str).unwrap_or("");
            if let Some(url) = canonical_list_url(text("url")) {
                out.insert(
                    url,
                    (text("decision").to_string(), text("decided_at").to_string()),
                );
            }
        }
        Ok(out)
    }

    /// Append one decision on canonical `url`, dropping every earlier entry naming the
    /// same list; return the entry.
    pub fn record(&self, url: &str, decision: &str) -> Result<Map<String, Value>, DecideError> {
        let mut entry = Map::new();
        entry.insert("url".into(), url.into());
        entry.insert("decision".into(), decision.into());
        entry.insert("decided_at".into(), Now::read().iso().into());
        let mut kept: Vec<Value> = self
            .load_raw()?
            .unwrap_or_default()
            .into_iter()
            .filter(|e| {
                let named = e
                    .get("url")
                    .and_then(Value::as_str)
                    .and_then(canonical_list_url);
                named.as_deref() != Some(url)
            })
            .collect();
        kept.push(Value::Object(entry.clone()));
        if kept.len() > MAX_ENTRIES {
            return Err(DecideError::Invalid(format!(
                "too many list decisions (max {MAX_ENTRIES})"
            )));
        }
        let body = format!(
            "{}\n",
            ascii_json(&serde_json::to_string_pretty(&kept).unwrap_or_default())
        );
        if body.len() > MAX_TOTAL_BYTES {
            return Err(DecideError::Invalid(format!(
                "list decisions would exceed {MAX_TOTAL_BYTES} bytes"
            )));
        }
        self.write(&body)?;
        Ok(entry)
    }

    fn write(&self, body: &str) -> Result<(), DecisionsFileError> {
        let mut name = self.path.file_name().unwrap_or_default().to_os_string();
        name.push(".tmp");
        let tmp = self.path.with_file_name(name);
        let done = std::fs::write(&tmp, body).and_then(|()| std::fs::rename(&tmp, &self.path));
        if let Err(err) = done {
            let _ = std::fs::remove_file(&tmp);
            return Err(DecisionsFileError(format!(
                "cannot write {}: {err}",
                self.path.display()
            )));
        }
        Ok(())
    }
}

/// Python's `json.dumps` default `ensure_ascii`: every character outside printable ASCII
/// becomes `\uXXXX`, a surrogate pair above the BMP.
pub fn ascii_json(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if (' '..='~').contains(&c) || matches!(c, '\n') {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// The canonical URL of every list the store holds, has decided or has failed to fetch.
pub fn known_list_urls(conn: &Connection) -> rusqlite::Result<HashSet<String>> {
    let mut urls = HashSet::new();
    for query in [
        "SELECT url FROM collection WHERE url IS NOT NULL",
        "SELECT url FROM list_decision",
        "SELECT list_key FROM list_attempt",
    ] {
        let mut stmt = conn.prepare(query)?;
        for raw in stmt.query_map([], |row| row.get::<_, String>(0))? {
            if let Some(canonical) = canonical_list_url(&raw?) {
                urls.insert(canonical);
            }
        }
    }
    Ok(urls)
}

/// `(canonical url, decision)`, or the field that is wrong.
pub fn check(url: &str, decision: &str) -> Result<(String, String), DecideError> {
    if url.is_empty() || url.chars().count() > URL_MAX {
        return Err(DecideError::Invalid(format!(
            "url must be text of 1 to {URL_MAX} characters"
        )));
    }
    let Some(canonical) = canonical_list_url(url) else {
        return Err(DecideError::Invalid(
            "url must be an MDBList list, like https://mdblist.com/lists/<user>/<list>, or a \
             Letterboxd list, like https://letterboxd.com/<user>/list/<list>/"
                .into(),
        ));
    };
    if !DECISIONS.contains(&decision) {
        return Err(DecideError::Invalid(format!(
            "decision must be one of {}",
            DECISIONS.join(", ")
        )));
    }
    Ok((canonical, decision.to_string()))
}

/// Append one decision to the file and return its entry. Anyone may add or exclude any
/// MDBList or Letterboxd list URL. A refetch, retry or removal must name a list the store
/// (or the file) already knows, so a caller cannot grow the file with made-up lists.
pub fn decide(
    conn: &Connection,
    file: &ListFile,
    url: &str,
    decision: &str,
) -> Result<Map<String, Value>, DecideError> {
    let (canonical, verdict) = check(url, decision)?;
    let latest = file.latest()?;
    if verdict != "added"
        && verdict != "excluded"
        && !known_list_urls(conn)?.contains(&canonical)
        && !latest.contains_key(&canonical)
    {
        return Err(DecideError::Unknown(canonical));
    }
    let folded_add: bool = conn
        .query_row(
            "SELECT 1 FROM list_decision WHERE url = ? AND added = 1",
            [&canonical],
            |_| Ok(()),
        )
        .optional()?
        .is_some();
    if (verdict == "refetch" || verdict == "retry")
        && latest.get(&canonical).is_some_and(|(d, _)| d == "added")
        && !folded_add
    {
        return Err(DecideError::Invalid(format!(
            "{canonical} was added but no sweep has folded the add yet; a {verdict} now would \
             replace it"
        )));
    }
    file.record(&canonical, &verdict)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_ascii_text_is_escaped_as_python_escapes_it() {
        assert_eq!(
            ascii_json("\"é 😀 \u{7f}\""),
            "\"\\u00e9 \\ud83d\\ude00 \\u007f\""
        );
    }
}
