//! Dashboard panels and their nightly values (ADR-0029): `explore-panels.json` holds the Query
//! tab's dashboards, each a grid of panels, each panel a saved query with a chart, and
//! `panel-history.json` the number the sweep's `record-panels` keeps for each panel per night.
//!
//! Both files sit in the decisions directory, never in `plexdb.db`: a count this project
//! computed is not a fact about a title. A panel's series is named by [`series_key`], which
//! hashes the SQL and the value setting, so an edit starts a new series and a line never
//! joins two different measurements.
//!
//! The sweep reads the panels and writes the history; Plex DB-EX reads both and edits the
//! panels (`upsert`, `move_panel`, `put_dashboard`, `delete_panel`, `delete_dashboard`).
//! Every write replaces the file by rename, so a reader never sees half of one.

use std::fs;
use std::io::Read as _;
use std::path::Path;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;

use plexdb_store::{StoreError, py_strip};

use crate::decisions::py_repr;
use crate::explore_views::ExploreError;
use crate::pyjson::{Json, dumps_indent2};
use crate::query::{QueryError, QuerySlots};

pub const PANELS_FILE: &str = "explore-panels.json";
pub const HISTORY_FILE: &str = "panel-history.json";

/// The Library health dashboard a missing panels file is first written with. An existing
/// file, even one with no dashboards, is never seeded again. The explorer's Python seeds
/// from the same panels until it moves; `tests/test_panels.py` holds the two together.
pub const DEFAULT_DASHBOARDS: &str = include_str!("default_dashboards.json");

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
/// The fields a panel keeps, in the order the file holds them, then its display settings.
const PANEL_FIELDS: &[&str] = &["id", "title", "sql", "chart", "value", "width", "height"];
const OPTIONAL: &[&str] = &["total", "label", "number", "unit", "x", "y"];
/// An edit is refused when it would grow the panels file past this: Plex DB-EX has no login.
const MAX_TOTAL_BYTES: usize = 1 << 20;
/// The longest dashboard name [`put_dashboard`] keeps.
const MAX_NAME: usize = 80;

#[derive(Debug, Error)]
pub enum PanelsError {
    /// `explore-panels.json` or `panel-history.json` is unreadable or not the shape.
    #[error("{0}")]
    File(String),
    #[error(transparent)]
    Store(#[from] StoreError),
}

impl From<PanelsError> for ExploreError {
    fn from(err: PanelsError) -> Self {
        match err {
            PanelsError::File(message) => ExploreError::Internal(format!("panels file: {message}")),
            PanelsError::Store(err) => ExploreError::from(err),
        }
    }
}

#[derive(Debug)]
pub struct Panel {
    pub id: String,
    pub title: String,
    pub sql: String,
    pub chart: String,
    /// `rows` or `column:<name>`.
    pub value: String,
    pub width: i64,
    pub height: String,
    /// Display settings the dashboard reads; the sweep never does.
    pub options: Vec<(String, Value)>,
}

impl Panel {
    /// The column `value` names, or `None` when it counts rows.
    pub fn column(&self) -> Option<&str> {
        self.value.strip_prefix(COLUMN_PREFIX)
    }
}

#[derive(Debug)]
pub struct Dashboard {
    pub id: String,
    pub name: String,
    pub panels: Vec<Panel>,
}

/// `<panel id>:<first 12 hex of sha256(sql NUL value)>`, the name of the panel's series.
pub fn series_key(panel: &Panel) -> String {
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
    let name = text(&where_, entry, "name")?;
    let panels = entry["panels"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .enumerate()
        .map(|(i, panel)| parse_panel(&format!("{where_} panel {i}"), panel))
        .collect::<Result<_, _>>()?;
    Ok(Dashboard { id, name, panels })
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
            py_repr(&value)
        )));
    }
    let width = entry.get("width");
    let Some(width_value) = width
        .and_then(Value::as_i64)
        .filter(|width| (1..=4).contains(width))
    else {
        return Err(file_error(format!(
            "{where_} has width {}, expected 1 to 4",
            width.map_or_else(|| "None".into(), py_json_repr)
        )));
    };
    let height = entry.get("height");
    let Some(height_value) = height
        .and_then(Value::as_str)
        .filter(|height| HEIGHTS.contains(height))
    else {
        return Err(file_error(format!(
            "{where_} has height {}, expected one of ('short', 'tall')",
            height.map_or_else(|| "None".into(), py_json_repr)
        )));
    };
    let chart = text(where_, entry, "chart")?;
    if !CHARTS.contains(&chart.as_str()) {
        return Err(file_error(format!(
            "{where_} has chart {}, expected one of ('line', 'stat', 'gauge', 'bar', 'pie', \
             'table', 'heatmap')",
            py_repr(&chart)
        )));
    }
    for (_, needed) in CHART_SETTINGS.iter().filter(|(name, _)| *name == chart) {
        for field in *needed {
            text(where_, entry, field)?;
        }
    }
    let id = text(where_, entry, "id")?;
    let title = text(where_, entry, "title")?;
    let sql = text(where_, entry, "sql")?;
    Ok(Panel {
        id,
        title,
        sql,
        chart,
        value,
        width: width_value,
        height: height_value.to_string(),
        options: OPTIONAL
            .iter()
            .filter_map(|field| entry.get(*field).map(|v| (field.to_string(), v.clone())))
            .collect(),
    })
}

/// How Python's `repr` writes a parsed JSON value in an error message.
fn py_json_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(text) => py_repr(text),
        other => other.to_string(),
    }
}

/// The raw file and its dashboards, or `None` when there is no file. A file that is not
/// valid JSON or breaks the shape is refused.
pub fn read_panels(path: &Path) -> Result<Option<(Value, Vec<Dashboard>)>, PanelsError> {
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
pub fn write_atomically(path: &Path, tmp: &Path, body: &str) -> Result<(), PanelsError> {
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

fn seed() -> Result<Value, PanelsError> {
    serde_json::from_str(DEFAULT_DASHBOARDS)
        .map_err(|err| file_error(format!("the default dashboards are not JSON: {err}")))
}

fn write_panels(path: &Path, raw: &Value) -> Result<(), PanelsError> {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let tmp = path.with_file_name(format!("{name}.{}.tmp", std::process::id()));
    write_atomically(
        path,
        &tmp,
        &format!("{}\n", dumps_indent2(&Json::from(raw))),
    )
}

/// The dashboards; a missing file is first written with the default dashboards. A file
/// that exists is read as it is, so one with no dashboards stays empty.
pub fn read_or_seed(path: &Path) -> Result<Vec<Dashboard>, PanelsError> {
    if let Some((_, dashboards)) = read_panels(path)? {
        return Ok(dashboards);
    }
    write_panels(path, &seed()?)?;
    read_panels(path)?
        .map(|(_, dashboards)| dashboards)
        .ok_or_else(|| file_error(format!("{} vanished after it was written", path.display())))
}

/// `panel-history.json`: `{series: {"<panel id>:<hash12>": [{day, value} | {day, error}]}}`.
pub fn read_history(path: &Path) -> Result<Map<String, Value>, PanelsError> {
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

/// Replace the history file with `series`.
pub fn write_history(path: &Path, series: Map<String, Value>) -> Result<(), PanelsError> {
    let document = Json::object([("series", Json::from(&Value::Object(series)))]);
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    write_atomically(
        path,
        &path.with_file_name(format!("{name}.{}.tmp", std::process::id())),
        &format!("{}\n", dumps_indent2(&document)),
    )
}

/// Remove every series whose key starts with `<panel id>:`; returns how many went.
pub fn drop_panel(path: &Path, panel_id: &str) -> Result<usize, PanelsError> {
    let mut series = read_history(path)?;
    let prefix = format!("{panel_id}:");
    let before = series.len();
    series.retain(|key, _| !key.starts_with(&prefix));
    let gone = before - series.len();
    if gone > 0 {
        write_history(path, series)?;
    }
    Ok(gone)
}

// ---------------------------------------------------------------------------------------
// Plex DB-EX's edits. The caller serializes them.

fn invalid(message: impl Into<String>) -> ExploreError {
    ExploreError::Invalid(message.into())
}

fn missing(what: String) -> ExploreError {
    ExploreError::NotFound(format!("no {what} in the panels file"))
}

/// The raw file for an edit, seeded first when it is missing. The whole file must hold the
/// shape before an edit rewrites it.
fn edit(path: &Path) -> Result<Value, PanelsError> {
    match read_panels(path)? {
        Some((raw, _)) => Ok(raw),
        None => seed(),
    }
}

fn save(path: &Path, raw: &Value) -> Result<(), ExploreError> {
    let body = format!("{}\n", dumps_indent2(&Json::from(raw)));
    if body.len() > MAX_TOTAL_BYTES {
        return Err(invalid(format!(
            "the panels would exceed {MAX_TOTAL_BYTES} bytes"
        )));
    }
    Ok(write_panels(path, raw)?)
}

fn dashboards_mut(raw: &mut Value) -> &mut Vec<Value> {
    raw.get_mut("dashboards")
        .and_then(Value::as_array_mut)
        .expect("checked by read_panels")
}

fn panels_of<'a>(
    raw: &'a mut Value,
    dashboard_id: &str,
) -> Result<&'a mut Vec<Value>, ExploreError> {
    dashboards_mut(raw)
        .iter_mut()
        .find(|d| d["id"] == dashboard_id)
        .and_then(|d| d.get_mut("panels").and_then(Value::as_array_mut))
        .ok_or_else(|| missing(format!("dashboard {dashboard_id}")))
}

/// A new id: `uuid.uuid4().hex`.
fn new_id() -> Result<String, ExploreError> {
    let mut bytes = [0u8; 16];
    fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|err| ExploreError::Internal(format!("panels file: no random id: {err}")))?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Create `panel` in the dashboard (no `id`: a new one, appended) or replace the one with
/// its `id` in place. Returns the stored panel.
pub fn upsert(
    path: &Path,
    dashboard_id: Option<&Value>,
    panel: &Map<String, Value>,
) -> Result<Value, ExploreError> {
    let Some(Value::String(dashboard_id)) = dashboard_id else {
        return Err(invalid("dashboard_id must be text and panel an object"));
    };
    let mut entry = Map::new();
    for field in PANEL_FIELDS.iter().chain(OPTIONAL) {
        if let Some(value) = panel.get(*field) {
            entry.insert(field.to_string(), value.clone());
        }
    }
    let given = entry.get("id").filter(|id| !id.is_null()).cloned();
    let id = match &given {
        Some(id) => id.clone(),
        None => Value::String(new_id()?),
    };
    entry.insert("id".into(), id);
    match &entry["id"] {
        Value::String(id) if !id.contains(':') => {}
        _ => return Err(invalid("id must be text without a colon")),
    }
    let entry = Value::Object(entry);
    if let Err(err) = parse_panel("panel", &entry) {
        return Err(invalid(err.to_string()));
    }
    for field in OPTIONAL {
        if let Some(value) = entry.get(*field)
            && !matches!(value, Value::String(text) if !text.is_empty())
        {
            return Err(invalid(format!("{field} must be text")));
        }
    }
    let mut raw = edit(path)?;
    let panels = panels_of(&mut raw, dashboard_id)?;
    match panels.iter().position(|p| p["id"] == entry["id"]) {
        Some(at) => panels[at] = entry.clone(),
        None => match &given {
            Some(given) => {
                return Err(missing(format!(
                    "panel {}",
                    given.as_str().unwrap_or_default()
                )));
            }
            None => panels.push(entry.clone()),
        },
    }
    save(path, &raw)?;
    Ok(entry)
}

/// Put the panel at index `to` of its dashboard; returns the dashboard's panel ids.
pub fn move_panel(
    path: &Path,
    dashboard_id: Option<&Value>,
    panel_id: Option<&Value>,
    to: Option<&Value>,
) -> Result<Vec<String>, ExploreError> {
    let (Some(Value::String(dashboard_id)), Some(Value::String(panel_id))) =
        (dashboard_id, panel_id)
    else {
        return Err(invalid("dashboard_id and panel_id must be text"));
    };
    let to = match to {
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => n.as_i64(),
        _ => return Err(invalid("to must be a whole number")),
    };
    let mut raw = edit(path)?;
    let panels = panels_of(&mut raw, dashboard_id)?;
    let Some(at) = panels.iter().position(|p| p["id"] == panel_id.as_str()) else {
        return Err(missing(format!("panel {panel_id}")));
    };
    let Some(to) = to
        .and_then(|to| usize::try_from(to).ok())
        .filter(|to| *to < panels.len())
    else {
        return Err(invalid(format!(
            "to must be 0 to {}",
            panels.len() as i64 - 1
        )));
    };
    let moving = panels.remove(at);
    panels.insert(to, moving);
    let order = panels
        .iter()
        .map(|p| p["id"].as_str().unwrap_or_default().to_string())
        .collect();
    save(path, &raw)?;
    Ok(order)
}

/// Create a dashboard (no `dashboard_id`: a new id, appended, with no panels) or rename the
/// one with that id. Returns its id and name.
pub fn put_dashboard(
    path: &Path,
    dashboard_id: Option<&Value>,
    name: Option<&Value>,
) -> Result<(String, String), ExploreError> {
    let name = match name {
        Some(Value::String(name)) if !py_strip(name).is_empty() => py_strip(name).to_string(),
        _ => return Err(invalid("name must be text")),
    };
    if name.chars().count() > MAX_NAME {
        return Err(invalid(format!("name is over {MAX_NAME} characters")));
    }
    let dashboard_id = match dashboard_id {
        None | Some(Value::Null) => None,
        Some(Value::String(id)) if !id.is_empty() && !id.contains(':') => Some(id.clone()),
        Some(_) => return Err(invalid("id must be text without a colon")),
    };
    let mut raw = edit(path)?;
    let id = match dashboard_id {
        None => {
            let id = new_id()?;
            let mut entry = Map::new();
            entry.insert("id".into(), Value::String(id.clone()));
            entry.insert("name".into(), Value::String(name.clone()));
            entry.insert("panels".into(), Value::Array(Vec::new()));
            dashboards_mut(&mut raw).push(Value::Object(entry));
            id
        }
        Some(id) => {
            let Some(entry) = dashboards_mut(&mut raw)
                .iter_mut()
                .find(|d| d["id"] == id.as_str())
            else {
                return Err(missing(format!("dashboard {id}")));
            };
            entry["name"] = Value::String(name.clone());
            id
        }
    };
    save(path, &raw)?;
    Ok((id, name))
}

/// Remove the dashboard; returns the ids of the panels that went with it. The last
/// dashboard is refused.
pub fn delete_dashboard(path: &Path, dashboard_id: &str) -> Result<Vec<String>, ExploreError> {
    let mut raw = edit(path)?;
    let dashboards = dashboards_mut(&mut raw);
    let Some(at) = dashboards.iter().position(|d| d["id"] == dashboard_id) else {
        return Err(missing(format!("dashboard {dashboard_id}")));
    };
    if dashboards.len() == 1 {
        return Err(invalid("the last dashboard cannot be deleted"));
    }
    let gone = dashboards.remove(at);
    save(path, &raw)?;
    Ok(gone["panels"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(|p| p["id"].as_str().unwrap_or_default().to_string())
        .collect())
}

/// Remove the panel from whichever dashboard holds it.
pub fn delete_panel(path: &Path, panel_id: &str) -> Result<(), ExploreError> {
    let mut raw = edit(path)?;
    for dashboard in dashboards_mut(&mut raw).iter_mut() {
        let Some(panels) = dashboard.get_mut("panels").and_then(Value::as_array_mut) else {
            continue;
        };
        let before = panels.len();
        panels.retain(|p| p["id"] != panel_id);
        if panels.len() != before {
            save(path, &raw)?;
            return Ok(());
        }
    }
    Err(missing(format!("panel {panel_id}")))
}

/// The `/api/dashboards` body: each panel with the rows its SQL returns now (`live`) and its
/// recorded `series`, oldest night first.
pub fn dashboards_json(
    store: &Path,
    slots: &QuerySlots,
    dashboards: &[Dashboard],
    history: &Map<String, Value>,
) -> Result<Json, ExploreError> {
    let mut out = Vec::new();
    for dashboard in dashboards {
        let mut panels = Vec::new();
        for panel in &dashboard.panels {
            let live = match slots.run(store, &panel.sql) {
                Ok(ran) => Json::object([
                    (
                        "columns",
                        Json::Array(ran.columns.iter().cloned().map(Json::Str).collect()),
                    ),
                    ("rows", ran.rows_json()),
                    ("truncated", Json::Bool(ran.truncated)),
                ]),
                Err(QueryError::Refused(message) | QueryError::Busy(message)) => {
                    Json::object([("error", Json::Str(message))])
                }
                Err(QueryError::Store(err)) => return Err(err.into()),
            };
            let mut fields: Vec<(String, Json)> = vec![
                ("id".into(), Json::str(&panel.id)),
                ("title".into(), Json::str(&panel.title)),
                ("sql".into(), Json::str(&panel.sql)),
                ("chart".into(), Json::str(&panel.chart)),
                ("value".into(), Json::str(&panel.value)),
                ("width".into(), Json::Int(panel.width)),
                ("height".into(), Json::str(&panel.height)),
            ];
            fields.extend(
                panel
                    .options
                    .iter()
                    .map(|(key, value)| (key.clone(), Json::from(value))),
            );
            fields.push(("live".into(), live));
            fields.push((
                "series".into(),
                history
                    .get(&series_key(panel))
                    .map_or(Json::Array(Vec::new()), Json::from),
            ));
            panels.push(Json::Object(fields));
        }
        out.push(Json::object([
            ("id", Json::str(&dashboard.id)),
            ("name", Json::str(&dashboard.name)),
            ("panels", Json::Array(panels)),
        ]));
    }
    Ok(Json::Array(out))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn panel(id: &str) -> Map<String, Value> {
        json!({"id": id, "title": id, "sql": "SELECT 1", "chart": "line", "value": "rows",
               "width": 2, "height": "short"})
        .as_object()
        .cloned()
        .expect("object")
    }

    fn file(dir: &Path, panels: &[Map<String, Value>]) -> std::path::PathBuf {
        let path = dir.join(PANELS_FILE);
        let panels: Vec<Value> = panels.iter().cloned().map(Value::Object).collect();
        let body = json!({"dashboards": [{"id": "d", "name": "D", "panels": panels},
                                         {"id": "e", "name": "E", "panels": []}]});
        fs::write(&path, body.to_string()).expect("panels file");
        path
    }

    fn ids(path: &Path, dashboard: usize) -> Vec<String> {
        read_panels(path).expect("read").expect("file").1[dashboard]
            .panels
            .iter()
            .map(|p| p.id.clone())
            .collect()
    }

    fn message(err: ExploreError) -> String {
        match err {
            ExploreError::Invalid(m) => format!("400 {m}"),
            ExploreError::NotFound(m) => format!("404 {m}"),
            ExploreError::Internal(m) => format!("500 {m}"),
            ExploreError::Busy(m) | ExploreError::Unavailable(m) => m,
        }
    }

    #[test]
    fn a_panel_without_an_id_is_appended_under_a_new_one_and_one_with_an_id_is_replaced() {
        let dir = tempfile::tempdir().expect("dir");
        let path = file(dir.path(), &[panel("a")]);
        let mut new = panel("x");
        new.remove("id");
        let stored = upsert(&path, Some(&json!("d")), &new).expect("stored");
        let id = stored["id"].as_str().expect("id").to_string();
        assert_eq!((id.len(), &id[12..13]), (32, "4"));
        // A new panel's id lands after its other fields, as `entry["id"] = …` puts it.
        assert_eq!(
            stored.as_object().expect("object").keys().next_back(),
            Some(&"id".into())
        );
        assert_eq!(ids(&path, 0), ["a".to_string(), id]);
        let mut edited = panel("a");
        edited.insert("title".into(), json!("renamed"));
        upsert(&path, Some(&json!("d")), &edited).expect("replaced");
        assert_eq!(
            read_panels(&path).expect("read").expect("file").1[0].panels[0].title,
            "renamed"
        );
        let err = upsert(&path, Some(&json!("d")), &panel("nope")).expect_err("unknown");
        assert_eq!(message(err), "404 no panel nope in the panels file");
        let err = upsert(&path, Some(&json!("zz")), &panel("a")).expect_err("no dashboard");
        assert_eq!(message(err), "404 no dashboard zz in the panels file");
        let mut bad = panel("a");
        bad.insert("width".into(), json!(9));
        let err = upsert(&path, Some(&json!("d")), &bad).expect_err("width");
        assert_eq!(message(err), "400 panel has width 9, expected 1 to 4");
    }

    #[test]
    fn moves_renames_and_deletes() {
        let dir = tempfile::tempdir().expect("dir");
        let path = file(dir.path(), &[panel("a"), panel("b"), panel("c")]);
        let order = move_panel(&path, Some(&json!("d")), Some(&json!("c")), Some(&json!(0)));
        assert_eq!(order.expect("moved"), ["c", "a", "b"]);
        let err = move_panel(&path, Some(&json!("d")), Some(&json!("a")), Some(&json!(3)));
        assert_eq!(message(err.expect_err("range")), "400 to must be 0 to 2");
        let err = move_panel(
            &path,
            Some(&json!("d")),
            Some(&json!("a")),
            Some(&json!(true)),
        );
        assert_eq!(
            message(err.expect_err("bool")),
            "400 to must be a whole number"
        );
        let (_, name) =
            put_dashboard(&path, Some(&json!("e")), Some(&json!("  Two  "))).expect("renamed");
        assert_eq!(name, "Two");
        delete_panel(&path, "a").expect("deleted");
        assert_eq!(ids(&path, 0), ["c", "b"]);
        assert_eq!(delete_dashboard(&path, "d").expect("gone"), ["c", "b"]);
        let err = delete_dashboard(&path, "e").expect_err("last");
        assert_eq!(message(err), "400 the last dashboard cannot be deleted");
    }

    #[test]
    fn dropping_a_panel_removes_only_its_series() {
        let dir = tempfile::tempdir().expect("dir");
        let path = dir.path().join(HISTORY_FILE);
        let series = json!({"a:1": [{"day": "2026-10-01", "value": 1}], "a:2": [], "ab:3": []});
        write_history(&path, series.as_object().cloned().expect("object")).expect("written");
        assert_eq!(drop_panel(&path, "a").expect("dropped"), 2);
        assert_eq!(
            read_history(&path)
                .expect("read")
                .keys()
                .collect::<Vec<_>>(),
            ["ab:3"]
        );
        assert_eq!(drop_panel(&path, "zz").expect("none"), 0);
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
    fn a_panels_file_with_no_panels_is_never_reseeded() {
        let dir = tempfile::tempdir().expect("dir");
        let path = dir.path().join(PANELS_FILE);
        fs::write(&path, r#"{"dashboards": []}"#).expect("file");
        assert!(read_or_seed(&path).expect("read").is_empty());
    }

    #[test]
    fn a_chart_missing_the_settings_it_draws_from_is_refused() {
        let dir = tempfile::tempdir().expect("dir");
        for (chart, settings) in [
            ("bar", json!({"label": "lang"})),
            ("bar", json!({"number": "n"})),
            ("gauge", json!({})),
        ] {
            let mut entry = panel("x");
            entry.insert("chart".into(), Value::String(chart.into()));
            for (field, value) in settings.as_object().expect("object") {
                entry.insert(field.clone(), value.clone());
            }
            let path = file(dir.path(), &[entry]);
            let err = read_panels(&path).expect_err("refused");
            assert!(err.to_string().contains("needs text"), "{err}");
        }
    }

    #[test]
    fn a_malformed_panels_file_is_refused_whole() {
        let dir = tempfile::tempdir().expect("dir");
        let path = dir.path().join(PANELS_FILE);
        fs::write(
            &path,
            r#"{"dashboards": [{"id": "d", "name": "D", "panels": [{"id": "x"}]}]}"#,
        )
        .expect("file");
        assert!(read_panels(&path).is_err());
    }

    #[test]
    fn series_key_names_the_series_the_explorer_reads() {
        // The dashboard finds a panel's series by this key: a contract.
        let mut entry = panel("a");
        entry.insert("sql".into(), json!("SELECT 1"));
        let parsed = parse_panel("p", &Value::Object(entry)).expect("panel");
        assert_eq!(series_key(&parsed), "a:2ae978d15fa2");
    }
}
