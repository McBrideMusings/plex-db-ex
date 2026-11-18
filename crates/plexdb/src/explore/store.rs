//! `/api/store/*`: every fact `plexdb-reader` reads, over HTTP (ADR-0031).
//!
//! Each route turns its request into a [`Call`], opens a [`Reader`] on the explorer's store
//! and answers with [`Reader::answer`]'s JSON — the same text `plexdb-read` prints for that
//! call. These routes and their JSON shapes are the store's public interface; no program
//! outside this repository opens a store file.

use std::time::Instant;

use axum::extract::{RawQuery, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use plexdb_reader::{Call, Needs, Reader, ReaderError};
use serde_json::Value;

use super::{App, JsonBody, Query, Shared, Slot, failed, parse_query, read_body, respond};
use crate::explore_views::{ExploreError, MAX_ITEM_ID};

/// Concurrent `/api/store/*` requests; the next is told to retry.
pub(super) const STORE_SLOTS: usize = 8;

/// The most ids a batch request may name: above the 13,210 movies of the 2026-11-18 store.
const MAX_BATCH: usize = 20_000;

/// The longest surface, keyword, namespace, tag, edge type or phrase a request may name.
const MAX_TEXT: usize = 500;

impl From<ReaderError> for ExploreError {
    fn from(err: ReaderError) -> Self {
        match err {
            ReaderError::InvalidSearch(_) => ExploreError::Invalid(err.to_string()),
            ReaderError::Reply(_) => ExploreError::Internal(err.to_string()),
            other => ExploreError::Unavailable(other.to_string()),
        }
    }
}

/// A Reader opened on the files `needs` names, from the Search tab's configuration.
fn reader(app: &App, needs: Needs) -> Result<Reader, ExploreError> {
    // A missing store is the 503 every route gives, not the reader's open error.
    app.generation()?;
    let (frames, phrases) = match needs {
        Needs::Store => (None, None),
        Needs::Frames => (Some(app.search.frames_file()?), None),
        Needs::Search => {
            let frames = app.search.frames_file()?;
            let phrases = app.search.phrase_cache(&frames)?;
            (Some(frames), Some(phrases))
        }
        Needs::Any => match app.search.frames_file() {
            Ok(frames) => {
                let phrases = app.search.phrase_cache(&frames).ok();
                (Some(frames), phrases)
            }
            Err(ExploreError::Unavailable(_)) => (None, None),
            Err(other) => return Err(other),
        },
    };
    Ok(Reader::open(&app.store, frames.as_deref(), phrases)?)
}

/// What a call's log line names: its arguments, with a list of ids as its length.
fn detail(call: &Call) -> String {
    match call {
        Call::EnrichmentFor { item_id, namespace } => {
            format!("item_id={item_id} namespace={namespace}")
        }
        Call::EnrichmentForMany {
            item_ids,
            namespace,
        } => format!("ids={} namespace={namespace}", item_ids.len()),
        Call::AudioFactsForMany { item_ids } => format!("ids={}", item_ids.len()),
        Call::AudioFactsFor { item_id }
        | Call::CollectionsFor { item_id }
        | Call::Cuts { item_id }
        | Call::Chapters { item_id } => format!("item_id={item_id}"),
        Call::KeywordForSurface { surface } => format!("surface={surface:?}"),
        Call::SurfacesForKeyword { keyword } | Call::DisplayName { keyword } => {
            format!("keyword={keyword:?}")
        }
        Call::EdgesFrom { item_id, edge_type } | Call::EdgesTo { item_id, edge_type } => {
            format!("item_id={item_id} edge_type={edge_type}")
        }
        Call::TagNeighbours { kind, tag } => format!("kind={kind} tag={tag:?}"),
        Call::TasteVectorFor { account } | Call::WatchedUnitsFor { account } => {
            format!("account={account}")
        }
        Call::Search {
            pool,
            phrase,
            negatives,
            draw,
            ..
        } => format!(
            "pool={} phrase={phrase:?} negatives={} draw={draw:?}",
            pool.len(),
            negatives.len()
        ),
        Call::PooledTasteVector | Call::WatchedUnits | Call::Capabilities => String::new(),
    }
}

fn log(route: &str, detail: &str, response: &Response, began: Instant) {
    let detail = if detail.is_empty() {
        String::new()
    } else {
        format!("{detail} ")
    };
    eprintln!(
        "explore: store {route} {detail}status={} ms={}",
        response.status().as_u16(),
        began.elapsed().as_millis()
    );
}

/// Answer `call` off the async threads, or the request's own error, and log one line.
async fn answer(app: Shared, route: &'static str, call: Result<Call, ExploreError>) -> Response {
    let began = Instant::now();
    let call = match call {
        Ok(call) => call,
        Err(err) => {
            let response = failed(&err);
            log(route, "", &response, began);
            return response;
        }
    };
    let detail = detail(&call);
    let result = tokio::task::spawn_blocking(move || {
        let _slot = Slot::take(&app.store_slots, STORE_SLOTS).ok_or_else(|| {
            ExploreError::Busy(
                "too many store requests are already running; try again in a moment".into(),
            )
        })?;
        Ok::<_, ExploreError>(reader(&app, call.needs())?.answer(&call)?)
    })
    .await
    .unwrap_or_else(|join| {
        Err(ExploreError::Internal(format!(
            "the request failed: {join}"
        )))
    });
    let response = match result {
        Ok(body) => respond(StatusCode::OK, "application/json", body),
        Err(err) => failed(&err),
    };
    log(route, &detail, &response, began);
    response
}

// --- reading a request ---

/// A required query parameter of at most `max` characters.
fn param(query: &Query, name: &str, max: usize) -> Result<String, ExploreError> {
    match query.first(name) {
        Some(value) if value.chars().count() <= max => Ok(value.to_string()),
        Some(_) => Err(ExploreError::Invalid(format!(
            "{name} is over {max} characters"
        ))),
        None => Err(ExploreError::Invalid(format!("{name} is required"))),
    }
}

fn item_id(query: &Query) -> Result<String, ExploreError> {
    param(query, "item_id", MAX_ITEM_ID)
}

fn text(query: &Query, name: &str) -> Result<String, ExploreError> {
    param(query, name, MAX_TEXT)
}

fn account(query: &Query) -> Result<i64, ExploreError> {
    query
        .first("account")
        .and_then(|raw| raw.parse::<i64>().ok())
        .ok_or_else(|| {
            ExploreError::Invalid("account must be a Plex account id, a whole number".into())
        })
}

/// A body field holding a list of at most [`MAX_BATCH`] item ids.
fn body_ids(body: &JsonBody, name: &str) -> Result<Vec<String>, ExploreError> {
    let bad = || {
        ExploreError::Invalid(format!(
            "{name} must be a list of at most {MAX_BATCH} item ids"
        ))
    };
    let Some(Value::Array(items)) = body.get(name) else {
        return Err(bad());
    };
    if items.len() > MAX_BATCH {
        return Err(bad());
    }
    items
        .iter()
        .map(|item| match item.as_str() {
            Some(id) if !id.is_empty() && id.chars().count() <= MAX_ITEM_ID => Ok(id.to_string()),
            _ => Err(bad()),
        })
        .collect()
}

fn body_text(body: &JsonBody, name: &str) -> Result<String, ExploreError> {
    match body.get(name).and_then(Value::as_str) {
        Some(text) if !text.is_empty() && text.chars().count() <= MAX_TEXT => Ok(text.to_string()),
        _ => Err(ExploreError::Invalid(format!(
            "{name} must be text of at most {MAX_TEXT} characters"
        ))),
    }
}

fn body_number(body: &JsonBody, name: &str) -> Result<Option<f32>, ExploreError> {
    match body.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_f64()
            .map(|n| Some(n as f32))
            .ok_or_else(|| ExploreError::Invalid(format!("{name} must be a number"))),
    }
}

fn search_call(body: &JsonBody) -> Result<Call, ExploreError> {
    let negatives = match body.get("negatives") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .filter(|t| t.chars().count() <= MAX_TEXT)
                    .map(str::to_string)
            })
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| ExploreError::Invalid("negatives must be a list of text".into()))?,
        Some(_) => {
            return Err(ExploreError::Invalid(
                "negatives must be a list of text".into(),
            ));
        }
    };
    let (draw, seed) = match body.get("draw") {
        None | Some(Value::Null) => (None, 0),
        Some(Value::Object(draw)) => {
            let n = draw
                .get("n")
                .and_then(Value::as_u64)
                .and_then(|n| usize::try_from(n).ok())
                .ok_or_else(|| ExploreError::Invalid("draw.n must be a whole number".into()))?;
            let seed = match draw.get("seed") {
                None | Some(Value::Null) => 0,
                Some(seed) => seed.as_u64().ok_or_else(|| {
                    ExploreError::Invalid("draw.seed must be a whole number".into())
                })?,
            };
            (Some(n), seed)
        }
        Some(_) => {
            return Err(ExploreError::Invalid(
                "draw must be an object with n and seed".into(),
            ));
        }
    };
    Ok(Call::Search {
        pool: body_ids(body, "pool")?,
        phrase: body_text(body, "phrase")?,
        negatives,
        weight: body_number(body, "weight")?,
        temperature: body_number(body, "temperature")?,
        draw,
        seed,
    })
}

/// A GET route: the query string read into a Call by `parse`.
async fn get(
    app: Shared,
    route: &'static str,
    raw: Option<String>,
    parse: impl FnOnce(&Query) -> Result<Call, ExploreError>,
) -> Response {
    let query = Query(parse_query(raw));
    answer(app, route, parse(&query)).await
}

/// A POST route: the JSON body read into a Call by `parse`.
async fn post(
    app: Shared,
    route: &'static str,
    request: Request,
    parse: impl FnOnce(&JsonBody) -> Result<Call, ExploreError>,
) -> Response {
    let call = read_body(request).await.and_then(|body| parse(&body));
    answer(app, route, call).await
}

// --- the routes, one per accessor ---

pub(super) async fn enrichment_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "enrichment", raw, |q| {
        Ok(Call::EnrichmentFor {
            item_id: item_id(q)?,
            namespace: text(q, "namespace")?,
        })
    })
    .await
}

pub(super) async fn enrichment_post(State(app): State<Shared>, request: Request) -> Response {
    post(app, "enrichment-many", request, |b| {
        Ok(Call::EnrichmentForMany {
            item_ids: body_ids(b, "item_ids")?,
            namespace: body_text(b, "namespace")?,
        })
    })
    .await
}

pub(super) async fn audio_facts_get(
    State(app): State<Shared>,
    RawQuery(raw): RawQuery,
) -> Response {
    get(app, "audio-facts", raw, |q| {
        Ok(Call::AudioFactsFor {
            item_id: item_id(q)?,
        })
    })
    .await
}

pub(super) async fn audio_facts_post(State(app): State<Shared>, request: Request) -> Response {
    post(app, "audio-facts-many", request, |b| {
        Ok(Call::AudioFactsForMany {
            item_ids: body_ids(b, "item_ids")?,
        })
    })
    .await
}

pub(super) async fn keyword_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "keyword", raw, |q| {
        Ok(Call::KeywordForSurface {
            surface: text(q, "surface")?,
        })
    })
    .await
}

pub(super) async fn surfaces_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "surfaces", raw, |q| {
        Ok(Call::SurfacesForKeyword {
            keyword: text(q, "keyword")?,
        })
    })
    .await
}

pub(super) async fn display_name_get(
    State(app): State<Shared>,
    RawQuery(raw): RawQuery,
) -> Response {
    get(app, "display-name", raw, |q| {
        Ok(Call::DisplayName {
            keyword: text(q, "keyword")?,
        })
    })
    .await
}

pub(super) async fn edges_from_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "edges/from", raw, |q| {
        Ok(Call::EdgesFrom {
            item_id: item_id(q)?,
            edge_type: text(q, "edge_type")?,
        })
    })
    .await
}

pub(super) async fn edges_to_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "edges/to", raw, |q| {
        Ok(Call::EdgesTo {
            item_id: item_id(q)?,
            edge_type: text(q, "edge_type")?,
        })
    })
    .await
}

pub(super) async fn tag_neighbours_get(
    State(app): State<Shared>,
    RawQuery(raw): RawQuery,
) -> Response {
    get(app, "tag-neighbours", raw, |q| {
        Ok(Call::TagNeighbours {
            kind: text(q, "kind")?,
            tag: text(q, "tag")?,
        })
    })
    .await
}

pub(super) async fn collections_get(
    State(app): State<Shared>,
    RawQuery(raw): RawQuery,
) -> Response {
    get(app, "collections", raw, |q| {
        Ok(Call::CollectionsFor {
            item_id: item_id(q)?,
        })
    })
    .await
}

pub(super) async fn taste_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "taste", raw, |q| {
        Ok(Call::TasteVectorFor {
            account: account(q)?,
        })
    })
    .await
}

pub(super) async fn taste_pooled_get(State(app): State<Shared>) -> Response {
    answer(app, "taste/pooled", Ok(Call::PooledTasteVector)).await
}

pub(super) async fn watched_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "watched", raw, |q| {
        Ok(Call::WatchedUnitsFor {
            account: account(q)?,
        })
    })
    .await
}

pub(super) async fn watched_all_get(State(app): State<Shared>) -> Response {
    answer(app, "watched/all", Ok(Call::WatchedUnits)).await
}

pub(super) async fn search_post(State(app): State<Shared>, request: Request) -> Response {
    post(app, "search", request, search_call).await
}

pub(super) async fn cuts_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "cuts", raw, |q| {
        Ok(Call::Cuts {
            item_id: item_id(q)?,
        })
    })
    .await
}

pub(super) async fn chapters_get(State(app): State<Shared>, RawQuery(raw): RawQuery) -> Response {
    get(app, "chapters", raw, |q| {
        Ok(Call::Chapters {
            item_id: item_id(q)?,
        })
    })
    .await
}

pub(super) async fn capabilities_get(State(app): State<Shared>) -> Response {
    answer(app, "capabilities", Ok(Call::Capabilities)).await
}
