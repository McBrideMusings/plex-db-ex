//! `plexdb record-panels` (ADR-0029): run every dashboard panel's SQL once and keep the
//! number in `panel-history.json` ([`crate::panels`] holds both files' shapes).
//!
//! The panel's SQL runs the way the Query tab runs a statement ([`run_query`]): a fresh
//! read-only connection, `query_only`, an authorizer that allows reads and nothing else, and
//! a deadline checked as the statement runs.

use std::path::{Path, PathBuf};
use std::time::Instant;

use serde_json::Value;

use plexdb_store::{Config, StoreError, py_strip};

use crate::panels::{
    HISTORY_FILE, PANELS_FILE, Panel, PanelsError, read_history, read_or_seed, series_key,
    write_history,
};
use crate::query::{Cell, QueryError, run_query};
use crate::staleness::Now;
use crate::steps::Failure;

pub const NAME: &str = "record-panels";

enum MeasureError {
    Query(String),
    Store(StoreError),
}

impl From<QueryError> for MeasureError {
    fn from(err: QueryError) -> Self {
        match err {
            QueryError::Refused(message) | QueryError::Busy(message) => {
                MeasureError::Query(message)
            }
            QueryError::Store(err) => MeasureError::Store(err),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Number {
    Int(i64),
    Real(f64),
}

impl Number {
    /// A cell that is one number: an integer, or a finite real.
    fn of(cell: &Cell) -> Option<Number> {
        match cell {
            Cell::Int(n) => Some(Number::Int(*n)),
            Cell::Real(x) if x.is_finite() => Some(Number::Real(*x)),
            _ => None,
        }
    }

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
    write_history(path, series)
}

/// The panel's value now. Anything that is not one number is a query error.
fn measure(store: &Path, panel: &Panel) -> Result<Number, MeasureError> {
    let not = |message: String| MeasureError::Query(message);
    let Some(column) = panel.column() else {
        let body = py_strip(&panel.sql).trim_end_matches(';');
        let ran = run_query(store, &format!("SELECT COUNT(*) FROM (\n{body}\n)"))?;
        return ran
            .rows
            .first()
            .and_then(|row| row.first())
            .and_then(Number::of)
            .ok_or_else(|| not("the count is not a number".into()));
    };
    let ran = run_query(store, &panel.sql)?;
    let Some(row) = ran.rows.first() else {
        return Err(not("the query returned no rows".into()));
    };
    let Some(at) = ran.columns.iter().position(|name| name == column) else {
        return Err(not(format!("the first row has no column {column}")));
    };
    Number::of(&row[at]).ok_or_else(|| not(format!("column {column} is not a number")))
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
                Err(MeasureError::Query(message)) => {
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
    use std::fs;
    use std::time::Duration;

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
                 CREATE TABLE extra_file (path TEXT, item_id TEXT, missing_since TEXT);
                 CREATE TABLE extra_check (path TEXT, kind TEXT, ok INTEGER,
                                           error TEXT, checked_at TEXT);",
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
}
