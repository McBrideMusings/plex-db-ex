//! Reading anime tags from AniList's GraphQL API, and the Fribb mapping that finds a title's
//! AniList ids: read-only and keyless (`plexdb/anilist_client.py`).
//!
//! The sweep depends on [`AniListSource`], not on [`LiveAniListClient`], so a test substitutes
//! a source answering recorded shapes without a socket.
//!
//! AniList carries no TMDB, IMDb or TVDB id; its only cross id is `idMal`. Fribb's
//! `anime-list-full.json` (<https://github.com/Fribb/anime-lists>) is the bridge: one entry per
//! AniList/MAL title with the TMDB, IMDb and TVDB ids it corresponds to. It is fetched whole,
//! once per sweep.
//!
//! Tags come from `Page { media(id_in: [...]) }`, up to [`PAGE_SIZE`] ids per request. An id
//! AniList has no entry for is absent from the page. The API's limit was 30 requests a minute
//! (`X-RateLimit-Limit: 30`), so the client pauses [`PAUSE_SECONDS`] after every request and
//! stays under it with one request in flight. A 429 carries `Retry-After`, honoured a bounded
//! number of times (<https://docs.anilist.co/guide/rate-limiting>).

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::backoff::{base_from_env, honoured_seconds, pause, pause_from_env};
use crate::pyval::truthy;

pub const ENDPOINT: &str = "https://graphql.anilist.co";
pub const MAPPING_URL: &str =
    "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json";
/// Point the client at another GraphQL server, another mapping file and another pause, for a
/// recorded server (`tools/parity.py`).
pub const BASE_VAR: &str = "ANILIST_BASE_URL";
pub const MAPPING_VAR: &str = "ANILIST_MAPPING_URL";
pub const PAUSE_VAR: &str = "ANILIST_PAUSE_SECONDS";

/// AniList's own ceiling on `perPage`: a request asks for at most this many ids, so every
/// match fits on the first page.
pub const PAGE_SIZE: usize = 50;

const QUERY: &str = "query ($ids: [Int], $perPage: Int) {
  Page(perPage: $perPage) {
    media(id_in: $ids, type: ANIME) {
      id
      tags { name rank category isMediaSpoiler isGeneralSpoiler }
    }
  }
}";

const TIMEOUT: Duration = Duration::from_secs(30);
/// 60 s / 30 requests, plus a margin, so one request in flight never trips the limit.
const PAUSE_SECONDS: f64 = 2.1;
/// 429s honoured for one request before it counts as failed.
const MAX_RETRIES: usize = 3;

/// `AniListError`: the message is what the sweep prints for a failed batch.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct AniListError(pub String);

/// One AniList tag on one media entry, as AniList gave it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tag {
    pub name: String,
    /// AniList's 0-100 rank: the share of voters who agree the tag applies.
    pub rank: Option<i64>,
    pub category: Option<String>,
    /// `isMediaSpoiler or isGeneralSpoiler`.
    pub spoiler: bool,
}

/// The read surface the AniList sweep needs: real or recorded.
pub trait AniListSource {
    /// Fribb's `anime-list-full.json`, as its list of entries.
    fn mapping(&mut self) -> Result<Vec<Value>, AniListError>;
    /// Every tag on each of these AniList ids. An id AniList has no entry for is absent.
    fn tags(&mut self, anilist_ids: &[i64]) -> Result<BTreeMap<i64, Vec<Tag>>, AniListError>;
}

/// One page of GraphQL JSON -> tags per AniList id. A tag with no name is dropped.
pub fn parse_media(body: &Value) -> BTreeMap<i64, Vec<Tag>> {
    let mut out = BTreeMap::new();
    let page = body
        .get("data")
        .filter(|v| truthy(v))
        .and_then(|data| data.get("Page"))
        .filter(|v| truthy(v));
    let Some(Value::Array(media)) = page
        .and_then(|page| page.get("media"))
        .filter(|v| truthy(v))
    else {
        return out;
    };
    for entry in media {
        let Some(id) = entry.get("id").and_then(Value::as_i64) else {
            continue;
        };
        let mut tags = Vec::new();
        if let Some(Value::Array(raw_tags)) = entry.get("tags").filter(|v| truthy(v)) {
            for raw in raw_tags {
                let Some(name) = raw.get("name").and_then(Value::as_str) else {
                    continue;
                };
                if plexdb_store::py_strip(name).is_empty() {
                    continue;
                }
                let rank = match raw.get("rank") {
                    Some(Value::Bool(flag)) => Some(i64::from(*flag)),
                    Some(value) => value.as_i64(),
                    None => None,
                };
                tags.push(Tag {
                    name: name.to_string(),
                    rank,
                    category: raw
                        .get("category")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                    spoiler: ["isMediaSpoiler", "isGeneralSpoiler"]
                        .iter()
                        .any(|key| raw.get(key).is_some_and(truthy)),
                });
            }
        }
        out.insert(id, tags);
    }
    out
}

pub struct LiveAniListClient {
    agent: ureq::Agent,
    endpoint: String,
    mapping_url: String,
    pause: f64,
}

impl LiveAniListClient {
    pub fn new() -> Self {
        Self::with(
            &base_from_env(BASE_VAR, ENDPOINT),
            &base_from_env(MAPPING_VAR, MAPPING_URL),
            pause_from_env(PAUSE_VAR, PAUSE_SECONDS),
        )
    }

    pub fn with(endpoint: &str, mapping_url: &str, pause: f64) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .max_redirects(5)
            .build()
            .into();
        LiveAniListClient {
            agent,
            endpoint: endpoint.to_string(),
            mapping_url: mapping_url.to_string(),
            pause,
        }
    }

    fn request(&mut self, ids: &[i64]) -> Result<BTreeMap<i64, Vec<Tag>>, AniListError> {
        for attempt in 0..=MAX_RETRIES {
            let started = Instant::now();
            let payload = json!({"query": QUERY, "variables": {"ids": ids, "perPage": PAGE_SIZE}});
            let mut response = self
                .agent
                .post(&self.endpoint)
                .header("Accept", "application/json")
                .send_json(&payload)
                .map_err(|err| {
                    AniListError(format!(
                        "cannot reach AniList at {}: {}",
                        self.endpoint,
                        transport_name(&err)
                    ))
                })?;
            let elapsed = started.elapsed().as_secs_f64();
            let status = response.status().as_u16();
            if status == 429 {
                if attempt == MAX_RETRIES {
                    break;
                }
                let raw = response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("")
                    .to_string();
                let wait = honoured_seconds(&raw).map_err(|asked| {
                    AniListError(format!(
                        "AniList asked for a {asked:.0} s wait; giving up on this batch"
                    ))
                })?;
                println!("anilist: 429 after {elapsed:.1} s, waiting {wait:.0} s");
                pause(wait);
                continue;
            }
            pause(self.pause);
            if status != 200 {
                return Err(AniListError(format!(
                    "AniList returned {status} for {} id(s) after {elapsed:.1} s",
                    ids.len()
                )));
            }
            let body: Value = response
                .body_mut()
                .read_json()
                .map_err(|_| AniListError("AniList returned a response that is not JSON".into()))?;
            if let Some(errors) = body.get("errors").filter(|v| truthy(v)) {
                let messages: Vec<String> = errors
                    .as_array()
                    .map(|list| {
                        list.iter()
                            .filter(|e| e.is_object())
                            .map(|e| {
                                e.get("message")
                                    .map_or_else(|| "None".into(), crate::pyval::py_str)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                return Err(AniListError(format!(
                    "AniList refused the query: {}",
                    messages.join("; ")
                )));
            }
            let found = parse_media(&body);
            println!(
                "anilist: {} id(s) -> {} media, {} tag(s) in {elapsed:.1} s",
                ids.len(),
                found.len(),
                found.values().map(Vec::len).sum::<usize>()
            );
            return Ok(found);
        }
        Err(AniListError(format!(
            "AniList kept answering 429 after {MAX_RETRIES} retries"
        )))
    }
}

impl Default for LiveAniListClient {
    fn default() -> Self {
        Self::new()
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

impl AniListSource for LiveAniListClient {
    fn mapping(&mut self) -> Result<Vec<Value>, AniListError> {
        let started = Instant::now();
        let url = &self.mapping_url;
        let mut response = self.agent.get(url).call().map_err(|err| {
            AniListError(format!(
                "cannot reach the anime mapping at {url}: {}",
                transport_name(&err)
            ))
        })?;
        let status = response.status().as_u16();
        if status != 200 {
            return Err(AniListError(format!("the anime mapping returned {status}")));
        }
        let text = response
            .body_mut()
            .with_config()
            .limit(256 << 20)
            .read_to_string()
            .map_err(|_| AniListError("the anime mapping is not JSON".into()))?;
        let body: Value = serde_json::from_str(&text)
            .map_err(|_| AniListError("the anime mapping is not JSON".into()))?;
        let Value::Array(items) = body else {
            return Err(AniListError(
                "the anime mapping is not a list of entries".into(),
            ));
        };
        let entries: Vec<Value> = items.into_iter().filter(Value::is_object).collect();
        println!(
            "anilist: mapping {} entries in {:.1} s",
            entries.len(),
            started.elapsed().as_secs_f64()
        );
        Ok(entries)
    }

    fn tags(&mut self, anilist_ids: &[i64]) -> Result<BTreeMap<i64, Vec<Tag>>, AniListError> {
        let mut ids: Vec<i64> = Vec::new();
        for id in anilist_ids {
            if !ids.contains(id) {
                ids.push(*id);
            }
        }
        let mut out = BTreeMap::new();
        for chunk in ids.chunks(PAGE_SIZE) {
            out.extend(self.request(chunk)?);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn a_page_gives_tags_per_media_and_drops_a_nameless_tag() {
        let body = json!({"data": {"Page": {"media": [
            {"id": 1, "tags": [
                {"name": "Heist", "rank": 80, "category": "Theme-Other", "isMediaSpoiler": false},
                {"name": " ", "rank": 5},
                {"name": "Twist", "rank": 40, "isGeneralSpoiler": true}
            ]},
            {"id": "x", "tags": []},
            {"id": 2, "tags": null}
        ]}}});
        let found = parse_media(&body);
        assert_eq!(found.keys().copied().collect::<Vec<_>>(), [1, 2]);
        assert_eq!(
            found[&1],
            [
                Tag {
                    name: "Heist".into(),
                    rank: Some(80),
                    category: Some("Theme-Other".into()),
                    spoiler: false
                },
                Tag {
                    name: "Twist".into(),
                    rank: Some(40),
                    category: None,
                    spoiler: true
                },
            ]
        );
        assert!(found[&2].is_empty());
        assert!(parse_media(&json!({"data": null})).is_empty());
    }
}
