//! Reading what Wikidata states about titles, by IMDb id, over its SPARQL endpoint:
//! read-only, one query per batch of titles (`plexdb/wikidata_client.py`).
//!
//! The sweep depends on [`WikidataSource`], not on [`LiveWikidataClient`], so a test
//! substitutes a source answering recorded shapes without a socket.
//!
//! One query carries many IMDb ids through `VALUES` on P345 and asks for the English labels of
//! five properties at once, one result row per (IMDb id, property, label). `wdt:` reads only a
//! statement's best rank, so a deprecated narrative location never arrives. An IMDb id Wikidata
//! has no item for contributes no rows, which is how "no match" looks.
//!
//! The query service's documented limits
//! (<https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits>) are a 60 s
//! deadline per query, 60 s of processing per minute per client and 5 parallel queries per IP.
//! This client sends one query at a time and pauses between queries, so a run of slow queries
//! stays below the per-minute budget rather than at it. A 429 carries `Retry-After`, honoured a
//! bounded number of times. Wikidata blocks clients without a descriptive User-Agent
//! (<https://meta.wikimedia.org/wiki/User-Agent_policy>), so every request sends one.

use std::time::{Duration, Instant};

use serde_json::Value;

use crate::backoff::{base_from_env, honoured_seconds, pause, pause_from_env};
use crate::mdblist::quote;

pub const ENDPOINT: &str = "https://query.wikidata.org/sparql";
/// Point the client at another SPARQL server and another pause, for a recorded server
/// (`tools/parity.py`).
pub const BASE_VAR: &str = "WIKIDATA_BASE_URL";
pub const PAUSE_VAR: &str = "WIKIDATA_PAUSE_SECONDS";

/// Who is asking, and where to find out more: what the User-Agent policy asks for.
pub const USER_AGENT: &str = concat!(
    "plexdb/0.1.0 (https://github.com/McBrideMusings/plex-db-ex; ",
    "metadata store over a personal Plex library) ureq"
);

pub const NARRATIVE_LOCATION: &str = "P840";
pub const SET_IN_PERIOD: &str = "P2408";
pub const MAIN_SUBJECT: &str = "P921";
pub const GENRE: &str = "P136";
pub const AWARD_RECEIVED: &str = "P166";
const PROPERTIES: [&str; 5] = [
    NARRATIVE_LOCATION,
    SET_IN_PERIOD,
    MAIN_SUBJECT,
    GENRE,
    AWARD_RECEIVED,
];

const PROP_PREFIX: &str = "http://www.wikidata.org/prop/direct/";

/// Past the service's own 60 s deadline, so its timeout error arrives as a response rather
/// than this client hanging up first.
const TIMEOUT: Duration = Duration::from_secs(65);
/// Pause after every query. With one query in flight at a time this keeps the client well
/// under 60 s of processing per minute even when every query is slow.
const PAUSE_SECONDS: f64 = 1.0;
/// 429s honoured for one batch before it counts as failed.
const MAX_RETRIES: usize = 3;

/// `WikidataError`: the message is what the sweep prints for a failed batch.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct WikidataError(pub String);

/// One statement: (IMDb id, property id such as `P840`, English label).
pub type Statement = (String, String, String);

/// The read surface the Wikidata sweep needs: real or recorded.
pub trait WikidataSource {
    /// Every English-labelled value of the five properties on the items carrying these IMDb
    /// ids, as one query. An id with no Wikidata item, or an item with none of the properties,
    /// contributes nothing.
    fn statements(&mut self, imdb_ids: &[String]) -> Result<Vec<Statement>, WikidataError>;
}

/// A title's IMDb id: `tt` and digits. Nothing else is spliced into a query.
pub fn is_imdb_id(value: &str) -> bool {
    value
        .strip_prefix("tt")
        .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
}

/// The SPARQL for one batch. A value that is not an IMDb id is refused here rather than
/// spliced into the query text; the sweep filters them out first.
pub fn build_query(imdb_ids: &[String]) -> Result<String, WikidataError> {
    for imdb_id in imdb_ids {
        if !is_imdb_id(imdb_id) {
            return Err(WikidataError(format!(
                "not an IMDb id: {}",
                crate::decisions::py_repr(imdb_id)
            )));
        }
    }
    let values: Vec<String> = imdb_ids.iter().map(|id| format!("\"{id}\"")).collect();
    let props: Vec<String> = PROPERTIES.iter().map(|p| format!("wdt:{p}")).collect();
    Ok(format!(
        "SELECT ?imdb ?prop ?label WHERE {{\n  VALUES ?imdb {{ {} }}\n  VALUES ?prop {{ {} }}\n  \
         ?film wdt:P345 ?imdb ; ?prop ?value .\n  ?value rdfs:label ?label .\n  \
         FILTER(LANG(?label) = \"en\")\n}}",
        values.join(" "),
        props.join(" ")
    ))
}

/// SPARQL JSON results -> statements. A row missing a binding is dropped.
pub fn parse_results(body: &Value) -> Vec<Statement> {
    let Some(Value::Array(bindings)) = body
        .get("results")
        .and_then(|results| results.get("bindings"))
    else {
        return Vec::new();
    };
    let text = |binding: &Value, name: &str| {
        binding
            .get(name)
            .and_then(|cell| cell.get("value"))
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    bindings
        .iter()
        .filter_map(|binding| {
            let imdb_id = text(binding, "imdb")?;
            let prop_uri = text(binding, "prop")?;
            let label = text(binding, "label")?;
            let prop = prop_uri.strip_prefix(PROP_PREFIX)?.to_string();
            Some((imdb_id, prop, label))
        })
        .collect()
}

pub struct LiveWikidataClient {
    agent: ureq::Agent,
    endpoint: String,
    pause: f64,
}

impl LiveWikidataClient {
    pub fn new() -> Self {
        Self::with(
            &base_from_env(BASE_VAR, ENDPOINT),
            pause_from_env(PAUSE_VAR, PAUSE_SECONDS),
        )
    }

    pub fn with(endpoint: &str, pause: f64) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .max_redirects(5)
            .user_agent(USER_AGENT)
            .build()
            .into();
        LiveWikidataClient {
            agent,
            endpoint: endpoint.to_string(),
            pause,
        }
    }
}

impl Default for LiveWikidataClient {
    fn default() -> Self {
        Self::new()
    }
}

impl WikidataSource for LiveWikidataClient {
    fn statements(&mut self, imdb_ids: &[String]) -> Result<Vec<Statement>, WikidataError> {
        if imdb_ids.is_empty() {
            return Ok(Vec::new());
        }
        let form = format!("query={}", quote(&build_query(imdb_ids)?));
        for attempt in 0..=MAX_RETRIES {
            let started = Instant::now();
            let mut response = self
                .agent
                .post(&self.endpoint)
                .header("Accept", "application/sparql-results+json")
                .content_type("application/x-www-form-urlencoded")
                .send(&form)
                .map_err(|err| {
                    WikidataError(format!(
                        "cannot reach Wikidata at {}: {}",
                        self.endpoint,
                        match err {
                            ureq::Error::Timeout(_) => "TimeoutException",
                            _ => "ConnectError",
                        }
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
                    WikidataError(format!(
                        "Wikidata asked for a {asked:.0} s wait; giving up on this batch"
                    ))
                })?;
                println!("wikidata: 429 after {elapsed:.1} s, waiting {wait:.0} s");
                pause(wait);
                continue;
            }
            pause(self.pause);
            if status != 200 {
                return Err(WikidataError(format!(
                    "Wikidata returned {status} for a {}-title query after {elapsed:.1} s",
                    imdb_ids.len()
                )));
            }
            let body: Value = response.body_mut().read_json().map_err(|_| {
                WikidataError("Wikidata returned a response that is not JSON".into())
            })?;
            let statements = parse_results(&body);
            println!(
                "wikidata: {} id(s) -> {} statement(s) in {elapsed:.1} s",
                imdb_ids.len(),
                statements.len()
            );
            return Ok(statements);
        }
        Err(WikidataError(format!(
            "Wikidata kept answering 429 after {MAX_RETRIES} retries"
        )))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn only_tt_and_digits_is_an_imdb_id_and_only_that_reaches_a_query() {
        assert!(is_imdb_id("tt1375666"));
        for bad in ["tt", "nm0000001", "tt12a", "TT1", "tt1\" } #"] {
            assert!(!is_imdb_id(bad), "{bad}");
        }
        let query = build_query(&["tt1".into(), "tt22".into()]).expect("a query");
        assert!(query.contains("VALUES ?imdb { \"tt1\" \"tt22\" }"));
        assert!(query.contains("VALUES ?prop { wdt:P840 wdt:P2408 wdt:P921 wdt:P136 wdt:P166 }"));
        assert!(build_query(&["tt1\" } #".into()]).is_err());
    }

    #[test]
    fn a_result_row_missing_a_binding_or_a_direct_property_is_dropped() {
        let cell = |v: &str| json!({"value": v});
        let body = json!({"results": {"bindings": [
            {"imdb": cell("tt1"), "prop": cell("http://www.wikidata.org/prop/direct/P136"),
             "label": cell("heist film")},
            {"imdb": cell("tt1"), "prop": cell("http://www.wikidata.org/prop/direct/P840")},
            {"imdb": cell("tt1"), "prop": cell("http://example.org/P136"), "label": cell("x")}
        ]}});
        assert_eq!(
            parse_results(&body),
            [(
                "tt1".to_string(),
                "P136".to_string(),
                "heist film".to_string()
            )]
        );
        assert!(parse_results(&json!({})).is_empty());
    }
}
