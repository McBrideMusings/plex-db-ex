//! Reading TMDB's keywords, details, recommendations and similar endpoints over
//! its HTTP API: read-only, GET requests only (`plexdb/tmdb_client.py`).
//!
//! The sweeps depend on [`TmdbSource`], not on [`LiveClient`], so a test can
//! substitute a fake that returns the same shapes without a socket.
//!
//! A movie's keywords live at `/movie/{id}/keywords` as `{"keywords": [...]}`; a
//! show's at `/tv/{id}/keywords` as `{"results": [...]}`. `recommendations` and
//! `similar` live at `/{movie,tv}/{id}/{recommendations,similar}` as `{"results":
//! [{"id": ...}]}`, best first with no rank field: array position is the rank.
//! Only the first page is fetched. An id TMDB has never heard of returns 404,
//! handled as an empty result: a stale id in `external_ids` must not stop a sweep.
//!
//! The API key rides in the query string, so no message here is built from a URL
//! that carries it or from the transport's own error text, which would print it.

use std::time::Duration;

use serde_json::Value;

pub const DEFAULT_BASE: &str = "https://api.themoviedb.org/3";
/// Names a different TMDB-shaped server, for a test or a caching proxy.
pub const BASE_VAR: &str = "TMDB_BASE_URL";
const TIMEOUT: Duration = Duration::from_secs(30);

/// A TMDB request that failed. The message never carries the API key.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct TmdbError(pub String);

/// The read surface an enrichment sweep needs from TMDB — real or recorded.
/// `media_type` is `"movie"` or `"tv"`.
pub trait TmdbSource {
    /// Keyword names TMDB reports for one title; empty both when TMDB has the title
    /// but no keywords and when it has never heard of the id.
    fn keywords(&self, tmdb_id: &str, media_type: &str) -> Result<Vec<String>, TmdbError>;
    /// The ISO 639-1 code of the language a title was made in, or `None`.
    fn original_language(
        &self,
        tmdb_id: &str,
        media_type: &str,
    ) -> Result<Option<String>, TmdbError>;
    /// TMDB ids TMDB recommends for one title, best match first.
    fn recommendations(&self, tmdb_id: &str, media_type: &str) -> Result<Vec<String>, TmdbError>;
    /// TMDB ids TMDB calls similar to one title, best match first.
    fn similar(&self, tmdb_id: &str, media_type: &str) -> Result<Vec<String>, TmdbError>;
}

/// Python truthiness of a JSON value: `if entry.get("name")`.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str(value)` for the scalars TMDB sends.
fn text_of(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn path_segment(media_type: &str) -> &'static str {
    if media_type == "movie" { "movie" } else { "tv" }
}

/// The names in a keywords response body.
pub fn keyword_names(body: &Value, media_type: &str) -> Vec<String> {
    let list = if path_segment(media_type) == "movie" {
        "keywords"
    } else {
        "results"
    };
    body.get(list)
        .and_then(Value::as_array)
        .map(|entries| {
            entries
                .iter()
                .filter_map(|entry| entry.get("name").filter(|name| truthy(name)))
                .map(text_of)
                .collect()
        })
        .unwrap_or_default()
}

/// The ids in a recommendations or similar response body, in TMDB's order.
pub fn related_ids(body: &Value) -> Vec<String> {
    body.get("results")
        .and_then(Value::as_array)
        .map(|entries| {
            entries
                .iter()
                .filter_map(|entry| entry.get("id").filter(|id| !id.is_null()))
                .map(text_of)
                .collect()
        })
        .unwrap_or_default()
}

/// The one [`TmdbSource`] that reaches the TMDB API.
pub struct LiveClient {
    key: String,
    base: String,
    agent: ureq::Agent,
}

impl LiveClient {
    pub fn new(api_key: &str) -> Self {
        let base = std::env::var(BASE_VAR)
            .ok()
            .map(|raw| raw.trim().trim_end_matches('/').to_string())
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| DEFAULT_BASE.to_string());
        Self::with_base(api_key, &base)
    }

    pub fn with_base(api_key: &str, base: &str) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .max_redirects(0)
            .max_redirects_will_error(false)
            .build()
            .into();
        LiveClient {
            key: api_key.to_string(),
            base: base.to_string(),
            agent,
        }
    }

    /// GET one TMDB path: the parsed JSON object, or `None` for a 404, the one
    /// response every endpoint here treats as "nothing", not an error.
    fn get(&self, path: &str) -> Result<Option<Value>, TmdbError> {
        let url = format!("{}/{path}", self.base);
        let mut response = self
            .agent
            .get(&url)
            .query("api_key", &self.key)
            .call()
            .map_err(|err| {
                let kind = match err {
                    ureq::Error::Timeout(_) => "Timeout",
                    ureq::Error::Io(_) => "Io",
                    ureq::Error::HostNotFound => "HostNotFound",
                    ureq::Error::ConnectionFailed => "ConnectionFailed",
                    _ => "Error",
                };
                TmdbError(format!("cannot reach TMDB at {url}: {kind}"))
            })?;
        let status = response.status().as_u16();
        if status == 404 {
            return Ok(None);
        }
        if !(200..300).contains(&status) {
            return Err(TmdbError(format!("TMDB returned {status} for {url}")));
        }
        let not_json = || {
            TmdbError(format!(
                "TMDB returned a response that is not JSON for {url}"
            ))
        };
        let text = response
            .body_mut()
            .read_to_string()
            .map_err(|_| not_json())?;
        match serde_json::from_str::<Value>(&text) {
            Ok(body @ Value::Object(_)) => Ok(Some(body)),
            _ => Err(not_json()),
        }
    }

    fn related(
        &self,
        tmdb_id: &str,
        media_type: &str,
        endpoint: &str,
    ) -> Result<Vec<String>, TmdbError> {
        let path = path_segment(media_type);
        Ok(self
            .get(&format!("{path}/{tmdb_id}/{endpoint}"))?
            .map(|body| related_ids(&body))
            .unwrap_or_default())
    }
}

impl TmdbSource for LiveClient {
    fn keywords(&self, tmdb_id: &str, media_type: &str) -> Result<Vec<String>, TmdbError> {
        let path = path_segment(media_type);
        Ok(self
            .get(&format!("{path}/{tmdb_id}/keywords"))?
            .map(|body| keyword_names(&body, media_type))
            .unwrap_or_default())
    }

    fn original_language(
        &self,
        tmdb_id: &str,
        media_type: &str,
    ) -> Result<Option<String>, TmdbError> {
        let path = path_segment(media_type);
        Ok(self.get(&format!("{path}/{tmdb_id}"))?.and_then(|body| {
            body.get("original_language")
                .filter(|language| truthy(language))
                .map(text_of)
        }))
    }

    fn recommendations(&self, tmdb_id: &str, media_type: &str) -> Result<Vec<String>, TmdbError> {
        self.related(tmdb_id, media_type, "recommendations")
    }

    fn similar(&self, tmdb_id: &str, media_type: &str) -> Result<Vec<String>, TmdbError> {
        self.related(tmdb_id, media_type, "similar")
    }
}
