//! The store API: every fact the store holds for consumers, as resources over HTTP
//! (ADR-0031, `docs/api.md`).
//!
//! One handler serves every route. It hands the request's method, raw path, query and
//! body to [`Call::parse`] — the table `plexdb-read` reads its command line with — opens a
//! [`Reader`] on the explorer's store and answers with [`Reader::answer`]'s JSON, the same
//! text `plexdb-read` prints for that path. These routes and their JSON shapes are the
//! store's public interface; no program outside this repository opens a store file.

use std::time::Instant;

use axum::Router;
use axum::extract::{Request, State};
use axum::http::{Method, StatusCode};
use axum::response::Response;
use axum::routing::any;
use plexdb_reader::{Call, Needs, Reader, ReaderError};
use serde_json::Value;

use super::{App, Shared, Slot, failed, json_reply, read_body, respond};
use crate::explore_views::ExploreError;
use crate::pyjson::Json;

/// Concurrent store requests; the next is told to retry.
pub(super) const STORE_SLOTS: usize = 8;

/// The first path segment of every store route.
const ROOTS: [&str; 10] = [
    "items",
    "plays",
    "accounts",
    "taste",
    "watched",
    "keywords",
    "keyword-pairs",
    "tags",
    "capabilities",
    "searches",
];

/// Every store route, each root and everything under it, served by [`route`].
pub(super) fn routes() -> Router<Shared> {
    ROOTS.iter().fold(Router::new(), |router, root| {
        router
            .route(&format!("/{root}"), any(route))
            .route(&format!("/{root}/{{*rest}}"), any(route))
    })
}

/// Why a store request could not be answered, as its status and message.
enum Refusal {
    Explore(ExploreError),
    MethodNotAllowed(String),
}

impl From<ExploreError> for Refusal {
    fn from(err: ExploreError) -> Self {
        Refusal::Explore(err)
    }
}

/// The status a reader's error answers with. A wrong method has its own status, which
/// only [`Call::parse`] gives and [`Refusal`] carries.
fn explore_error(err: ReaderError) -> ExploreError {
    match err {
        ReaderError::BadRequest(_)
        | ReaderError::InvalidSearch(_)
        | ReaderError::MethodNotAllowed(_) => ExploreError::Invalid(err.to_string()),
        ReaderError::NotFound(message) => ExploreError::NotFound(message),
        ReaderError::Reply(_) => ExploreError::Internal(err.to_string()),
        other => ExploreError::Unavailable(other.to_string()),
    }
}

impl From<ReaderError> for Refusal {
    fn from(err: ReaderError) -> Self {
        match err {
            ReaderError::MethodNotAllowed(message) => Refusal::MethodNotAllowed(message),
            other => Refusal::Explore(explore_error(other)),
        }
    }
}

impl Refusal {
    fn response(&self) -> Response {
        match self {
            Refusal::Explore(err) => failed(err),
            Refusal::MethodNotAllowed(message) => json_reply(
                StatusCode::METHOD_NOT_ALLOWED,
                &Json::object([("error", Json::Str(message.clone()))]),
            ),
        }
    }

    fn message(&self) -> String {
        match self {
            Refusal::Explore(
                ExploreError::Invalid(m)
                | ExploreError::NotFound(m)
                | ExploreError::Busy(m)
                | ExploreError::Unavailable(m)
                | ExploreError::Internal(m),
            )
            | Refusal::MethodNotAllowed(m) => m.clone(),
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
    Reader::open(&app.store, frames.as_deref(), phrases).map_err(explore_error)
}

/// A search's log names its pool's size and its phrases, never the pool itself.
fn search_detail(call: &Call) -> String {
    match call {
        Call::Search {
            pool,
            phrase,
            negatives,
            draw,
            ..
        } => format!(
            " pool={} phrase={phrase:?} negatives={} draw={draw:?}",
            pool.len(),
            negatives.len()
        ),
        _ => String::new(),
    }
}

/// Read the request into a Call and answer it off the async threads.
async fn run(app: Shared, request: Request) -> Result<(String, String), Refusal> {
    let method = request.method().clone();
    let path = request.uri().path().to_string();
    let query = request.uri().query().unwrap_or("").to_string();
    // Only a search has a body; any other POST is refused by its route, not its body.
    let body = if method == Method::POST && path == "/searches" {
        Some(Value::Object(read_body(request).await?))
    } else {
        None
    };
    let call = Call::parse(method.as_str(), &path, &query, body.as_ref())?;
    let detail = search_detail(&call);
    let body = tokio::task::spawn_blocking(move || {
        let _slot = Slot::take(&app.store_slots, STORE_SLOTS).ok_or_else(|| {
            ExploreError::Busy(
                "too many store requests are already running; try again in a moment".into(),
            )
        })?;
        Ok::<_, Refusal>(reader(&app, call.needs())?.answer(&call)?)
    })
    .await
    .unwrap_or_else(|join| {
        Err(ExploreError::Internal(format!("the request failed: {join}")).into())
    })?;
    Ok((body, detail))
}

/// Every store route. One log line per request: the method, the path and query as sent,
/// the status, the time taken, and the reply's size or the refusal's message.
pub(super) async fn route(State(app): State<Shared>, request: Request) -> Response {
    let began = Instant::now();
    let target = request
        .uri()
        .path_and_query()
        .map_or_else(|| request.uri().path().to_string(), ToString::to_string);
    let method = request.method().clone();
    let (response, tail) = match run(app, request).await {
        Ok((body, detail)) => {
            let tail = format!("bytes={}{detail}", body.len());
            (respond(StatusCode::OK, "application/json", body), tail)
        }
        Err(refusal) => (refusal.response(), format!("error={:?}", refusal.message())),
    };
    eprintln!(
        "explore: store {method} {target} status={} ms={} {tail}",
        response.status().as_u16(),
        began.elapsed().as_millis()
    );
    response
}
