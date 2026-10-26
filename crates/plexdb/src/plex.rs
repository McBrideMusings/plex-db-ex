//! Reading a Plex library over its HTTP API — read-only, GET requests only.
//!
//! The steps depend on [`PlexSource`], not on [`PlexClient`], so a test hands them a
//! recorded library without a socket. Section types and metadata types follow
//! Plex's own numbering: a section is `movie` or `show`, and an item listed from
//! it is typed `1` (movie), `2` (show) or `4` (episode). A show section is walked
//! twice, once at type 2 for the show's own ids and once at type 4 for its
//! episodes, because an episode's `Guid` list is the episode's identity.

use std::time::Duration;

use plexdb_store::StoreError;
use serde_json::Value;

use crate::http::{self, Reply};
use crate::pyval::{get, or, py_str};

pub const TYPE_MOVIE: i64 = 1;
pub const TYPE_SHOW: i64 = 2;
pub const TYPE_EPISODE: i64 = 4;

const TIMEOUT: Duration = Duration::from_secs(60);
/// `/status/sessions/history/all` does not return everything in one response.
const HISTORY_PAGE: usize = 200;
/// `/library/sections/<key>/all` pages small enough to stay inside the timeout.
const SECTION_PAGE: usize = 500;

/// One library section, as `/library/sections` lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Section {
    pub key: String,
    /// `movie`, `show`, or something the steps skip (`photo`).
    pub kind: String,
    pub title: String,
}

/// Section type -> the `(items row type, Plex item type)` passes to run over it.
/// A show section is read twice, shows before episodes, so an episode's
/// `show_item_id` resolves against a show written moments earlier. A type absent
/// here is skipped rather than guessed at.
pub fn section_passes(kind: &str) -> &'static [(&'static str, i64)] {
    match kind {
        "movie" => &[("movie", TYPE_MOVIE)],
        "show" => &[("show", TYPE_SHOW), ("episode", TYPE_EPISODE)],
        _ => &[],
    }
}

/// The read surface the steps need from Plex.
pub trait PlexSource {
    fn sections(&self) -> Result<Vec<Section>, StoreError>;
    /// Raw `Metadata` records for one section at one Plex item type.
    fn items(&self, section_key: &str, plex_type: i64) -> Result<Vec<Value>, StoreError>;
    /// The one full `Metadata` record Plex holds for `rating_key`, streams included.
    fn metadata(&self, rating_key: &str) -> Result<Option<Value>, StoreError>;
    /// Every history event at or newer than `since_viewed_at`, newest first.
    fn history(&self, since_viewed_at: Option<i64>) -> Result<Vec<Value>, StoreError>;
    fn devices(&self) -> Result<Vec<Value>, StoreError>;
    fn accounts(&self) -> Result<Vec<Value>, StoreError>;
}

/// The one [`PlexSource`] that reaches a real server. Every method issues a GET.
pub struct PlexClient {
    base: String,
    token: String,
    agent: ureq::Agent,
}

impl PlexClient {
    pub fn new(base_url: &str, token: &str) -> Result<Self, StoreError> {
        Ok(PlexClient {
            base: base_url.trim_end_matches('/').to_string(),
            token: token.to_string(),
            agent: http::agent(TIMEOUT),
        })
    }

    /// `MediaContainer` of one GET. The token rides in a header, not the query
    /// string, so it never lands in a proxy or access log.
    fn container(&self, path: &str, query: &[(&str, String)]) -> Result<Value, StoreError> {
        let url = format!("{}{path}", self.base);
        let reply = http::get(
            &self.agent,
            &url,
            query,
            &[
                ("Accept", "application/json"),
                ("X-Plex-Token", &self.token),
            ],
        );
        match reply {
            Reply::Unreachable(why) => Err(StoreError::Store(format!(
                "cannot reach Plex at {url}: {why}"
            ))),
            Reply::Status(code) => Err(StoreError::Store(format!(
                "cannot reach Plex at {url}: HTTP status {code}"
            ))),
            Reply::NotJson(why) => Err(StoreError::Store(format!(
                "Plex returned a response that is not JSON for {url}: {why}"
            ))),
            Reply::Json(Value::Object(mut body)) => Ok(body
                .remove("MediaContainer")
                .unwrap_or(Value::Object(Default::default()))),
            Reply::Json(other) => Err(StoreError::Store(format!(
                "Plex returned a {} body for {url}, expected an object",
                json_type(&other)
            ))),
        }
    }

    fn list(&self, path: &str, key: &str) -> Result<Vec<Value>, StoreError> {
        Ok(array(&self.container(path, &[])?, key))
    }
}

fn json_type(value: &Value) -> &'static str {
    match value {
        Value::Array(_) => "list",
        Value::String(_) => "str",
        Value::Number(_) => "int",
        Value::Bool(_) => "bool",
        Value::Null => "NoneType",
        Value::Object(_) => "dict",
    }
}

fn array(container: &Value, key: &str) -> Vec<Value> {
    get(container, key).as_array().cloned().unwrap_or_default()
}

impl PlexSource for PlexClient {
    fn sections(&self) -> Result<Vec<Section>, StoreError> {
        let container = self.container("/library/sections", &[])?;
        let mut sections = Vec::new();
        for entry in array(&container, "Directory") {
            let (key, kind) = (get(&entry, "key"), get(&entry, "type"));
            if key.is_null() || kind.is_null() {
                continue;
            }
            sections.push(Section {
                key: py_str(key),
                kind: py_str(kind),
                title: py_str(or(get(&entry, "title"), &Value::String(String::new()))),
            });
        }
        Ok(sections)
    }

    /// Every record of one Plex type in a section, a page at a time so no single
    /// response runs into the timeout. Stops at the container's `totalSize`, or on a
    /// short page when Plex reports none.
    fn items(&self, section_key: &str, plex_type: i64) -> Result<Vec<Value>, StoreError> {
        let mut records = Vec::new();
        let mut start = 0usize;
        loop {
            let container = self.container(
                &format!("/library/sections/{section_key}/all"),
                &[
                    ("type", plex_type.to_string()),
                    ("includeGuids", "1".into()),
                    ("X-Plex-Container-Start", start.to_string()),
                    ("X-Plex-Container-Size", SECTION_PAGE.to_string()),
                ],
            )?;
            let page = array(&container, "Metadata");
            start += page.len();
            let total = get(&container, "totalSize").as_u64();
            let done = page.is_empty()
                || match total {
                    Some(total) => start as u64 >= total,
                    None => page.len() < SECTION_PAGE,
                };
            records.extend(page);
            if done {
                break;
            }
        }
        Ok(records)
    }

    fn metadata(&self, rating_key: &str) -> Result<Option<Value>, StoreError> {
        let records = self.list(&format!("/library/metadata/{rating_key}"), "Metadata")?;
        Ok(records.into_iter().next())
    }

    /// Filters server-side with Plex's own `viewedAt>` operator, which is inclusive
    /// in practice: the newest event already on file comes back again, and
    /// `ON CONFLICT DO NOTHING` makes that free. Paginated, since a history runs to
    /// tens of thousands of rows.
    fn history(&self, since_viewed_at: Option<i64>) -> Result<Vec<Value>, StoreError> {
        let mut events = Vec::new();
        let mut start = 0usize;
        loop {
            let mut query = vec![("sort", "viewedAt:desc".to_string())];
            if let Some(since) = since_viewed_at {
                query.push(("viewedAt>", since.to_string()));
            }
            query.push(("X-Plex-Container-Start", start.to_string()));
            query.push(("X-Plex-Container-Size", HISTORY_PAGE.to_string()));
            let container = self.container("/status/sessions/history/all", &query)?;
            let page = array(&container, "Metadata");
            start += page.len();
            let total = get(&container, "totalSize")
                .as_u64()
                .unwrap_or(start as u64);
            let done = page.is_empty() || start as u64 >= total;
            events.extend(page);
            if done {
                break;
            }
        }
        Ok(events)
    }

    fn devices(&self) -> Result<Vec<Value>, StoreError> {
        self.list("/devices", "Device")
    }

    fn accounts(&self) -> Result<Vec<Value>, StoreError> {
        self.list("/accounts", "Account")
    }
}
