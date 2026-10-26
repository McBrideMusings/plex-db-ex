//! Writing `list_decisions.json`: the file Plex DB-EX's Lists tab and `plexdb
//! list-decide` append a person's decision about a crowd list to
//! (`plexdb/list_decisions.py`, `plexdb/decisions.py` `DecisionsFile.record`).
//!
//! The file keeps one entry per canonical list URL, written whole by rename so a reader
//! never sees half of it. Python's explorer writes the same file in-process until it
//! moves (plex-db-ex-f58.11); both spell it identically, with `json.dumps(indent=2)`'s
//! ASCII-only escapes.

use std::collections::HashSet;
use std::path::PathBuf;

use rusqlite::{Connection, OptionalExtension};

use crate::decision_book::{Book, Key, RecordError};
use crate::decisions::{DecisionsFileError, Entry};
use crate::list_decisions::{LIST_DECISIONS_FILE, SPEC, canonical_list_url};

pub const URL_MAX: usize = 300;
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

impl From<RecordError> for DecideError {
    fn from(err: RecordError) -> Self {
        match err {
            RecordError::Invalid(message) => DecideError::Invalid(message),
            RecordError::File(err) => DecideError::File(err),
        }
    }
}

/// `list_decisions.json` at `path`: one entry per list, keyed `(url,)`.
pub fn list_book(path: PathBuf) -> Book {
    Book::new(
        &SPEC,
        "list decisions",
        |key| canonical_list_url(&key[0]).map(|url| vec![url]),
        |key, _| vec![key.to_vec()],
        path,
    )
}

/// The file `list_decisions.json` is called, beside the explorer's other files.
pub fn list_book_in(dir: &std::path::Path) -> Book {
    list_book(dir.join(LIST_DECISIONS_FILE))
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
    book: &Book,
    url: &str,
    decision: &str,
) -> Result<Entry, DecideError> {
    let (canonical, verdict) = check(url, decision)?;
    let latest = book.latest()?;
    let key: Key = vec![canonical.clone()];
    if verdict != "added"
        && verdict != "excluded"
        && !known_list_urls(conn)?.contains(&canonical)
        && latest.get(&key).is_none()
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
        && latest
            .get(&key)
            .is_some_and(|pending| pending.decision == "added")
        && !folded_add
    {
        return Err(DecideError::Invalid(format!(
            "{canonical} was added but no sweep has folded the add yet; a {verdict} now would \
             replace it"
        )));
    }
    let (_, entry) = book.record(&key, &verdict, &Default::default(), &[])?;
    Ok(entry)
}
