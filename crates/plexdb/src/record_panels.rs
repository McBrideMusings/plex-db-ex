//! `plexdb record-panels` (`plexdb/panels.py`, ADR-0029): run every dashboard panel's SQL
//! once and keep the number in `panel-history.json`.
//!
//! Both files sit in the decisions directory, never in `plexdb.db`: a count this project
//! computed is not a fact about a title. A panel's series is named by [`series_key`], which
//! hashes the SQL and the value setting, so an edit starts a new series and a line never
//! joins two different measurements.
//!
//! The panel's SQL runs the way the Query tab runs a statement ([`run_query`]): a fresh
//! read-only connection, `query_only`, an authorizer that allows reads and nothing else, and
//! a deadline checked as the statement runs.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use rusqlite::hooks::{AuthAction, Authorization};
use rusqlite::limits::Limit;
use rusqlite::types::ValueRef;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;

use plexdb_store::{Config, StoreError, open_readonly, py_strip};

use crate::pyjson::{Json, dumps_indent2};
use crate::staleness::Now;
use crate::steps::Failure;

pub const NAME: &str = "record-panels";

pub const PANELS_FILE: &str = "explore-panels.json";
pub const HISTORY_FILE: &str = "panel-history.json";

/// The Library health dashboard a missing panels file is first written with. An existing
/// file, even one with no dashboards, is never seeded again. The explorer's Python seeds
/// from the same panels until it moves; `tests/test_panels.py` holds the two together.
const DEFAULT_DASHBOARDS: &str = include_str!("default_dashboards.json");

/// A panel's `value` setting: every row counted, or one column of the first row.
const ROWS: &str = "rows";
const COLUMN_PREFIX: &str = "column:";
/// The display settings a chart cannot draw without.
const CHART_SETTINGS: &[(&str, &[&str])] = &[
    ("gauge", &["total"]),
    ("bar", &["label", "number"]),
    ("pie", &["label", "number"]),
    ("heatmap", &["x", "y", "number"]),
];
const CHARTS: &[&str] = &["line", "stat", "gauge", "bar", "pie", "table", "heatmap"];
const HEIGHTS: &[&str] = &["short", "tall"];
/// How many hex digits of the SQL-and-value hash a series key keeps.
const HASH_DIGITS: usize = 12;

/// The Query tab's limits (`plexdb/explore.py`).
const QUERY_SECONDS: u64 = 5;
const QUERY_ROWS: usize = 500;
/// Longest string or blob one SQL function may build (`SQLITE_LIMIT_LENGTH`).
const QUERY_VALUE_BYTES: i32 = 8 << 20;
/// Statements between two looks at the clock.
const PROGRESS_OPS: i32 = 1000;

#[derive(Debug, Error)]
pub enum PanelsError {
    /// `explore-panels.json` or `panel-history.json` is unreadable or not the shape.
    #[error("{0}")]
    File(String),
    #[error(transparent)]
    Store(#[from] StoreError),
}

/// A query the store refused or could not finish; the message is SQLite's own.
#[derive(Debug)]
struct QueryError(String);

enum MeasureError {
    Query(QueryError),
    Store(StoreError),
}

impl From<QueryError> for MeasureError {
    fn from(err: QueryError) -> Self {
        MeasureError::Query(err)
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Number {
    Int(i64),
    Real(f64),
}

impl Number {
    fn json(self) -> Value {
        match self {
            Number::Int(n) => Value::from(n),
            Number::Real(x) => Value::from(x),
        }
    }

    fn shown(self) -> String {
        match self {
            Number::Int(n) => n.to_string(),
            Number::Real(x) => crate::pyjson::float_repr(x),
        }
    }
}

#[derive(Debug)]
struct Panel {
    id: String,
    sql: String,
    /// `rows` or `column:<name>`.
    value: String,
}

impl Panel {
    /// The column `value` names, or `None` when it counts rows.
    fn column(&self) -> Option<&str> {
        self.value.strip_prefix(COLUMN_PREFIX)
    }
}

#[derive(Debug)]
struct Dashboard {
    id: String,
    panels: Vec<Panel>,
}

/// `<panel id>:<first 12 hex of sha256(sql NUL value)>`, the name of the panel's series.
fn series_key(panel: &Panel) -> String {
    let digest = Sha256::digest(format!("{}\0{}", panel.sql, panel.value).as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("{}:{}", panel.id, &hex[..HASH_DIGITS])
}

fn file_error(message: String) -> PanelsError {
    PanelsError::File(message)
}

fn text(where_: &str, entry: &Map<String, Value>, field: &str) -> Result<String, PanelsError> {
    match entry.get(field) {
        Some(Value::String(value)) if !value.is_empty() => Ok(value.clone()),
        _ => Err(file_error(format!("{where_} needs text {field}"))),
    }
}

fn parse_dashboard(path: &Path, index: usize, raw: &Value) -> Result<Dashboard, PanelsError> {
    let where_ = format!("{} dashboard {index}", path.display());
    let entry = raw
        .as_object()
        .filter(|entry| entry.get("panels").is_some_and(Value::is_array))
        .ok_or_else(|| file_error(format!("{where_} needs text id, name and a panels list")))?;
    let id = text(&where_, entry, "id")?;
    text(&where_, entry, "name")?;
    let panels = entry["panels"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .enumerate()
        .map(|(i, panel)| parse_panel(&format!("{where_} panel {i}"), panel))
        .collect::<Result<_, _>>()?;
    Ok(Dashboard { id, panels })
}

fn parse_panel(where_: &str, raw: &Value) -> Result<Panel, PanelsError> {
    let entry = raw
        .as_object()
        .ok_or_else(|| file_error(format!("{where_} is not an object")))?;
    let value = text(where_, entry, "value")?;
    if value != ROWS
        && !(value.starts_with(COLUMN_PREFIX) && value.chars().count() > COLUMN_PREFIX.len())
    {
        return Err(file_error(format!(
            "{where_} has value {}, expected rows or column:<name>",
            crate::decisions::py_repr(&value)
        )));
    }
    let width = entry.get("width");
    if !width
        .and_then(Value::as_i64)
        .is_some_and(|width| (1..=4).contains(&width))
    {
        return Err(file_error(format!(
            "{where_} has width {}, expected 1 to 4",
            width.map_or_else(|| "None".into(), py_json_repr)
        )));
    }
    let height = entry.get("height");
    if !height
        .and_then(Value::as_str)
        .is_some_and(|height| HEIGHTS.contains(&height))
    {
        return Err(file_error(format!(
            "{where_} has height {}, expected one of ('short', 'tall')",
            height.map_or_else(|| "None".into(), py_json_repr)
        )));
    }
    let chart = text(where_, entry, "chart")?;
    if !CHARTS.contains(&chart.as_str()) {
        return Err(file_error(format!(
            "{where_} has chart {}, expected one of ('line', 'stat', 'gauge', 'bar', 'pie', \
             'table', 'heatmap')",
            crate::decisions::py_repr(&chart)
        )));
    }
    for (_, needed) in CHART_SETTINGS.iter().filter(|(name, _)| *name == chart) {
        for field in *needed {
            text(where_, entry, field)?;
        }
    }
    Ok(Panel {
        id: text(where_, entry, "id")?,
        sql: {
            text(where_, entry, "title")?;
            text(where_, entry, "sql")?
        },
        value,
    })
}

/// How Python's `repr` writes a parsed JSON value in an error message.
fn py_json_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(text) => crate::decisions::py_repr(text),
        other => other.to_string(),
    }
}

/// The dashboards in `explore-panels.json`, or `None` when there is no file. A file that
/// is not valid JSON or breaks the shape is refused.
fn read_panels(path: &Path) -> Result<Option<(Value, Vec<Dashboard>)>, PanelsError> {
    let body = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => {
            return Err(file_error(format!("cannot read {}: {err}", path.display())));
        }
    };
    let raw: Value = std::str::from_utf8(&body)
        .map_err(|err| err.to_string())
        .and_then(|body| serde_json::from_str(body).map_err(|err| err.to_string()))
        .map_err(|err| file_error(format!("{} is not valid JSON: {err}", path.display())))?;
    let Some(list) = raw.get("dashboards").and_then(Value::as_array) else {
        return Err(file_error(format!(
            "{} does not hold a dashboards list",
            path.display()
        )));
    };
    let dashboards = list
        .iter()
        .enumerate()
        .map(|(i, dashboard)| parse_dashboard(path, i, dashboard))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Some((raw, dashboards)))
}

/// Replace `path` by rename, so a reader never sees half of it.
fn write_atomically(path: &Path, tmp: &Path, body: &str) -> Result<(), PanelsError> {
    let written = fs::write(tmp, body).and_then(|()| fs::rename(tmp, path));
    if let Err(err) = written {
        let _ = fs::remove_file(tmp);
        return Err(file_error(format!(
            "cannot write {}: {err}",
            path.display()
        )));
    }
    Ok(())
}

/// The dashboards; a missing file is first written with the default dashboards. A file
/// that exists is read as it is, so one with no dashboards stays empty.
fn read_or_seed(path: &Path) -> Result<Vec<Dashboard>, PanelsError> {
    if let Some((_, dashboards)) = read_panels(path)? {
        return Ok(dashboards);
    }
    let seed: Value = serde_json::from_str(DEFAULT_DASHBOARDS)
        .map_err(|err| file_error(format!("the default dashboards are not JSON: {err}")))?;
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let tmp = path.with_file_name(format!("{name}.{}.tmp", std::process::id()));
    write_atomically(
        path,
        &tmp,
        &format!("{}\n", dumps_indent2(&Json::from(&seed))),
    )?;
    read_panels(path)?
        .map(|(_, dashboards)| dashboards)
        .ok_or_else(|| file_error(format!("{} vanished after it was written", path.display())))
}

/// `panel-history.json`: `{series: {"<panel id>:<hash12>": [{day, value} | {day, error}]}}`.
fn read_history(path: &Path) -> Result<Map<String, Value>, PanelsError> {
    let body = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
        Err(err) => {
            return Err(file_error(format!("cannot read {}: {err}", path.display())));
        }
    };
    let raw: Value = std::str::from_utf8(&body)
        .map_err(|err| err.to_string())
        .and_then(|body| serde_json::from_str(body).map_err(|err| err.to_string()))
        .map_err(|err| file_error(format!("{} is not valid JSON: {err}", path.display())))?;
    let series = raw.get("series").and_then(Value::as_object);
    let well_formed = series.is_some_and(|series| {
        series.values().all(|entries| {
            entries.as_array().is_some_and(|entries| {
                entries
                    .iter()
                    .all(|entry| entry.get("day").is_some_and(Value::is_string))
            })
        })
    });
    match series {
        Some(series) if well_formed => Ok(series.clone()),
        _ => Err(file_error(format!(
            "{} does not hold a series map of day entries",
            path.display()
        ))),
    }
}

/// Put each key's entry in its series: the one for that entry's day is replaced, else the
/// entry is appended. One atomic write for the lot.
fn record_history(path: &Path, results: &[(String, Value)]) -> Result<(), PanelsError> {
    let mut series = read_history(path)?;
    for (key, entry) in results {
        let day = entry.get("day").cloned().unwrap_or(Value::Null);
        let held: Vec<Value> = series
            .get(key)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let same_day_value = held
            .iter()
            .any(|e| e.get("day") == Some(&day) && e.get("value").is_some());
        if entry.get("error").is_some() && same_day_value {
            continue; // a rerun that failed never erases the day's recorded number
        }
        let mut entries: Vec<Value> = held
            .into_iter()
            .filter(|e| e.get("day") != Some(&day))
            .collect();
        entries.push(entry.clone());
        entries.sort_by(|a, b| {
            a.get("day")
                .and_then(Value::as_str)
                .cmp(&b.get("day").and_then(Value::as_str))
        });
        series.insert(key.clone(), Value::Array(entries));
    }
    let document = Json::object([("series", Json::from(&Value::Object(series)))]);
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    write_atomically(
        path,
        &path.with_file_name(name),
        &format!("{}\n", dumps_indent2(&document)),
    )
}

/// The first row of a result and how it was cut short, for [`measure`].
struct FirstRow {
    columns: Vec<String>,
    first: Option<Vec<Option<Number>>>,
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

/// Run one SELECT against a fresh read-only connection, the Query tab's way. Only the
/// first row is kept: a panel reads one number from it.
fn run_query(store: &Path, sql: &str) -> Result<FirstRow, MeasureError> {
    if py_strip(sql).is_empty() {
        return Err(QueryError("no SQL to run".into()).into());
    }
    let conn = open_readonly(store).map_err(MeasureError::Store)?;
    let deadline = Instant::now() + Duration::from_secs(QUERY_SECONDS);
    let timed_out = Arc::new(AtomicBool::new(false));
    let refused = |err: rusqlite::Error| {
        if timed_out.load(Ordering::Relaxed) {
            return QueryError(format!("query stopped after {QUERY_SECONDS} seconds"));
        }
        match err {
            // Python's sqlite3 module refuses a second statement before SQLite sees it.
            rusqlite::Error::MultipleStatement => {
                QueryError("You can only execute one statement at a time.".into())
            }
            // `Display` adds the statement and offset; Python's message is SQLite's alone.
            rusqlite::Error::SqlInputError { msg, .. } => QueryError(msg),
            other => QueryError(other.to_string()),
        }
    };
    conn.execute_batch("PRAGMA query_only = ON; PRAGMA temp_store = FILE")
        .map_err(refused)?;
    conn.set_limit(Limit::SQLITE_LIMIT_LENGTH, QUERY_VALUE_BYTES);
    conn.authorizer(Some(|ctx: rusqlite::hooks::AuthContext<'_>| {
        match ctx.action {
            AuthAction::Select
            | AuthAction::Read { .. }
            | AuthAction::Function { .. }
            | AuthAction::Recursive => Authorization::Allow,
            _ => Authorization::Deny,
        }
    }));
    let flag = Arc::clone(&timed_out);
    conn.progress_handler(
        PROGRESS_OPS,
        Some(move || {
            let past = Instant::now() > deadline;
            flag.store(past, Ordering::Relaxed);
            past
        }),
    );
    let mut stmt = conn.prepare(sql).map_err(refused)?;
    // SQLite prepares the first statement and leaves the rest; Python's sqlite3 refuses
    // anything but whitespace and comments after it, so a panel's error reads the same.
    // A panel binds nothing, so a parameter is Python's "bindings" error. The check comes
    // first because `expanded_sql` below rewrites a parameter as `NULL`, which moves the tail.
    if stmt.parameter_count() > 0 {
        return Err(QueryError(format!(
            "Incorrect number of bindings supplied. The current statement uses {}, and there \
             are 0 supplied.",
            stmt.parameter_count()
        ))
        .into());
    }
    if let Some(prepared) = stmt.expanded_sql()
        && has_more_sql(sql.get(prepared.len()..).unwrap_or_default())
    {
        return Err(refused(rusqlite::Error::MultipleStatement).into());
    }
    if stmt.column_count() == 0 {
        return Err(QueryError("the statement returns no rows".into()).into());
    }
    let columns: Vec<String> = stmt
        .column_names()
        .into_iter()
        .map(str::to_string)
        .collect();
    let mut rows = stmt.query([]).map_err(refused)?;
    let mut first = None;
    let mut fetched = 0;
    // One row past the cap, as the Query tab fetches it, so a statement that sorts or
    // scans meets the same deadline.
    while fetched <= QUERY_ROWS {
        let Some(row) = rows.next().map_err(refused)? else {
            break;
        };
        if first.is_none() {
            let cells = (0..columns.len())
                .map(|at| match row.get_ref(at) {
                    Ok(ValueRef::Integer(n)) => Ok(Some(Number::Int(n))),
                    Ok(ValueRef::Real(x)) if x.is_finite() => Ok(Some(Number::Real(x))),
                    Ok(_) => Ok(None),
                    Err(err) => Err(err),
                })
                .collect::<Result<Vec<_>, _>>()
                .map_err(refused)?;
            first = Some(cells);
        }
        fetched += 1;
    }
    Ok(FirstRow { columns, first })
}

/// The panel's value now. Anything that is not one number is a [`QueryError`].
fn measure(store: &Path, panel: &Panel) -> Result<Number, MeasureError> {
    let Some(column) = panel.column() else {
        let body = py_strip(&panel.sql).trim_end_matches(';');
        let result = run_query(store, &format!("SELECT COUNT(*) FROM (\n{body}\n)"))?;
        return result
            .first
            .and_then(|row| row.first().copied().flatten())
            .ok_or_else(|| QueryError("the count is not a number".into()).into());
    };
    let result = run_query(store, &panel.sql)?;
    let Some(row) = result.first else {
        return Err(QueryError("the query returned no rows".into()).into());
    };
    let Some(at) = result.columns.iter().position(|name| name == column) else {
        return Err(QueryError(format!("the first row has no column {column}")).into());
    };
    row[at].ok_or_else(|| QueryError(format!("column {column} is not a number")).into())
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct RecordStats {
    pub recorded: usize,
    pub failed: usize,
}

/// Record every panel's value for `day`, seeding the panels file first when it is missing.
/// A panel whose query fails records `{day, error}` and the rest still run.
pub fn record_panels(
    store: &Path,
    panels_path: &Path,
    history_path: &Path,
    day: &str,
    log: &mut dyn FnMut(&str),
) -> Result<RecordStats, PanelsError> {
    let mut stats = RecordStats::default();
    let mut results: Vec<(String, Value)> = Vec::new();
    for dashboard in read_or_seed(panels_path)? {
        for panel in &dashboard.panels {
            let key = series_key(panel);
            let started = Instant::now();
            let (entry, shown) = match measure(store, panel) {
                Ok(value) => {
                    stats.recorded += 1;
                    let entry = serde_json::json!({"day": day, "value": value.json()});
                    (entry, format!("value {}", value.shown()))
                }
                Err(MeasureError::Query(QueryError(message))) => {
                    stats.failed += 1;
                    let entry = serde_json::json!({"day": day, "error": message});
                    (entry, format!("error {message}"))
                }
                Err(MeasureError::Store(err)) => return Err(err.into()),
            };
            let elapsed_ms = started.elapsed().as_millis();
            match results.iter_mut().find(|(held, _)| *held == key) {
                Some((_, slot)) => *slot = entry,
                None => results.push((key.clone(), entry)),
            }
            log(&format!(
                "panel {}/{} {key}: {shown} ({elapsed_ms} ms)",
                dashboard.id, panel.id
            ));
        }
    }
    if !results.is_empty() {
        record_history(history_path, &results)?;
    }
    Ok(stats)
}

pub fn run(args: &[String]) -> Result<(), Failure> {
    if !args.is_empty() {
        return Err(Failure::Usage(format!(
            "{NAME} takes no arguments, got {}",
            args.join(" ")
        )));
    }
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let history: PathBuf = config.decisions_file(HISTORY_FILE);
    let day = Now::read().iso()[..10].to_string();
    let stats = record_panels(
        &config.store_path,
        &config.decisions_file(PANELS_FILE),
        &history,
        &day,
        &mut |line| println!("{line}"),
    )
    .map_err(|err| match err {
        PanelsError::Store(err) => crate::steps::failure(err),
        other => Failure::Failed(other.to_string()),
    })?;
    println!(
        "panels: {} recorded, {} failed, in {}",
        stats.recorded,
        stats.failed,
        history.display()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    const DAY1: &str = "2026-10-01";
    const DAY2: &str = "2026-10-02";

    fn store(dir: &Path) -> PathBuf {
        let path = dir.join("plexdb.db");
        let conn = Connection::open(&path).expect("store");
        conn.execute_batch(
            "CREATE TABLE items (item_id TEXT PRIMARY KEY, type TEXT, title TEXT);
             INSERT INTO items VALUES ('imdb:tt1', 'movie', 'A'), ('imdb:tt2', 'movie', 'B'),
                                      ('imdb:tt3', 'movie', 'C');",
        )
        .expect("rows");
        path
    }

    fn panel(id: &str, sql: &str, value: &str) -> Value {
        serde_json::json!({
            "id": id, "title": id, "sql": sql, "chart": "line", "value": value,
            "width": 2, "height": "short"
        })
    }

    fn panels_file(dir: &Path, panels: &[Value]) -> PathBuf {
        let path = dir.join(PANELS_FILE);
        let body = serde_json::json!({"dashboards": [{"id": "d", "name": "D", "panels": panels}]});
        fs::write(&path, body.to_string()).expect("panels file");
        path
    }

    fn record(
        dir: &Path,
        panels: &Path,
        day: &str,
    ) -> Result<(RecordStats, Vec<String>), PanelsError> {
        let mut lines = Vec::new();
        let stats = record_panels(
            &dir.join("plexdb.db"),
            panels,
            &dir.join(HISTORY_FILE),
            day,
            &mut |line| lines.push(line.to_string()),
        )?;
        Ok((stats, lines))
    }

    fn series_of(dir: &Path, id: &str) -> Vec<Value> {
        read_history(&dir.join(HISTORY_FILE))
            .expect("history")
            .into_iter()
            .filter(|(key, _)| key.starts_with(&format!("{id}:")))
            .map(|(_, entries)| entries)
            .collect()
    }

    #[test]
    fn a_rows_panel_and_a_column_panel_each_record_their_value() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let panels = panels_file(
            dir.path(),
            &[
                panel("count", "SELECT item_id FROM items", "rows"),
                panel(
                    "peak",
                    "SELECT COUNT(*) AS n FROM items WHERE title <> 'C'",
                    "column:n",
                ),
            ],
        );
        let (stats, _) = record(dir.path(), &panels, DAY1).expect("record");
        assert_eq!(
            stats,
            RecordStats {
                recorded: 2,
                failed: 0
            }
        );
        assert_eq!(
            series_of(dir.path(), "count"),
            [serde_json::json!([{"day": DAY1, "value": 3}])]
        );
        assert_eq!(
            series_of(dir.path(), "peak"),
            [serde_json::json!([{"day": DAY1, "value": 2}])]
        );
    }

    #[test]
    fn a_second_run_on_the_same_day_replaces_that_days_entry() {
        let dir = tempfile::tempdir().expect("dir");
        let db = store(dir.path());
        let panels = panels_file(
            dir.path(),
            &[panel("count", "SELECT item_id FROM items", "rows")],
        );
        record(dir.path(), &panels, DAY1).expect("first");
        Connection::open(&db)
            .expect("store")
            .execute("INSERT INTO items VALUES ('imdb:tt4', 'movie', 'D')", [])
            .expect("insert");
        record(dir.path(), &panels, DAY1).expect("second");
        assert_eq!(
            series_of(dir.path(), "count"),
            [serde_json::json!([{"day": DAY1, "value": 4}])]
        );
        record(dir.path(), &panels, DAY2).expect("next day");
        assert_eq!(
            series_of(dir.path(), "count"),
            [serde_json::json!([{"day": DAY1, "value": 4}, {"day": DAY2, "value": 4}])]
        );
    }

    #[test]
    fn a_failed_rerun_keeps_the_days_recorded_value() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let panels = panels_file(
            dir.path(),
            &[panel("p", "SELECT item_id FROM items", "rows")],
        );
        record(dir.path(), &panels, DAY1).expect("record");
        let history = dir.path().join(HISTORY_FILE);
        let key = read_history(&history)
            .expect("history")
            .keys()
            .next()
            .cloned();
        let key = key.expect("one series");
        let failed = serde_json::json!({"day": DAY1, "error": "query stopped after 5 seconds"});
        record_history(&history, &[(key.clone(), failed)]).expect("rewrite");
        assert_eq!(
            read_history(&history).expect("history")[&key],
            serde_json::json!([{"day": DAY1, "value": 3}])
        );
    }

    #[test]
    fn editing_a_panels_sql_starts_a_second_series() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let first = panels_file(
            dir.path(),
            &[panel("p", "SELECT item_id FROM items", "rows")],
        );
        record(dir.path(), &first, DAY1).expect("first");
        let edited = panels_file(
            dir.path(),
            &[panel(
                "p",
                "SELECT item_id FROM items WHERE title = 'A'",
                "rows",
            )],
        );
        record(dir.path(), &edited, DAY1).expect("edited");
        let mut values: Vec<i64> = series_of(dir.path(), "p")
            .iter()
            .map(|entries| entries[0]["value"].as_i64().expect("number"))
            .collect();
        values.sort_unstable();
        assert_eq!(values, [1, 3]);
    }

    #[test]
    fn a_failing_panel_records_an_error_and_the_others_still_record() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let panels = panels_file(
            dir.path(),
            &[
                panel("bad", "SELECT nope FROM items", "rows"),
                panel(
                    "none",
                    "SELECT n FROM (SELECT 1 AS n) WHERE n = 2",
                    "column:n",
                ),
                panel("nocol", "SELECT 1 AS n", "column:m"),
                panel(
                    "text",
                    "SELECT title AS t FROM items ORDER BY title",
                    "column:t",
                ),
                panel("write", "DELETE FROM items", "column:n"),
                panel("good", "SELECT item_id FROM items", "rows"),
            ],
        );
        let (stats, _) = record(dir.path(), &panels, DAY1).expect("record");
        assert_eq!(
            stats,
            RecordStats {
                recorded: 1,
                failed: 5
            }
        );
        let error = |id: &str| series_of(dir.path(), id)[0][0]["error"].to_string();
        assert!(error("bad").contains("no such column"), "{}", error("bad"));
        assert_eq!(error("none"), "\"the query returned no rows\"");
        assert_eq!(error("nocol"), "\"the first row has no column m\"");
        assert_eq!(error("text"), "\"column t is not a number\"");
        assert!(
            error("write").contains("not authorized"),
            "{}",
            error("write")
        );
        assert_eq!(
            series_of(dir.path(), "good"),
            [serde_json::json!([{"day": DAY1, "value": 3}])]
        );
        let held: i64 = Connection::open(dir.path().join("plexdb.db"))
            .expect("store")
            .query_row("SELECT COUNT(*) FROM items", [], |row| row.get(0))
            .expect("count");
        assert_eq!(held, 3);
    }

    #[test]
    fn a_real_valued_column_is_recorded_as_python_writes_a_float() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let panels = panels_file(
            dir.path(),
            &[panel("avg", "SELECT 2.0 AS n, 1e16 AS big", "column:n")],
        );
        record(dir.path(), &panels, DAY1).expect("record");
        let body = fs::read_to_string(dir.path().join(HISTORY_FILE)).expect("history");
        assert!(body.contains("\"value\": 2.0"), "{body}");
    }

    #[test]
    fn a_missing_panels_file_is_seeded_with_the_five_library_health_panels() {
        let dir = tempfile::tempdir().expect("dir");
        let path = dir.path().join(PANELS_FILE);
        let dashboards = read_or_seed(&path).expect("seed");
        assert_eq!(dashboards.len(), 1);
        assert_eq!(
            dashboards[0]
                .panels
                .iter()
                .map(|panel| panel.id.as_str())
                .collect::<Vec<_>>(),
            [
                "damaged-files",
                "english-no-english-subtitle",
                "decode-progress",
                "tracks-per-language",
                "extras-with-more-than-one-file"
            ]
        );
        let written: Value =
            serde_json::from_str(&fs::read_to_string(&path).expect("file")).expect("json");
        let default: Value = serde_json::from_str(DEFAULT_DASHBOARDS).expect("default");
        assert_eq!(written, default);
    }

    #[test]
    fn a_statement_past_its_deadline_is_stopped_and_recorded_as_an_error() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let panels = panels_file(
            dir.path(),
            &[panel(
                "forever",
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) \
                 SELECT COUNT(*) AS n FROM c",
                "column:n",
            )],
        );
        let started = Instant::now();
        let (stats, _) = record(dir.path(), &panels, DAY1).expect("record");
        assert_eq!(
            stats,
            RecordStats {
                recorded: 0,
                failed: 1
            }
        );
        assert_eq!(
            series_of(dir.path(), "forever")[0][0]["error"],
            "query stopped after 5 seconds"
        );
        assert!(started.elapsed() < Duration::from_secs(30));
    }

    #[test]
    fn a_panel_with_a_parameter_records_pythons_bindings_error() {
        let dir = tempfile::tempdir().expect("dir");
        store(dir.path());
        let panels = panels_file(dir.path(), &[panel("p", "SELECT ? AS n", "column:n")]);
        record(dir.path(), &panels, DAY1).expect("record");
        assert_eq!(
            series_of(dir.path(), "p")[0][0]["error"],
            "Incorrect number of bindings supplied. The current statement uses 1, and there are 0 supplied."
        );
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
    fn a_panels_file_with_no_panels_is_never_reseeded() {
        let dir = tempfile::tempdir().expect("dir");
        let path = dir.path().join(PANELS_FILE);
        fs::write(&path, r#"{"dashboards": []}"#).expect("file");
        assert!(read_or_seed(&path).expect("read").is_empty());
    }

    #[test]
    fn the_sweep_seeds_a_missing_panels_file_and_records_its_panels() {
        let dir = tempfile::tempdir().expect("dir");
        let db = dir.path().join("plexdb.db");
        Connection::open(&db)
            .expect("store")
            .execute_batch(
                "CREATE TABLE items (item_id TEXT PRIMARY KEY, type TEXT, title TEXT,
                                      show_item_id TEXT);
                 CREATE TABLE enrichment (item_id TEXT, namespace TEXT, key TEXT, value TEXT);
                 CREATE TABLE media_part (part_id INTEGER, item_id TEXT, file TEXT);
                 CREATE TABLE media_stream (part_id INTEGER, kind TEXT, lang TEXT);
                 CREATE TABLE media_check (part_id INTEGER, kind TEXT, ok INTEGER,
                                           error TEXT, checked_at TEXT);
                 CREATE TABLE extra_item (item_id TEXT, library TEXT);
                 CREATE TABLE extra_file (item_id TEXT, path TEXT, missing_since TEXT);",
            )
            .expect("schema");
        let (stats, lines) = record(dir.path(), &dir.path().join(PANELS_FILE), DAY1).expect("run");
        assert_eq!(
            stats,
            RecordStats {
                recorded: 5,
                failed: 0
            }
        );
        assert_eq!(lines.len(), 5);
        assert_eq!(
            read_history(&dir.path().join(HISTORY_FILE))
                .expect("history")
                .len(),
            5
        );
    }

    #[test]
    fn a_chart_missing_the_settings_it_draws_from_is_refused() {
        let dir = tempfile::tempdir().expect("dir");
        for (chart, settings) in [
            ("bar", serde_json::json!({"label": "lang"})),
            ("bar", serde_json::json!({"number": "n"})),
            ("gauge", serde_json::json!({})),
        ] {
            let mut entry = panel("x", "SELECT 1", "rows");
            entry["chart"] = Value::String(chart.into());
            for (field, value) in settings.as_object().expect("object") {
                entry[field] = value.clone();
            }
            let path = panels_file(dir.path(), &[entry]);
            let err = read_panels(&path).expect_err("refused");
            assert!(err.to_string().contains("needs text"), "{err}");
        }
    }

    #[test]
    fn a_malformed_panels_file_is_refused_whole() {
        let dir = tempfile::tempdir().expect("dir");
        let path = panels_file(dir.path(), &[serde_json::json!({"id": "x"})]);
        assert!(read_panels(&path).is_err());
    }

    #[test]
    fn series_key_names_the_series_the_explorer_reads() {
        // The dashboard finds a panel's series by this key: a contract.
        let key = series_key(&Panel {
            id: "a".into(),
            sql: "SELECT 1".into(),
            value: "rows".into(),
        });
        assert_eq!(key, "a:2ae978d15fa2");
    }
}
