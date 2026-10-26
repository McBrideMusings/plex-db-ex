//! `plexdb-rs explore` — Plex DB-EX served from Rust: the page and its read-only keyword
//! routes (`/api/tags`, `/api/titles`, `/api/cotags`, `/api/map`, `/api/tagnetwork`,
//! `/api/title`), answering what `plexdb/explore.py` answers (ADR-0001, ADR-0005).
//!
//! Nothing here writes the store: every request opens it read-only. A route not yet moved
//! from Python is forwarded, with its method, headers and body, to the Python explorer
//! named by `--fallback`, so the one page keeps working while the routes move a slice at a
//! time. Without `--fallback` such a route answers 404.
//!
//! Computed answers are cached per store file, keyed on its modification time and size, so a
//! newly published snapshot is picked up on the next request without a restart. One compute
//! runs per key: a second request for the same key waits for the first and reuses its result.

use std::collections::{BTreeSet, HashSet};
use std::convert::Infallible;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context, Poll};
use std::time::Duration;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{RawQuery, Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::Response;
use axum::routing::{get, post};
use futures_core::Stream;
use plexdb_store::{Config, EXPLORE_SAVED_PATH_VAR, py_path, py_strip};
use tokio::sync::mpsc;

use crate::decision_book::{Book, Latest};
use crate::decision_views::{
    check_merge, check_name, check_role, doctor_titles, is_carried, keyword_search,
    keyword_spellings, merge_book, merges_json, name_book, name_view_json, names_json,
    names_object, own_labels, require_pair, require_role, review_pair, review_rows, role_book,
    role_cell, roles_json, title_doctor,
};
use crate::decisions::py_repr;
use crate::explore_views::{
    ExploreError, KeywordSpace, MAX_ITEM_ID, MapPayload, Overlay, TagIndex, build_index,
    check_kind, cotags_json, display_labels, draw_map, draw_network, map_json, network_done_json,
    stored_map, stored_network, title_details, title_kind, titles_json,
};
use crate::keyword_views::{keyword_rows, merge_map};
use crate::list_file::{DecideError, decide as decide_list, list_book_in};
use crate::lists::{list_detail, list_table, with_pending};
use crate::pyjson::{Json, dumps_compact};
use crate::refresh_tagnetwork::Network;
use crate::steps::Failure;

pub const NAME: &str = "explore";

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 5194;

/// The page: the one file `plexdb/explore.py` also serves.
const PAGE: &str = include_str!("../../../plexdb/explore.html");

/// Concurrent `/api/title`, `/api/titles` and `/api/doctor` requests; a fifth is told to retry.
const TITLE_SLOTS: usize = 4;

/// Concurrent `/api/keywords` and `/api/doctor/titles` requests.
const KEYWORD_SLOTS: usize = 2;

/// The longest request body a decision POST may carry.
const MAX_BODY: usize = 1 << 20;

/// How long a forwarded request may take: the Search tab's runs are the slow ones.
const FALLBACK_TIMEOUT: Duration = Duration::from_secs(600);

/// A cache of computed values by key: one compute per key, and no key waits on another.
/// Each entry carries the store generation it was computed for, so a newer generation
/// replaces it, and the oldest entry goes once more than `keep` are held.
struct Keyed<K, V> {
    keep: usize,
    entries: Mutex<Vec<Entry<K, V>>>,
}

/// A key, the generation its value was computed for, and the value once there is one.
type Entry<K, V> = (K, (i64, u64), Arc<Mutex<Option<Arc<V>>>>);

impl<K: PartialEq + Clone, V> Keyed<K, V> {
    fn new(keep: usize) -> Self {
        Keyed {
            keep,
            entries: Mutex::new(Vec::new()),
        }
    }

    /// The value for `key` at `generation`, computed by `compute` when it is not held. The
    /// flag says whether this call computed it.
    fn get<E>(
        &self,
        key: &K,
        generation: (i64, u64),
        compute: impl FnOnce() -> Result<V, E>,
    ) -> Result<(Arc<V>, bool), E> {
        let cell = {
            let mut entries = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
            match entries.iter().position(|(k, _, _)| k == key) {
                Some(at) if entries[at].1 == generation => entries[at].2.clone(),
                found => {
                    if let Some(at) = found {
                        entries.remove(at);
                    }
                    let cell = Arc::new(Mutex::new(None));
                    entries.push((key.clone(), generation, cell.clone()));
                    while entries.len() > self.keep {
                        entries.remove(0);
                    }
                    cell
                }
            }
        };
        let mut slot = cell.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(value) = slot.as_ref() {
            return Ok((value.clone(), false));
        }
        let value = Arc::new(compute()?);
        *slot = Some(value.clone());
        Ok((value, true))
    }
}

/// A claim on one of a route's concurrent-request slots, given back on drop.
struct Slot<'a>(&'a AtomicUsize);

impl<'a> Slot<'a> {
    fn take(slots: &'a AtomicUsize, limit: usize) -> Option<Self> {
        let mut held = slots.load(Ordering::SeqCst);
        loop {
            if held >= limit {
                return None;
            }
            match slots.compare_exchange(held, held + 1, Ordering::SeqCst, Ordering::SeqCst) {
                Ok(_) => return Some(Slot(slots)),
                Err(now) => held = now,
            }
        }
    }
}

impl Drop for Slot<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

struct App {
    store: PathBuf,
    /// Where `name_decisions.json` sits.
    names: PathBuf,
    merges: Book,
    roles: Book,
    name_decisions: Book,
    list_decisions: Book,
    fallback: Option<String>,
    indexes: Keyed<String, TagIndex>,
    spaces: Keyed<String, KeywordSpace>,
    maps: Keyed<(String, BTreeSet<String>), MapPayload>,
    stale_maps: Keyed<String, Option<MapPayload>>,
    networks: Keyed<(String, BTreeSet<String>), Network>,
    title_slots: AtomicUsize,
    keyword_slots: AtomicUsize,
}

type Shared = Arc<App>;

impl App {
    /// The store file's modification time and size: the generation every cache is keyed on.
    fn generation(&self) -> Result<(i64, u64), ExploreError> {
        use std::os::unix::fs::MetadataExt;
        let stat = std::fs::metadata(&self.store).map_err(|err| {
            if err.kind() == std::io::ErrorKind::NotFound {
                ExploreError::Unavailable(format!(
                    "no store at {} yet — the next sweep publishes one",
                    self.store.display()
                ))
            } else {
                ExploreError::Internal(format!("could not read the store: {err}"))
            }
        })?;
        Ok((stat.mtime() * 1_000_000_000 + stat.mtime_nsec(), stat.len()))
    }

    fn stamp(generation: (i64, u64)) -> String {
        format!("{}:{}", generation.0, generation.1)
    }

    fn open(&self) -> Result<rusqlite::Connection, ExploreError> {
        Ok(plexdb_store::open_readonly(&self.store)?)
    }
}

fn respond(status: StatusCode, content_type: &'static str, body: impl Into<Body>) -> Response {
    let mut response = Response::new(body.into());
    *response.status_mut() = status;
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    // The page and its answers change with every deploy and every write; a browser
    // revalidates rather than showing yesterday's page beside today's API.
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    response
}

fn json_reply(status: StatusCode, body: &Json) -> Response {
    respond(status, "application/json", dumps_compact(body))
}

fn error_reply(err: &ExploreError) -> (StatusCode, String) {
    match err {
        ExploreError::Invalid(message) => (StatusCode::BAD_REQUEST, message.clone()),
        ExploreError::NotFound(message) => (StatusCode::NOT_FOUND, message.clone()),
        ExploreError::Busy(message) => (StatusCode::TOO_MANY_REQUESTS, message.clone()),
        ExploreError::Unavailable(message) => (StatusCode::SERVICE_UNAVAILABLE, message.clone()),
        ExploreError::Internal(message) => (StatusCode::INTERNAL_SERVER_ERROR, message.clone()),
    }
}

fn failed(err: &ExploreError) -> Response {
    let (status, message) = error_reply(err);
    if matches!(err, ExploreError::Unavailable(_)) && !message.contains(" yet — ") {
        eprintln!("explore: store problem: {message}");
    }
    json_reply(status, &Json::object([("error", Json::Str(message))]))
}

/// The query string as Python's `parse_qs` reads it: pairs in order, a blank value dropped.
fn parse_query(raw: Option<String>) -> Vec<(String, String)> {
    let decode = |text: &str| crate::pyurl::unquote(&text.replace('+', " "));
    raw.unwrap_or_default()
        .split('&')
        .filter_map(|pair| {
            let (name, value) = pair.split_once('=')?;
            (!value.is_empty()).then(|| (decode(name), decode(value)))
        })
        .collect()
}

struct Query(Vec<(String, String)>);

impl Query {
    fn first(&self, name: &str) -> Option<&str> {
        self.0
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.as_str())
    }

    fn kind(&self) -> String {
        self.first("kind").unwrap_or("movie").to_string()
    }

    fn all(&self, name: &str) -> BTreeSet<String> {
        self.0
            .iter()
            .filter(|(n, _)| n == name)
            .map(|(_, v)| v.clone())
            .collect()
    }
}

/// Run `work` off the async threads and turn its result into a response.
async fn blocking<F>(work: F) -> Response
where
    F: FnOnce() -> Result<Json, ExploreError> + Send + 'static,
{
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(body)) => json_reply(StatusCode::OK, &body),
        Ok(Err(err)) => failed(&err),
        Err(join) => failed(&ExploreError::Internal(format!(
            "the request failed: {join}"
        ))),
    }
}

async fn page() -> Response {
    respond(StatusCode::OK, "text/html; charset=utf-8", PAGE)
}

async fn tags(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let kind = query.kind();
        check_kind(&kind)?;
        let (index, _) = app.index(&kind)?;
        Ok(index.json.clone())
    })
    .await
}

async fn cotags(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let kind = query.kind();
        check_kind(&kind)?;
        let Some(tag) = query.first("tag") else {
            return Err(ExploreError::Invalid("tag is required".into()));
        };
        let (index, generation) = app.index(&kind)?;
        Ok(cotags_json(&kind, &index, tag, &App::stamp(generation)))
    })
    .await
}

fn too_many_titles() -> ExploreError {
    ExploreError::Busy("too many title requests are already running; try again in a moment".into())
}

async fn titles(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let kind = query.kind();
        let Some(tag) = query.first("tag") else {
            return Err(ExploreError::Invalid("tag is required".into()));
        };
        let _slot = Slot::take(&app.title_slots, TITLE_SLOTS).ok_or_else(too_many_titles)?;
        let conn = app.open()?;
        titles_json(&conn, &kind, tag)
    })
    .await
}

async fn title(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let Some(item_id) = query.first("item_id") else {
            return Err(ExploreError::Invalid("item_id is required".into()));
        };
        if item_id.chars().count() > MAX_ITEM_ID {
            return Err(ExploreError::Invalid(format!(
                "item_id is over {MAX_ITEM_ID} characters"
            )));
        }
        let _slot = Slot::take(&app.title_slots, TITLE_SLOTS).ok_or_else(too_many_titles)?;
        let conn = app.open()?;
        let space = match title_kind(&conn, item_id)? {
            Some(kind) => Some(app.space(&kind)?),
            None => None,
        };
        let overlay = Overlay::read(&app.names)?;
        let folded = merge_map(&conn)?;
        let labels = display_labels(&conn, &overlay, &folded)?;
        title_details(&conn, item_id, &labels, &folded, space.as_deref())
    })
    .await
}

async fn map(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let kind = query.kind();
        check_kind(&kind)?;
        let exclude = query.all("exclude");
        let conn = app.open()?;
        let overlay = Overlay::read(&app.names)?;
        // The map and its labels are cached or stored as keywords; a display label is not
        // part of either, so it is laid over them on every request and a rename shows on the
        // next one.
        let labels = display_labels(&conn, &overlay, &merge_map(&conn)?)?;
        drop(conn);
        // `stored=1` asks for the stored map even when it is stale, for the page to show
        // dimmed while the plain request draws the fresh one.
        if query.first("stored") == Some("1")
            && exclude.is_empty()
            && let Some(stale) = app.stale_map(&kind)?.as_ref()
        {
            return Ok(map_json(stale, &labels));
        }
        let drawn = app.map(&kind, &exclude)?;
        Ok(map_json(&drawn, &labels))
    })
    .await
}

/// A response body that sends each line a compute hands it as it is ready.
struct Lines(mpsc::UnboundedReceiver<Bytes>);

impl Stream for Lines {
    type Item = Result<Bytes, Infallible>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.0.poll_recv(cx).map(|line| line.map(Ok))
    }
}

async fn tagnetwork(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    let kind = query.kind();
    // The kind is checked before the headers go out, as every other route checks it.
    if let Err(err) = check_kind(&kind) {
        return failed(&err);
    }
    let exclude = query.all("exclude");
    let (tx, rx) = mpsc::unbounded_channel::<Bytes>();
    // One JSON object per line, flushed as each stage completes. The compute runs to
    // completion and lands in the cache even if the client has gone: laying the network
    // out is the expensive part, and the next request for it should not pay again.
    tokio::task::spawn_blocking(move || {
        let send = |line: Json| {
            let mut text = dumps_compact(&line);
            text.push('\n');
            let _ = tx.send(Bytes::from(text));
        };
        match app.network(&kind, &exclude, &mut |line| send(line)) {
            Ok((network, generation)) => {
                send(network_done_json(&kind, &network, &App::stamp(generation)));
            }
            Err(err) => {
                let (_, message) = error_reply(&err);
                send(Json::object([
                    ("stage", Json::str("error")),
                    ("error", Json::Str(message)),
                ]));
            }
        }
    });
    let mut response = Response::new(Body::from_stream(Lines(rx)));
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/x-ndjson"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

impl App {
    fn index(&self, kind: &str) -> Result<(Arc<TagIndex>, (i64, u64)), ExploreError> {
        let generation = self.generation()?;
        let (index, _) = self.indexes.get(&kind.to_string(), generation, || {
            let conn = self.open()?;
            let rows = keyword_rows(&conn, kind, &merge_map(&conn)?)?;
            Ok::<_, ExploreError>(build_index(kind, &rows, &Self::stamp(generation)))
        })?;
        Ok((index, generation))
    }

    fn space(&self, kind: &str) -> Result<Arc<KeywordSpace>, ExploreError> {
        let generation = self.generation()?;
        let (space, _) = self.spaces.get(&kind.to_string(), generation, || {
            let conn = self.open()?;
            let rows = keyword_rows(&conn, kind, &merge_map(&conn)?)?;
            Ok::<_, ExploreError>(KeywordSpace::new(&rows))
        })?;
        Ok(space)
    }

    /// The map of `kind` with `exclude` left out: the stored one when nothing is excluded and
    /// it matches the keyword rows, else drawn live.
    fn map(&self, kind: &str, exclude: &BTreeSet<String>) -> Result<Arc<MapPayload>, ExploreError> {
        let generation = self.generation()?;
        let key = (kind.to_string(), exclude.clone());
        let (payload, _) = self.maps.get(&key, generation, || {
            let conn = self.open()?;
            if exclude.is_empty()
                && let Some(stored) = stored_map(&conn, kind, false)?
            {
                return Ok::<_, ExploreError>(stored);
            }
            let excluded: HashSet<String> = exclude.iter().cloned().collect();
            draw_map(&conn, kind, &excluded)
        })?;
        Ok(payload)
    }

    /// The stored default map of `kind` when it was drawn from older keyword rows (`stale`
    /// set), so the page has something to show while [`App::map`] draws the fresh one.
    fn stale_map(&self, kind: &str) -> Result<Arc<Option<MapPayload>>, ExploreError> {
        let generation = self.generation()?;
        let (stale, _) = self.stale_maps.get(&kind.to_string(), generation, || {
            let conn = self.open()?;
            Ok::<_, ExploreError>(stored_map(&conn, kind, true)?.filter(|m| m.stale))
        })?;
        Ok(stale)
    }

    /// The network of `kind` with `exclude` left out and the generation it was computed for.
    /// The call that computes it hands `emit` the vocabulary and edges lines on the way;
    /// every other call, a cache hit or a wait behind that one, emits nothing.
    fn network(
        &self,
        kind: &str,
        exclude: &BTreeSet<String>,
        emit: &mut dyn FnMut(Json),
    ) -> Result<(Arc<Network>, (i64, u64)), ExploreError> {
        let generation = self.generation()?;
        let key = (kind.to_string(), exclude.clone());
        let (network, _) = self.networks.get(&key, generation, || {
            let conn = self.open()?;
            if exclude.is_empty()
                && let Some(stored) = stored_network(&conn, kind)?
            {
                return Ok::<_, ExploreError>(stored);
            }
            let excluded: HashSet<String> = exclude.iter().cloned().collect();
            draw_network(&conn, kind, &excluded, emit)
        })?;
        Ok((network, generation))
    }
}

// ---------------------------------------------------------------------------------------
// The decision tabs and the tag doctor

fn too_many_requests() -> ExploreError {
    ExploreError::Busy("too many requests are already running; try again in a moment".into())
}

type JsonBody = serde_json::Map<String, serde_json::Value>;

/// A decision POST's JSON object (`_body`). A page on another site can send `text/plain`
/// without a preflight; `application/json` it cannot, so requiring it keeps such a page out.
async fn read_body(request: Request) -> Result<JsonBody, ExploreError> {
    let invalid = |message: &str| ExploreError::Invalid(message.to_string());
    let headers = request.headers();
    let media = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(|value| value.split(';').next().unwrap_or("").trim().to_lowercase());
    if media.as_deref() != Some("application/json") {
        return Err(invalid("Content-Type must be application/json"));
    }
    let length = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()))
        .map(str::to_string);
    let Some(length) = length else {
        return Err(invalid("a JSON body with a Content-Length is required"));
    };
    if length.len() > 12 || length.parse::<usize>().unwrap_or(usize::MAX) > MAX_BODY {
        return Err(ExploreError::Invalid(format!(
            "body is over {MAX_BODY} bytes"
        )));
    }
    let bytes = axum::body::to_bytes(request.into_body(), MAX_BODY)
        .await
        .map_err(|err| ExploreError::Invalid(format!("body is not JSON: {err}")))?;
    match serde_json::from_slice::<serde_json::Value>(&bytes) {
        Ok(serde_json::Value::Object(body)) => Ok(body),
        Ok(_) => Err(invalid("body must be a JSON object")),
        Err(err) => Err(ExploreError::Invalid(format!("body is not JSON: {err}"))),
    }
}

/// Run a decision POST: read its body, then `work` off the async threads.
async fn decide<F>(request: Request, work: F) -> Response
where
    F: FnOnce(JsonBody) -> Result<Json, ExploreError> + Send + 'static,
{
    match read_body(request).await {
        Ok(body) => blocking(move || work(body)).await,
        Err(err) => failed(&err),
    }
}

fn log_decision(line: String) {
    eprintln!("explore: {line}");
}

async fn merges_get(State(app): State<Shared>) -> Response {
    blocking(move || {
        let conn = app.open()?;
        let overlay = Overlay::read(&app.names)?;
        // Each side's own label, not its group's: both sides can already sit in one merge
        // group, and the group label would name them alike.
        let labels = own_labels(&conn, &overlay)?;
        let pairs = review_rows(&conn, &app.merges.latest()?, &labels)?;
        // Each named keyword's own name, so a side's rename box starts from it.
        let named = names_json(&conn, &overlay)?;
        let mut body = merges_json(&pairs);
        body.push(("names", names_object(&named)));
        Ok(Json::object_from(body))
    })
    .await
}

async fn merges_post(State(app): State<Shared>, request: Request) -> Response {
    decide(request, move |body| {
        let (a, b, decision) = check_merge(&body)?;
        // Only a pair the table holds, or two keywords some title carries, may be decided,
        // so an anonymous caller cannot grow the file with made-up names.
        let conn = app.open()?;
        require_pair(&conn, &a, &b)?;
        let spellings = keyword_spellings(&conn, &[&a, &b])?;
        let key = vec![a.clone(), b.clone()];
        let (_, entry) = app.merges.record(&key, &decision, &spellings, &[])?;
        let labels = own_labels(&conn, &Overlay::read(&app.names)?)?;
        let pair = review_pair(&conn, &Latest::single(key.clone(), entry), &labels, &key)?;
        log_decision(format!(
            "merge decision {} {} {decision} -> {}",
            py_repr(&a),
            py_repr(&b),
            app.merges.path().display()
        ));
        Ok(Json::object([("pair", pair.json())]))
    })
    .await
}

async fn roles_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let conn = app.open()?;
        let overlay = Overlay::read(&app.names)?;
        let labels = display_labels(&conn, &overlay, &merge_map(&conn)?)?;
        Ok(roles_json(
            &conn,
            &app.roles.latest()?,
            &labels,
            query.first("q").unwrap_or(""),
        )?)
    })
    .await
}

async fn roles_post(State(app): State<Shared>, request: Request) -> Response {
    decide(request, move |body| {
        let (keyword, role, decision) = check_role(&body)?;
        // As for a pair: only a (keyword, role) the table holds may be decided.
        let conn = app.open()?;
        require_role(&conn, &keyword, &role)?;
        let spellings = keyword_spellings(&conn, &[&keyword])?;
        let key = vec![keyword.clone(), role.clone()];
        let (entry, _) = app.roles.record(&key, &decision, &spellings, &[])?;
        let overlay = Overlay::read(&app.names)?;
        let labels = display_labels(&conn, &overlay, &merge_map(&conn)?)?;
        let cell = role_cell(&conn, &app.roles.latest()?, &labels, &keyword, &role)?;
        log_decision(format!(
            "role decision {} {role} {decision} -> {}",
            py_repr(&keyword),
            app.roles.path().display()
        ));
        Ok(Json::object([("cell", cell), ("entry", entry)]))
    })
    .await
}

async fn names_post(State(app): State<Shared>, request: Request) -> Response {
    decide(request, move |body| {
        let (keyword, decision, name) = check_name(&body)?;
        // Only a keyword some title carries may be named, so an anonymous caller cannot grow
        // the file with made-up keywords.
        let conn = app.open()?;
        if !is_carried(&conn, &keyword)? {
            return Err(ExploreError::NotFound(format!(
                "no title carries the keyword {}",
                py_repr(&keyword)
            )));
        }
        let spellings = keyword_spellings(&conn, &[&keyword])?;
        let (entry, _) = app.name_decisions.record(
            std::slice::from_ref(&keyword),
            &decision,
            &spellings,
            std::slice::from_ref(&name),
        )?;
        let overlay = Overlay::read(&app.names)?;
        let shown = names_json(&conn, &overlay)?
            .into_iter()
            .find(|(k, _)| *k == keyword)
            .map_or(Json::Null, |(_, view)| name_view_json(&view));
        let labels = display_labels(&conn, &overlay, &merge_map(&conn)?)?;
        let label = labels.get(&keyword).unwrap_or(&keyword).clone();
        log_decision(format!(
            "name decision {} {decision} {} -> {}",
            py_repr(&keyword),
            py_repr(&name),
            app.name_decisions.path().display()
        ));
        Ok(Json::object([
            ("keyword", Json::Str(keyword)),
            ("label", Json::Str(label)),
            ("name", shown),
            ("entry", entry),
        ]))
    })
    .await
}

async fn lists_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let conn = app.open()?;
        let rows = list_table(&conn, query.first("source"))?;
        let rows = with_pending(rows, &app.list_decisions.latest()?);
        let lists = serde_json::Value::Array(rows.into_iter().map(Into::into).collect());
        Ok(Json::object([("lists", Json::from(&lists))]))
    })
    .await
}

async fn list_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let Some(reference) = query.first("ref") else {
            return Err(ExploreError::Invalid("ref is required".into()));
        };
        if reference.chars().count() > MAX_ITEM_ID {
            return Err(ExploreError::Invalid(format!(
                "ref is over {MAX_ITEM_ID} characters"
            )));
        }
        let conn = app.open()?;
        match list_detail(&conn, reference)? {
            Some(detail) => Ok(Json::from(&serde_json::Value::Object(detail))),
            None => Err(ExploreError::NotFound(format!("no list {reference}"))),
        }
    })
    .await
}

async fn list_decisions_post(State(app): State<Shared>, request: Request) -> Response {
    decide(request, move |body| {
        let text = |field: &str| {
            body.get(field)
                .and_then(serde_json::Value::as_str)
                .unwrap_or("")
        };
        let conn = app.open()?;
        let entry = decide_list(&conn, &app.list_decisions, text("url"), text("decision"))
            .map_err(|err| match err {
                DecideError::Invalid(message) => ExploreError::Invalid(message),
                DecideError::Unknown(url) => {
                    ExploreError::NotFound(format!("no stored list has the URL {url}"))
                }
                DecideError::File(err) => ExploreError::from(err),
                DecideError::Sqlite(err) => ExploreError::from(err),
            })?;
        log_decision(format!(
            "list decision {} {} -> {}",
            py_repr(&entry.key[0]),
            entry.decision,
            app.list_decisions.path().display()
        ));
        Ok(Json::object([(
            "entry",
            Json::object([
                ("url", Json::Str(entry.key[0].clone())),
                ("decision", Json::Str(entry.decision.clone())),
                ("decided_at", Json::Str(entry.decided_at.clone())),
            ]),
        )]))
    })
    .await
}

/// The tag doctor's routes share one shape: a slot, the store, the display labels.
async fn doctor_route(app: Shared, raw: Option<String>, which: Doctor) -> Response {
    let query = Query(parse_query(raw));
    blocking(move || {
        let (slots, limit, busy) = match which {
            Doctor::Title => (&app.title_slots, TITLE_SLOTS, too_many_requests()),
            Doctor::Keywords | Doctor::Titles => {
                (&app.keyword_slots, KEYWORD_SLOTS, too_many_requests())
            }
        };
        let _slot = Slot::take(slots, limit).ok_or(busy)?;
        let conn = app.open()?;
        let overlay = Overlay::read(&app.names)?;
        let labels = display_labels(&conn, &overlay, &merge_map(&conn)?)?;
        match which {
            Doctor::Keywords => Ok(Json::object([(
                "keywords",
                keyword_search(&conn, query.first("q").unwrap_or(""), &labels)?,
            )])),
            Doctor::Titles => Ok(Json::object([(
                "titles",
                doctor_titles(&conn, query.first("q").unwrap_or(""))?,
            )])),
            Doctor::Title => {
                let Some(item_id) = query.first("item_id") else {
                    return Err(ExploreError::Invalid("item_id is required".into()));
                };
                if item_id.chars().count() > MAX_ITEM_ID {
                    return Err(ExploreError::Invalid(format!(
                        "item_id is over {MAX_ITEM_ID} characters"
                    )));
                }
                let named = names_json(&conn, &overlay)?;
                title_doctor(&conn, item_id, &app.merges.latest()?, &labels, &named)
            }
        }
    })
    .await
}

#[derive(Clone, Copy)]
enum Doctor {
    Title,
    Keywords,
    Titles,
}

async fn doctor(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    doctor_route(app, raw, Doctor::Title).await
}

async fn doctor_keywords(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    doctor_route(app, raw, Doctor::Keywords).await
}

async fn doctor_title_search(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    doctor_route(app, raw, Doctor::Titles).await
}

/// Forward a request this server does not answer to the Python explorer.
async fn forward(State(app): State<Shared>, request: Request) -> Response {
    let path = request.uri().path().to_string();
    let Some(fallback) = app.fallback.clone() else {
        return json_reply(
            StatusCode::NOT_FOUND,
            &Json::object([("error", Json::Str(format!("no route {path}")))]),
        );
    };
    let method = request.method().clone();
    let target = match request.uri().path_and_query() {
        Some(pq) => format!("{}{}", fallback.trim_end_matches('/'), pq),
        None => format!("{}{path}", fallback.trim_end_matches('/')),
    };
    let headers = request.headers().clone();
    let Ok(body) = axum::body::to_bytes(request.into_body(), 2 << 20).await else {
        return failed(&ExploreError::Invalid("body is too large".into()));
    };
    match tokio::task::spawn_blocking(move || proxy(&method, &target, &headers, body.to_vec()))
        .await
    {
        Ok(Ok(response)) => response,
        Ok(Err(message)) => json_reply(
            StatusCode::BAD_GATEWAY,
            &Json::object([("error", Json::Str(message))]),
        ),
        Err(join) => failed(&ExploreError::Internal(format!(
            "the request failed: {join}"
        ))),
    }
}

/// Headers a forwarded request carries on: what the Python routes read.
const FORWARDED: [&str; 3] = ["content-type", "range", "accept"];

/// Headers a forwarded answer carries back.
const RETURNED: [&str; 6] = [
    "content-type",
    "cache-control",
    "accept-ranges",
    "content-range",
    "content-disposition",
    "etag",
];

fn proxy(
    method: &Method,
    target: &str,
    headers: &HeaderMap,
    body: Vec<u8>,
) -> Result<Response, String> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(FALLBACK_TIMEOUT))
        .max_redirects(0)
        .http_status_as_error(false)
        .build()
        .into();
    let mut request = ureq::http::Request::builder()
        .method(method.as_str())
        .uri(target);
    for name in FORWARDED {
        if let Some(value) = headers.get(name) {
            request = request.header(name, value.as_bytes());
        }
    }
    let request = request
        .body(body)
        .map_err(|err| format!("cannot forward the request: {err}"))?;
    let mut answer = agent
        .run(request)
        .map_err(|err| format!("the Python explorer did not answer: {err}"))?;
    let status = StatusCode::from_u16(answer.status().as_u16())
        .map_err(|err| format!("the Python explorer sent a bad status: {err}"))?;
    let mut response_headers = HeaderMap::new();
    for name in RETURNED {
        if let Some(value) = answer.headers().get(name)
            && let Ok(value) = HeaderValue::from_bytes(value.as_bytes())
        {
            response_headers.insert(header::HeaderName::from_static(name), value);
        }
    }
    let data = answer
        .body_mut()
        .with_config()
        .limit(u64::MAX)
        .read_to_vec()
        .map_err(|err| format!("the Python explorer's answer was cut off: {err}"))?;
    let mut response = Response::new(Body::from(data));
    *response.status_mut() = status;
    *response.headers_mut() = response_headers;
    Ok(response)
}

fn router(app: Shared) -> Router {
    Router::new()
        .route("/", get(page))
        .route("/api/tags", get(tags))
        .route("/api/titles", get(titles))
        .route("/api/cotags", get(cotags))
        .route("/api/map", get(map))
        .route("/api/tagnetwork", get(tagnetwork))
        .route("/api/title", get(title))
        .route(
            "/api/merges",
            get(merges_get).post(merges_post).fallback(forward),
        )
        .route(
            "/api/roles",
            get(roles_get).post(roles_post).fallback(forward),
        )
        .route("/api/names", post(names_post).fallback(forward))
        .route("/api/lists", get(lists_get).fallback(forward))
        .route("/api/list", get(list_get).fallback(forward))
        .route(
            "/api/list-decisions",
            post(list_decisions_post).fallback(forward),
        )
        .route("/api/doctor", get(doctor).fallback(forward))
        .route("/api/keywords", get(doctor_keywords).fallback(forward))
        .route(
            "/api/doctor/titles",
            get(doctor_title_search).fallback(forward),
        )
        .fallback(forward)
        .with_state(app)
}

struct Options {
    host: String,
    port: u16,
    store: Option<PathBuf>,
    fallback: Option<String>,
}

fn parse_options(args: &[String]) -> Result<Options, Failure> {
    let usage = |message: String| Failure::Usage(message);
    let mut options = Options {
        host: DEFAULT_HOST.to_string(),
        port: DEFAULT_PORT,
        store: None,
        fallback: None,
    };
    let mut rest = args.iter();
    while let Some(flag) = rest.next() {
        let (name, inline) = match flag.split_once('=') {
            Some((name, value)) => (name, Some(value.to_string())),
            None => (flag.as_str(), None),
        };
        let mut value = || {
            inline
                .clone()
                .or_else(|| rest.next().cloned())
                .ok_or_else(|| usage(format!("{name} needs a value")))
        };
        match name {
            "--host" => options.host = value()?,
            "--port" => {
                let raw = value()?;
                options.port = raw
                    .parse()
                    .map_err(|_| usage(format!("--port must be a port number, got {raw:?}")))?;
            }
            "--store" => options.store = Some(py_path(&value()?)),
            "--fallback" => options.fallback = Some(value()?),
            other => {
                return Err(usage(format!(
                    "explore takes [--host H] [--port N] [--store PATH] [--fallback URL], got {other}"
                )));
            }
        }
    }
    Ok(options)
}

/// `plexdb-rs explore`: serve until interrupted.
pub fn run(args: &[String]) -> Result<(), Failure> {
    let options = parse_options(args)?;
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let store = options.store.unwrap_or_else(|| config.store_path.clone());
    if !store.exists() {
        return Err(Failure::Failed(format!(
            "no store at {} — run `admin pull-baseline`",
            store.display()
        )));
    }
    let dir = decisions_dir(&store);
    let fallback = options
        .fallback
        .or_else(|| std::env::var("PLEXDB_EXPLORE_FALLBACK").ok())
        .map(|url| py_strip(&url).to_string())
        .filter(|url| !url.is_empty());
    let app = Arc::new(App {
        store: store.clone(),
        names: name_book(&dir).path().to_path_buf(),
        merges: merge_book(&dir),
        roles: role_book(&dir),
        name_decisions: name_book(&dir),
        list_decisions: list_book_in(&dir),
        fallback,
        indexes: Keyed::new(2),
        spaces: Keyed::new(2),
        maps: Keyed::new(6),
        stale_maps: Keyed::new(6),
        networks: Keyed::new(6),
        title_slots: AtomicUsize::new(0),
        keyword_slots: AtomicUsize::new(0),
    });
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|err| Failure::Failed(format!("cannot start the server: {err}")))?;
    runtime.block_on(async {
        let listener = tokio::net::TcpListener::bind((options.host.as_str(), options.port))
            .await
            .map_err(|err| {
                Failure::Failed(format!(
                    "cannot listen on {}:{}: {err}",
                    options.host, options.port
                ))
            })?;
        let port = listener.local_addr().map_or(options.port, |a| a.port());
        println!(
            "Plex DB-EX on http://{}:{port}/ reading {} (read-only); Ctrl-C to stop",
            options.host,
            store.display()
        );
        use std::io::Write as _;
        let _ = std::io::stdout().flush();
        axum::serve(listener, router(app))
            .with_graceful_shutdown(async {
                let _ = tokio::signal::ctrl_c().await;
            })
            .await
            .map_err(|err| Failure::Failed(format!("the server stopped: {err}")))
    })
}

/// Where the explorer's own files sit (`explore.data_dir`): beside the saved-queries file
/// when `PLEXDB_EXPLORE_SAVED_PATH` names one, else beside the store.
fn decisions_dir(store: &Path) -> PathBuf {
    match std::env::var(EXPLORE_SAVED_PATH_VAR) {
        Ok(raw) if !py_strip(&raw).is_empty() => {
            let saved = py_path(py_strip(&raw));
            saved.parent().map(Path::to_path_buf).unwrap_or_default()
        }
        _ => store.parent().map(Path::to_path_buf).unwrap_or_default(),
    }
}
