//! Reading MDBList's top lists, one list and its entries over its HTTP API, GET only
//! (`plexdb/mdblist_client.py`).
//!
//! The harvests depend on [`MdbSource`], not on [`LiveMdbClient`], so a test
//! substitutes a source answering recorded shapes without a socket.
//!
//! `GET /lists/top` answers a JSON array of lists; `GET /lists/{id}/items` answers an
//! **object** (`movies`, `shows`, `pagination`), paged by `has_more`, and each entry's own
//! `rank` is a different ordering from the list's, so only array position is used. A
//! `User-Agent` is required: without one every endpoint answers `403` with the body
//! `error code: 1010`, which reads as a dead key.

use std::time::Duration;

use serde_json::Value;

use crate::pyval::{optional_int, optional_text, py_int, py_str, py_type_name, truthy};

pub const DEFAULT_BASE: &str = "https://api.mdblist.com";
/// Points the client elsewhere, for a recorded server (`tools/parity.py`).
pub const BASE_VAR: &str = "MDBLIST_BASE_URL";
const TIMEOUT: Duration = Duration::from_secs(30);
const USER_AGENT: &str = "plexdb";
const PAGE_LIMIT: i64 = 1000;
const MAX_PAGES: usize = 100;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// MDBList answered, but not with what was asked for.
    Other,
    /// 429: the daily quota is spent.
    Quota,
    /// MDBList never answered.
    Unreachable,
}

/// `MDBListError` and its two subclasses.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct MdbError {
    pub kind: Kind,
    pub status: Option<u16>,
    pub message: String,
}

impl MdbError {
    pub fn new(message: impl Into<String>, status: Option<u16>) -> Self {
        MdbError {
            kind: Kind::Other,
            status,
            message: message.into(),
        }
    }

    /// A failure that says nothing about one list: the quota is spent, the key is
    /// refused, or MDBList never answered (`collections.is_systemic`).
    pub fn is_systemic(&self) -> bool {
        self.kind != Kind::Other || matches!(self.status, Some(401 | 403))
    }

    /// `HTTP {status or 'none'}` of the harvests' log lines.
    pub fn status_text(&self) -> String {
        match self.status {
            Some(status) if status != 0 => status.to_string(),
            _ => "none".into(),
        }
    }
}

/// One list, as MDBList reported it. No derived values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MdbList {
    pub list_id: i64,
    pub name: String,
    pub slug: String,
    pub user_name: String,
    pub size: Option<i64>,
    pub likes: Option<i64>,
}

/// One title on a list: every id it carried in the store's priority order, and its
/// media type (`movie` or `show`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MdbEntry {
    pub external_ids: Vec<(String, String)>,
    pub media_type: String,
    pub title: Option<String>,
    pub year: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MdbContents {
    pub status: u16,
    pub entries: Vec<MdbEntry>,
}

/// The read surface a collection harvest needs from MDBList: real or recorded.
pub trait MdbSource {
    fn top_lists(&mut self) -> Result<Vec<MdbList>, MdbError>;
    fn find_list(&mut self, user_name: &str, slug: &str) -> Result<MdbList, MdbError>;
    fn list_entries(&mut self, list_id: i64) -> Result<MdbContents, MdbError>;
    fn search_lists(&mut self, query: &str) -> Result<Vec<MdbList>, MdbError>;
}

pub struct LiveMdbClient {
    key: String,
    base: String,
    agent: ureq::Agent,
    last_status: u16,
}

impl LiveMdbClient {
    pub fn new(key: &str) -> Self {
        let base = std::env::var(BASE_VAR)
            .ok()
            .map(|raw| raw.trim().trim_end_matches('/').to_string())
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| DEFAULT_BASE.to_string());
        Self::with_base(key, &base)
    }

    pub fn with_base(key: &str, base: &str) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .max_redirects(0)
            .max_redirects_will_error(false)
            .user_agent(USER_AGENT)
            .build()
            .into();
        LiveMdbClient {
            key: key.to_string(),
            base: base.to_string(),
            agent,
            last_status: 0,
        }
    }

    /// One GET with the key in the query, never in a message: every message below is
    /// built from `url` without its query and the status, so a refused key never
    /// prints itself.
    fn get(&mut self, path: &str, extra: &[(&str, String)]) -> Result<Value, MdbError> {
        let url = format!("{}{path}", self.base);
        let mut request = self.agent.get(&url).query("apikey", &self.key);
        for (name, value) in extra {
            request = request.query(name, value);
        }
        let mut response = request.call().map_err(|err| MdbError {
            kind: Kind::Unreachable,
            status: None,
            message: format!("cannot reach MDBList at {url}: {}", transport_name(&err)),
        })?;
        let status = response.status().as_u16();
        self.last_status = status;
        if status == 403 {
            return Err(MdbError::new(
                format!(
                    "MDBList returned 403 for {url}. Either MDBLIST_API_KEY is not valid, or the \
                     request carried no User-Agent — the service answers 403 with body \
                     'error code: 1010' to an agentless request holding a perfectly good key."
                ),
                Some(403),
            ));
        }
        if status == 429 {
            let header = |name: &str| {
                response
                    .headers()
                    .get(name)
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("not sent")
                    .to_string()
            };
            return Err(MdbError {
                kind: Kind::Quota,
                status: Some(429),
                message: format!(
                    "MDBList returned 429 for {url}: the daily request quota is spent \
                     (x-ratelimit-remaining {}, resets at epoch {}); harvest-mdblist and \
                     enrich-mdblist-ratings share it",
                    header("x-ratelimit-remaining"),
                    header("x-ratelimit-reset"),
                ),
            });
        }
        if status >= 300 {
            return Err(MdbError::new(
                format!("MDBList returned {status} for {url}"),
                Some(status),
            ));
        }
        let text = response
            .body_mut()
            .with_config()
            .limit(512 << 20)
            .read_to_string()
            .map_err(|_| {
                MdbError::new(
                    format!("MDBList returned a response that is not JSON for {url}"),
                    None,
                )
            })?;
        serde_json::from_str(&text).map_err(|_| {
            MdbError::new(
                format!("MDBList returned a response that is not JSON for {url}"),
                None,
            )
        })
    }

    fn lists_from(
        &mut self,
        path: &str,
        extra: &[(&str, String)],
    ) -> Result<Vec<MdbList>, MdbError> {
        let body = self.get(path, extra)?;
        let Value::Array(items) = body else {
            return Err(MdbError::new(
                format!(
                    "MDBList {path} returned {}, expected a list of lists",
                    py_type_name(&body)
                ),
                None,
            ));
        };
        Ok(items
            .iter()
            .filter(|entry| entry.get("id").is_some_and(|id| !id.is_null()))
            .filter_map(list_from)
            .collect())
    }
}

fn transport_name(err: &ureq::Error) -> &'static str {
    match err {
        ureq::Error::Timeout(_) => "TimeoutException",
        ureq::Error::Io(_) | ureq::Error::HostNotFound | ureq::Error::ConnectionFailed => {
            "ConnectError"
        }
        _ => "TransportError",
    }
}

/// `urllib.parse.quote(text, safe='')`.
pub fn quote(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

impl MdbSource for LiveMdbClient {
    fn top_lists(&mut self) -> Result<Vec<MdbList>, MdbError> {
        self.lists_from("/lists/top", &[])
    }

    fn search_lists(&mut self, query: &str) -> Result<Vec<MdbList>, MdbError> {
        self.lists_from("/lists/search", &[("query", query.to_string())])
    }

    fn find_list(&mut self, user_name: &str, slug: &str) -> Result<MdbList, MdbError> {
        let path = format!("/lists/{}/{}", quote(user_name), quote(slug));
        let body = self.get(&path, &[])?;
        let found = match &body {
            Value::Array(items) if !items.is_empty() => &items[0],
            other => other,
        };
        let usable = found.is_object() && found.get("id").is_some_and(|id| !id.is_null());
        if !usable {
            return Err(MdbError::new(
                format!("MDBList {path} returned no list"),
                None,
            ));
        }
        list_from(found)
            .ok_or_else(|| MdbError::new(format!("MDBList {path} returned no list"), None))
    }

    fn list_entries(&mut self, list_id: i64) -> Result<MdbContents, MdbError> {
        let mut entries = Vec::new();
        let mut offset: i64 = 0;
        for _ in 0..MAX_PAGES {
            let body = self.get(
                &format!("/lists/{list_id}/items"),
                &[
                    ("limit", PAGE_LIMIT.to_string()),
                    ("offset", offset.to_string()),
                ],
            )?;
            let Value::Object(map) = &body else {
                return Err(MdbError::new(
                    format!(
                        "MDBList /lists/{list_id}/items returned {}, expected an object with \
                         movies/shows/pagination",
                        py_type_name(&body)
                    ),
                    None,
                ));
            };
            // Both arrays, separately: movies before shows within a page keeps the
            // order deterministic.
            for (media_type, key) in [("movie", "movies"), ("show", "shows")] {
                let Some(Value::Array(items)) = map.get(key).filter(|v| truthy(v)) else {
                    continue;
                };
                for item in items {
                    let ids = external_ids(item);
                    if !ids.is_empty() {
                        entries.push(MdbEntry {
                            external_ids: ids,
                            media_type: media_type.into(),
                            title: optional_text(item.get("title")),
                            year: optional_int(item.get("release_year")),
                        });
                    }
                }
            }
            let more = map
                .get("pagination")
                .filter(|v| truthy(v))
                .and_then(|p| p.get("has_more"))
                .is_some_and(truthy);
            if !more {
                return Ok(MdbContents {
                    status: self.last_status,
                    entries,
                });
            }
            offset += PAGE_LIMIT;
        }
        Err(MdbError::new(
            format!(
                "MDBList list {list_id} still reported more pages after {MAX_PAGES} requests — \
                 refusing to page further"
            ),
            None,
        ))
    }
}

fn list_from(entry: &Value) -> Option<MdbList> {
    let id = entry.get("id")?;
    let list_id = py_int(id)?;
    let text_or = |value: Option<&Value>, fallback: String| match value.filter(|v| truthy(v)) {
        Some(value) => py_str(value),
        None => fallback,
    };
    let user_name = entry
        .get("user_name")
        .filter(|v| truthy(v))
        .or_else(|| entry.get("user").filter(|v| truthy(v)));
    Some(MdbList {
        list_id,
        name: text_or(entry.get("name"), format!("MDBList {}", py_str(id))),
        slug: text_or(entry.get("slug"), String::new()),
        user_name: text_or(user_name, String::new()),
        size: optional_int(entry.get("items")),
        likes: optional_int(entry.get("likes")),
    })
}

/// Every id one entry carries, in the store's priority order. MDBList nests them under
/// `ids` and repeats some at the top level (`imdb_id`, `tvdb_id`, `id`); both are read,
/// `ids` first.
fn external_ids(item: &Value) -> Vec<(String, String)> {
    let nested = item.get("ids").filter(|v| truthy(v));
    let pick = |inner: &str, flat: &str| -> Option<Value> {
        nested
            .and_then(|n| n.get(inner))
            .filter(|v| truthy(v))
            .or_else(|| item.get(flat))
            .cloned()
    };
    [
        ("imdb", pick("imdb", "imdb_id")),
        ("tmdb", pick("tmdb", "id")),
        ("tvdb", pick("tvdb", "tvdb_id")),
    ]
    .into_iter()
    .filter_map(|(ns, value)| {
        let value = value?;
        if value.is_null() || value.as_str() == Some("") {
            return None;
        }
        Some((ns.to_string(), py_str(&value)))
    })
    .collect()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn an_entry_keeps_every_id_it_carries_in_priority_order() {
        let item = json!({
            "id": 27205, "imdb_id": "tt1375666", "ids": {"tmdb": 27205, "imdb": null, "tvdb": ""}
        });
        assert_eq!(
            external_ids(&item),
            [
                ("imdb".to_string(), "tt1375666".to_string()),
                ("tmdb".to_string(), "27205".to_string()),
            ]
        );
    }

    #[test]
    fn a_zero_id_falls_back_to_the_flat_field() {
        let item = json!({"ids": {"tmdb": 0}, "id": 9});
        assert_eq!(external_ids(&item), [("tmdb".to_string(), "9".to_string())]);
    }

    #[test]
    fn a_list_without_a_name_is_named_for_its_id() {
        let list = list_from(&json!({"id": 14, "slug": "x", "user": "lina", "items": "300"}));
        let list = list.expect("a list");
        assert_eq!(list.name, "MDBList 14");
        assert_eq!(list.user_name, "lina");
        assert_eq!(list.size, Some(300));
        assert_eq!(list.likes, None);
    }

    #[test]
    fn quote_leaves_only_unreserved_characters() {
        assert_eq!(quote("Lina B/x y~"), "Lina%20B%2Fx%20y~");
        assert_eq!(quote("é"), "%C3%A9");
    }
}
