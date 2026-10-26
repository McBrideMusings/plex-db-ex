//! Reading Tautulli's watch history over its HTTP API — read-only, GET requests
//! only.
//!
//! Tautulli's `/api/v2` has no header-based auth, so the key rides in the query
//! string. Every error below is built from the command and the status, never from
//! the request URL, so the key cannot reach a log through one.

use std::time::Duration;

use plexdb_store::StoreError;
use serde_json::Value;

use crate::http::{self, Reply};
use crate::pyval::get;

const TIMEOUT: Duration = Duration::from_secs(30);
/// `get_history` does not return everything in one response; a real server holds
/// tens of thousands of rows.
const HISTORY_PAGE: usize = 1000;

/// The read surface `enrich-tautulli-plays` needs from Tautulli.
pub trait TautulliSource {
    /// Every history row, completed and in-progress alike. A null `id` is a session
    /// Tautulli has not yet written to its history.
    fn history(&self) -> Result<Vec<Value>, StoreError>;
    /// Every user `get_users` reports, including `user_id` 0's "Local" placeholder.
    fn users(&self) -> Result<Vec<Value>, StoreError>;
}

pub struct TautulliClient {
    base: String,
    key: String,
    agent: ureq::Agent,
}

impl TautulliClient {
    pub fn new(base_url: &str, api_key: &str) -> Result<Self, StoreError> {
        Ok(TautulliClient {
            base: base_url.trim_end_matches('/').to_string(),
            key: api_key.to_string(),
            agent: http::agent(TIMEOUT),
        })
    }

    /// GET one Tautulli `cmd`, returning `response.data`.
    fn call(&self, cmd: &str, extra: &[(&str, String)]) -> Result<Value, StoreError> {
        let mut query = vec![("apikey", self.key.clone()), ("cmd", cmd.to_string())];
        query.extend(extra.iter().map(|(key, value)| (*key, value.clone())));
        let url = format!("{}/api/v2", self.base);
        let body = match http::get(&self.agent, &url, &query, &[]) {
            Reply::Unreachable(_) => {
                return Err(StoreError::Store(format!(
                    "cannot reach Tautulli at {}",
                    self.base
                )));
            }
            Reply::Status(code) => {
                return Err(StoreError::Store(format!(
                    "Tautulli returned {code} for cmd={cmd}"
                )));
            }
            Reply::NotJson(why) => {
                return Err(StoreError::Store(format!(
                    "Tautulli returned a response that is not JSON for cmd={cmd}: {why}"
                )));
            }
            Reply::Json(body) => body,
        };
        let response = get(&body, "response");
        if get(response, "result").as_str() != Some("success") {
            let message = match get(response, "message") {
                Value::Null => "None".to_string(),
                Value::String(text) => text.clone(),
                other => other.to_string(),
            };
            return Err(StoreError::Store(format!(
                "Tautulli reported failure for cmd={cmd}: {message}"
            )));
        }
        Ok(response
            .get("data")
            .cloned()
            .unwrap_or(Value::Object(Default::default())))
    }
}

impl TautulliSource for TautulliClient {
    /// Newest first, paged by `start`/`length`: a page shorter than asked for is
    /// the last one, rather than trusting a `recordsFiltered` total that could shift
    /// while a long fetch is still paging.
    fn history(&self) -> Result<Vec<Value>, StoreError> {
        let mut rows = Vec::new();
        let mut start = 0usize;
        loop {
            let data = self.call(
                "get_history",
                &[
                    ("start", start.to_string()),
                    ("length", HISTORY_PAGE.to_string()),
                    ("order_column", "date".into()),
                    ("order_dir", "desc".into()),
                ],
            )?;
            let page = get(&data, "data").as_array().cloned().unwrap_or_default();
            let short = page.len() < HISTORY_PAGE;
            start += page.len();
            rows.extend(page);
            if short {
                break;
            }
        }
        Ok(rows)
    }

    /// `get_users`'s `response.data` is the bare user list, not `get_history`'s
    /// `{"data": [...]}` wrapper.
    fn users(&self) -> Result<Vec<Value>, StoreError> {
        Ok(self
            .call("get_users", &[])?
            .as_array()
            .cloned()
            .unwrap_or_default())
    }
}
