//! The Query tab's guarded statement (`run_query` in `plexdb/explore.py`): one SELECT against
//! a fresh read-only connection, `query_only`, an authorizer that allows reads and nothing
//! else, a value-length limit and a deadline checked as the statement runs.
//!
//! Plex DB-EX's `/api/query`, its panels and dashboards, and the sweep's `record-panels` all
//! run a statement through [`run_query`], so a panel's nightly number and the Query tab's
//! answer come from the same guarded run. The explorer goes through [`QuerySlots`], which
//! holds a slot for the whole run, label lookup included.

use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use rusqlite::Connection;
use rusqlite::hooks::{AuthAction, Authorization};
use rusqlite::limits::Limit;
use rusqlite::types::ValueRef;

use plexdb_store::{StoreError, open_readonly, py_strip};

use crate::identity::PRIORITY;
use crate::pyjson::Json;

/// How long a statement may run, and how many rows it returns.
pub const QUERY_SECONDS: u64 = 5;
pub const QUERY_ROWS: usize = 500;
/// Longest string or blob one SQL function may build (`SQLITE_LIMIT_LENGTH`).
const QUERY_VALUE_BYTES: i32 = 8 << 20;
/// Statements between two looks at the clock.
const PROGRESS_OPS: i32 = 1000;
/// Statements Plex DB-EX runs at once. It has no login, so this bounds what an anonymous
/// caller can spend.
pub const QUERY_SLOTS: usize = 2;
/// Ids per label lookup, under SQLite's 999-variable limit on older builds.
const LABEL_CHUNK: usize = 500;

#[derive(Debug)]
pub enum QueryError {
    /// The store refused the statement or could not finish it; the message is SQLite's own.
    Refused(String),
    /// Every one of the explorer's [`QuerySlots`] is already running a statement.
    Busy(String),
    /// The store could not be opened.
    Store(StoreError),
}

/// One cell of a result, as the Query tab shows it.
#[derive(Debug, Clone, PartialEq)]
pub enum Cell {
    Null,
    Int(i64),
    Real(f64),
    Text(String),
    /// A blob, shown by its length.
    Blob(usize),
}

impl Cell {
    /// `_cell` in `plexdb/explore.py`: a blob by its size, a non-finite float as its name.
    pub fn json(&self) -> Json {
        match self {
            Cell::Null => Json::Null,
            Cell::Int(n) => Json::Int(*n),
            Cell::Real(x) if x.is_finite() => Json::Float(*x),
            Cell::Real(x) if x.is_nan() => Json::str("nan"),
            Cell::Real(x) => Json::str(if *x > 0.0 { "inf" } else { "-inf" }),
            Cell::Text(text) => Json::Str(text.clone()),
            Cell::Blob(len) => Json::Str(format!("<blob {len} bytes>")),
        }
    }
}

/// A statement that ran: its columns, up to [`QUERY_ROWS`] rows, and whether more followed.
pub struct Ran {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Cell>>,
    pub truncated: bool,
    /// Item id to readable label for every id-shaped cell that has an `items` row.
    pub labels: Vec<(String, String)>,
    /// Statement and label lookup together, as Python's `elapsed_ms` counts them.
    pub elapsed_ms: i64,
}

/// Plex DB-EX's limit on statements at once: [`QuerySlots::run`] is [`run_query`] once a
/// slot is free, and "two queries are already running" when none is.
#[derive(Default)]
pub struct QuerySlots(AtomicUsize);

/// A claim on one of the slots, given back on drop.
struct Slot<'a>(&'a AtomicUsize);

impl QuerySlots {
    fn take(&self) -> Option<Slot<'_>> {
        let mut held = self.0.load(Ordering::SeqCst);
        loop {
            if held >= QUERY_SLOTS {
                return None;
            }
            match self
                .0
                .compare_exchange(held, held + 1, Ordering::SeqCst, Ordering::SeqCst)
            {
                Ok(_) => return Some(Slot(&self.0)),
                Err(now) => held = now,
            }
        }
    }

    pub fn run(&self, store: &Path, sql: &str) -> Result<Ran, QueryError> {
        if py_strip(sql).is_empty() {
            return Err(QueryError::Refused("no SQL to run".into()));
        }
        let Some(_slot) = self.take() else {
            return Err(QueryError::Busy(
                "two queries are already running; try again in a moment".into(),
            ));
        };
        run_query(store, sql)
    }
}

impl Drop for Slot<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Whether `tail` holds anything past whitespace and comments (`lstrip_sql` in CPython's
/// `_sqlite/statement.c`).
fn has_more_sql(tail: &str) -> bool {
    let bytes = tail.as_bytes();
    let mut at = 0;
    while at < bytes.len() {
        match bytes[at] {
            b' ' | b'\t' | b'\x0c' | b'\n' | b'\r' => at += 1,
            b'-' if bytes.get(at + 1) == Some(&b'-') => {
                while at < bytes.len() && bytes[at] != b'\n' {
                    at += 1;
                }
                if at == bytes.len() {
                    return false;
                }
            }
            b'/' if bytes.get(at + 1) == Some(&b'*') => {
                at += 2;
                while at < bytes.len() && !(bytes[at] == b'*' && bytes.get(at + 1) == Some(&b'/')) {
                    at += 1;
                }
                if at == bytes.len() {
                    return false;
                }
                at += 2;
            }
            _ => return true,
        }
    }
    false
}

/// Run one SELECT against a fresh read-only connection, the Query tab's way.
pub fn run_query(store: &Path, sql: &str) -> Result<Ran, QueryError> {
    if py_strip(sql).is_empty() {
        return Err(QueryError::Refused("no SQL to run".into()));
    }
    let started = Instant::now();
    let mut conn = open_readonly(store).map_err(QueryError::Store)?;
    let deadline = started + Duration::from_secs(QUERY_SECONDS);
    let timed_out = Arc::new(AtomicBool::new(false));
    let refused = |err: rusqlite::Error| {
        if timed_out.load(Ordering::Relaxed) {
            return QueryError::Refused(format!("query stopped after {QUERY_SECONDS} seconds"));
        }
        QueryError::Refused(sqlite_message(err))
    };
    conn.execute_batch("PRAGMA query_only = ON; PRAGMA temp_store = FILE")
        .map_err(refused)?;
    conn.set_limit(Limit::SQLITE_LIMIT_LENGTH, QUERY_VALUE_BYTES)
        .map_err(refused)?;
    conn.authorizer(Some(|ctx: rusqlite::hooks::AuthContext<'_>| {
        match ctx.action {
            AuthAction::Select
            | AuthAction::Read { .. }
            | AuthAction::Function { .. }
            | AuthAction::Recursive => Authorization::Allow,
            _ => Authorization::Deny,
        }
    }))
    .map_err(refused)?;
    let flag = Arc::clone(&timed_out);
    conn.progress_handler(
        PROGRESS_OPS,
        Some(move || {
            let past = Instant::now() > deadline;
            flag.store(past, Ordering::Relaxed);
            past
        }),
    )
    .map_err(refused)?;
    let (columns, rows, fetched) = {
        let mut stmt = conn.prepare(sql).map_err(refused)?;
        // SQLite prepares the first statement and leaves the rest; Python's sqlite3 refuses
        // anything but whitespace and comments after it. A statement binds nothing, so a
        // parameter is Python's "bindings" error. The check comes first because
        // `expanded_sql` below rewrites a parameter as `NULL`, which moves the tail.
        if stmt.parameter_count() > 0 {
            return Err(QueryError::Refused(format!(
                "Incorrect number of bindings supplied. The current statement uses {}, and there \
                 are 0 supplied.",
                stmt.parameter_count()
            )));
        }
        if let Some(prepared) = stmt.expanded_sql()
            && has_more_sql(sql.get(prepared.len()..).unwrap_or_default())
        {
            return Err(refused(rusqlite::Error::MultipleStatement));
        }
        if stmt.column_count() == 0 {
            return Err(QueryError::Refused("the statement returns no rows".into()));
        }
        let columns: Vec<String> = stmt.column_names().map(str::to_string).collect();
        let mut cursor = stmt.query([]).map_err(refused)?;
        let mut rows = Vec::new();
        let mut fetched = 0;
        // One row past the cap, so `truncated` is known and a statement that sorts or scans
        // meets the same deadline.
        while fetched <= QUERY_ROWS {
            let Some(row) = cursor.next().map_err(refused)? else {
                break;
            };
            // Python's `fetchmany` decodes the extra row too, so its text refuses alike.
            let mut cells = Vec::with_capacity(columns.len());
            for (at, name) in columns.iter().enumerate() {
                cells.push(cell(name, row.get_ref(at).map_err(refused)?)?);
            }
            if fetched < QUERY_ROWS {
                rows.push(cells);
            }
            fetched += 1;
        }
        (columns, rows, fetched)
    };
    // The statement finished inside its budget; what follows is decoration and must not
    // turn that result into a timeout.
    conn.progress_handler(0, None::<fn() -> bool>)
        .map_err(refused)?;
    let labels = item_labels(&conn, &rows)?;
    Ok(Ran {
        columns,
        rows,
        truncated: fetched > QUERY_ROWS,
        labels,
        elapsed_ms: (started.elapsed().as_secs_f64() * 1000.0).round() as i64,
    })
}

/// SQLite's own message for `err`, as Python's `sqlite3` module words it.
fn sqlite_message(err: rusqlite::Error) -> String {
    match err {
        // Python's sqlite3 module refuses a second statement before SQLite sees it.
        rusqlite::Error::MultipleStatement => {
            "You can only execute one statement at a time.".into()
        }
        // `Display` adds the statement and offset; Python's message is SQLite's alone.
        rusqlite::Error::SqlInputError { msg, .. } => msg,
        other => other.to_string(),
    }
}

/// One value as a [`Cell`]. Text that is not UTF-8 refuses the statement, as Python's
/// `sqlite3` module refuses it.
fn cell(column: &str, value: ValueRef<'_>) -> Result<Cell, QueryError> {
    Ok(match value {
        ValueRef::Null => Cell::Null,
        ValueRef::Integer(n) => Cell::Int(n),
        ValueRef::Real(x) => Cell::Real(x),
        ValueRef::Text(bytes) => match std::str::from_utf8(bytes) {
            Ok(text) => Cell::Text(text.to_string()),
            Err(_) => {
                return Err(QueryError::Refused(format!(
                    "Could not decode to UTF-8 column '{column}' with text '{}'",
                    String::from_utf8_lossy(bytes)
                )));
            }
        },
        ValueRef::Blob(bytes) => Cell::Blob(bytes.len()),
    })
}

/// `_item_label`: `Show S01E02: Title` for an episode, `Title (1999)` for the rest.
fn item_label(
    kind: Option<&str>,
    title: &str,
    show_title: Option<&str>,
    season: Option<i64>,
    episode: Option<i64>,
    year: Option<i64>,
) -> String {
    if kind == Some("episode")
        && let Some(show) = show_title.filter(|show| !show.is_empty())
    {
        let code = match (season, episode) {
            (Some(season), Some(episode)) => format!(" S{season:02}E{episode:02}"),
            _ => String::new(),
        };
        return format!("{show}{code}: {title}");
    }
    match year {
        Some(year) => format!("{title} ({year})"),
        None => title.to_string(),
    }
}

/// A readable label for each item id among `rows`' cells (`_item_labels`): one SELECT per
/// chunk of ids on the statement's own connection. An id with no `items` row is left out,
/// and a cell that is not id-shaped is never looked up.
fn item_labels(conn: &Connection, rows: &[Vec<Cell>]) -> Result<Vec<(String, String)>, QueryError> {
    let prefixes: Vec<String> = PRIORITY
        .iter()
        .map(|ns| format!("{ns}:"))
        .chain(["fs:".to_string()])
        .collect();
    let ids: std::collections::BTreeSet<&str> = rows
        .iter()
        .flatten()
        .filter_map(|cell| match cell {
            Cell::Text(text) if prefixes.iter().any(|p| text.starts_with(p.as_str())) => {
                Some(text.as_str())
            }
            _ => None,
        })
        .collect();
    let ids: Vec<&str> = ids.into_iter().collect();
    let refused = |err: rusqlite::Error| QueryError::Refused(sqlite_message(err));
    let mut labels: Vec<(String, String)> = Vec::new();
    for chunk in ids.chunks(LABEL_CHUNK) {
        let marks = vec!["?"; chunk.len()].join(",");
        let mut stmt = conn
            .prepare(&format!(
                "SELECT item_id, type, title, show_title, season, episode, year \
                     FROM items WHERE item_id IN ({marks})"
            ))
            .map_err(refused)?;
        let mut rows = stmt
            .query(rusqlite::params_from_iter(chunk.iter()))
            .map_err(refused)?;
        while let Some(row) = rows.next().map_err(refused)? {
            let item_id: String = row.get(0).map_err(refused)?;
            let label = item_label(
                row.get::<_, Option<String>>(1).map_err(refused)?.as_deref(),
                &row.get::<_, Option<String>>(2)
                    .map_err(refused)?
                    .unwrap_or_default(),
                row.get::<_, Option<String>>(3).map_err(refused)?.as_deref(),
                row.get(4).map_err(refused)?,
                row.get(5).map_err(refused)?,
                row.get(6).map_err(refused)?,
            );
            match labels.iter_mut().find(|(id, _)| *id == item_id) {
                Some((_, held)) => *held = label,
                None => labels.push((item_id, label)),
            }
        }
    }
    Ok(labels)
}

impl Ran {
    /// The `/api/query` body: columns, rows, labels, `row_count`, `truncated`, `elapsed_ms`.
    pub fn json(&self) -> Json {
        Json::object([
            (
                "columns",
                Json::Array(self.columns.iter().cloned().map(Json::Str).collect()),
            ),
            ("rows", self.rows_json()),
            (
                "labels",
                Json::Object(
                    self.labels
                        .iter()
                        .map(|(id, label)| (id.clone(), Json::Str(label.clone())))
                        .collect(),
                ),
            ),
            ("row_count", Json::Int(self.rows.len() as i64)),
            ("truncated", Json::Bool(self.truncated)),
            ("elapsed_ms", Json::Int(self.elapsed_ms)),
        ])
    }

    pub fn rows_json(&self) -> Json {
        Json::Array(
            self.rows
                .iter()
                .map(|row| Json::Array(row.iter().map(Cell::json).collect()))
                .collect(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store(dir: &Path) -> std::path::PathBuf {
        let path = dir.join("plexdb.db");
        let conn = Connection::open(&path).expect("store");
        conn.execute_batch(
            "CREATE TABLE items (item_id TEXT PRIMARY KEY, type TEXT, title TEXT,
                                 show_title TEXT, season INTEGER, episode INTEGER, year INTEGER);
             INSERT INTO items VALUES ('imdb:tt1', 'movie', 'A', NULL, NULL, NULL, 1999),
                                      ('plex:9', 'episode', 'Pilot', 'Show', 1, 2, 2001),
                                      ('fs:abc', 'movie', 'C', NULL, NULL, NULL, NULL);",
        )
        .expect("rows");
        path
    }

    #[test]
    fn cells_labels_and_truncation() {
        let dir = tempfile::tempdir().expect("dir");
        let path = store(dir.path());
        let ran = run_query(
            &path,
            "SELECT item_id, x'0102', 1e999, -1e999, 2.5, NULL FROM items ORDER BY item_id",
        )
        .map_err(|err| format!("{err:?}"))
        .expect("ran");
        assert_eq!(
            ran.rows[0][1..],
            [
                Cell::Blob(2),
                Cell::Real(f64::INFINITY),
                Cell::Real(f64::NEG_INFINITY),
                Cell::Real(2.5),
                Cell::Null
            ]
        );
        assert_eq!(ran.rows[0][1].json(), Json::str("<blob 2 bytes>"));
        assert_eq!(ran.rows[0][2].json(), Json::str("inf"));
        let mut labels = ran.labels.clone();
        labels.sort();
        assert_eq!(
            labels,
            [
                ("fs:abc".to_string(), "C".to_string()),
                ("imdb:tt1".to_string(), "A (1999)".to_string()),
                ("plex:9".to_string(), "Show S01E02: Pilot".to_string()),
            ]
        );
        let many = run_query(
            &path,
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 600) \
             SELECT i FROM n",
        )
        .map_err(|err| format!("{err:?}"))
        .expect("ran");
        assert_eq!((many.rows.len(), many.truncated), (QUERY_ROWS, true));
    }

    #[test]
    fn only_whitespace_and_comments_may_follow_a_statement() {
        assert!(!has_more_sql(""));
        assert!(!has_more_sql(" \n\t"));
        assert!(!has_more_sql("-- done"));
        assert!(!has_more_sql(" /* done */ \n-- more"));
        assert!(!has_more_sql("/* unterminated"));
        assert!(has_more_sql(";"));
        assert!(has_more_sql(" SELECT 2"));
        assert!(has_more_sql("/* c */ SELECT 2"));
        assert!(has_more_sql("-"));
    }

    #[test]
    fn a_third_statement_at_once_is_told_to_wait() {
        let dir = tempfile::tempdir().expect("dir");
        let path = store(dir.path());
        let slots = QuerySlots::default();
        let held: Vec<Slot> = std::iter::from_fn(|| slots.take())
            .take(QUERY_SLOTS)
            .collect();
        let busy = matches!(slots.run(&path, "SELECT 1"), Err(QueryError::Busy(_)));
        let blank = matches!(slots.run(&path, " "), Err(QueryError::Refused(_)));
        drop(held);
        assert!(busy && blank);
        assert!(slots.run(&path, "SELECT 1").is_ok());
    }

    #[test]
    fn refusals_read_as_python_words_them() {
        let dir = tempfile::tempdir().expect("dir");
        let path = store(dir.path());
        let message = |sql: &str| match run_query(&path, sql) {
            Err(QueryError::Refused(message)) => message,
            Err(other) => format!("{other:?}"),
            Ok(_) => "ran".into(),
        };
        assert_eq!(message("  "), "no SQL to run");
        assert_eq!(
            message("SELECT 1; SELECT 2"),
            "You can only execute one statement at a time."
        );
        assert_eq!(message("SELECT 1; -- done"), "ran");
        assert_eq!(
            message("SELECT CAST(x'41ff42' AS TEXT) AS bad"),
            "Could not decode to UTF-8 column 'bad' with text 'A\u{fffd}B'"
        );
        // Row 501 is fetched only to set `truncated`, and its text still refuses.
        assert_eq!(
            message(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 501) \
                 SELECT CASE i WHEN 501 THEN CAST(x'41ff42' AS TEXT) ELSE 'ok' END AS bad FROM n"
            ),
            "Could not decode to UTF-8 column 'bad' with text 'A\u{fffd}B'"
        );
        assert_eq!(message("DELETE FROM items"), "not authorized");
        assert_eq!(
            message("SELECT ?"),
            "Incorrect number of bindings supplied. The current statement uses 1, and there are \
             0 supplied."
        );
    }
}
