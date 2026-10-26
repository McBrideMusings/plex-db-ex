//! Asking Jev whether two keywords mean the same thing, and what roles one keyword plays
//! (`plexdb/jev_client.py`).
//!
//! The judge steps depend on [`Jev`], not on [`LiveJev`], so a test can substitute a
//! fake that answers without a socket.
//!
//! Jev's `noul` answer is a probability between 0 and 1 that the statement is true. The
//! pair request also carries a `score` question; Jev answers both from one state, and
//! only the `noul` answer is kept (ADR-0018 stores the judge's own answer, not a
//! conclusion drawn from it). The role request carries one `noul` question per role in
//! [`ROLES`], all about the same keyword.

use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use serde_json::{Value, json};

use crate::frame_encoder::error_kind;
use crate::role_decisions::ROLES;

pub const DEFAULT_URL: &str = "https://api.typesafe.ai/v1/systemone";
/// Names a different Jev-shaped server, for a test or a proxy.
pub const URL_VAR: &str = "JEV_URL";

/// Statuses that mean this request is invalid. A bad key (401, 403) or an outage (5xx)
/// would refuse every pair, so those stay plain errors that stop the run.
const REJECTED_STATUSES: [u16; 2] = [400, 422];
const MODEL: &str = "jev-latest";
const TIMEOUT: Duration = Duration::from_secs(120);
const ATTEMPTS: u32 = 8;
const BACKOFF: Duration = Duration::from_millis(500);

const SCORE_INSTRUCTIONS: &str = "Two tags from a movie and TV keyword database. Would a viewer \
who searches or likes one tag be equally well served by the other, so that merging them into \
one tag loses nothing?";
const SCORE_CRITERIA: [&str; 3] = [
    "different tags: related at most, merging would lose information",
    "overlapping but not interchangeable, a curator should decide",
    "same meaning: interchangeable synonyms, safe to merge",
];
const NOUL_INSTRUCTIONS: &str = "Do the two tags mean the same thing?";

const ROLE_CONTEXT: &str = "A tag attached to movies and TV shows in a keyword database.";

/// The `noul` question for one role, in [`ROLES`] order.
const ROLE_INSTRUCTIONS: [&str; 5] = [
    "The tag names a tone or mood a title has, such as bleak, whimsical or tense.",
    "The tag names a time period a title is set in, such as the 1970s or the Victorian era.",
    "The tag names a place a title is set in, such as Los Angeles, Tokyo or Scotland.",
    "The tag names a theme a title is about, such as grief, revenge or redemption.",
    "The tag names a trait of a title's characters, such as antihero, genius or loner.",
];

/// A request Jev did not answer.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum JevFailure {
    /// Jev refused this one request (400, 422). The run goes on without it.
    #[error("{0}")]
    Rejected(String),
    /// Anything else: 429 exhausted, 5xx, a bad key, no connection. The run stops.
    #[error("{0}")]
    Error(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Verdict {
    /// Jev's `noul` answer, 0 to 1, exactly as given.
    pub score: f64,
    /// The model that answered, e.g. `jev-1.13.0` — not the `jev-latest` alias asked for.
    pub model: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RoleVerdict {
    /// Jev's `noul` answer per role, in [`ROLES`] order, 0 to 1, exactly as given.
    pub scores: [f64; 5],
    pub model: String,
}

pub trait Jev: Sync {
    /// Whether two readable keyword spellings mean the same thing.
    fn judge(&self, tag_a: &str, tag_b: &str) -> Result<Verdict, JevFailure>;
    /// How likely one readable keyword spelling is to play each role.
    fn judge_roles(&self, tag: &str) -> Result<RoleVerdict, JevFailure>;
}

/// `ask(item)` for every item on `workers` threads, results in order. An item Jev refuses
/// comes back as its [`JevFailure::Rejected`] message in its place; any other failure
/// stops the calls not yet started and is returned alone (the earliest item's, when
/// several fail).
pub fn judge_all<I: Sync, T: Send>(
    ask: impl Fn(&I) -> Result<T, JevFailure> + Sync,
    inputs: &[I],
    workers: usize,
) -> Result<Vec<Result<T, String>>, String> {
    let next = AtomicUsize::new(0);
    let abort = AtomicBool::new(false);
    let slots: Mutex<Vec<Option<Result<T, JevFailure>>>> =
        Mutex::new((0..inputs.len()).map(|_| None).collect());
    std::thread::scope(|scope| {
        for _ in 0..workers.min(inputs.len()) {
            scope.spawn(|| {
                while !abort.load(Ordering::SeqCst) {
                    let index = next.fetch_add(1, Ordering::SeqCst);
                    let Some(input) = inputs.get(index) else {
                        return;
                    };
                    let outcome = ask(input);
                    if matches!(outcome, Err(JevFailure::Error(_))) {
                        abort.store(true, Ordering::SeqCst);
                    }
                    slots.lock().expect("slots lock")[index] = Some(outcome);
                }
            });
        }
    });
    let mut results = Vec::with_capacity(inputs.len());
    for slot in slots.into_inner().expect("slots lock") {
        match slot {
            Some(Ok(value)) => results.push(Ok(value)),
            Some(Err(JevFailure::Rejected(message))) => results.push(Err(message)),
            Some(Err(JevFailure::Error(message))) => return Err(message),
            None => {}
        }
    }
    Ok(results)
}

/// Python's `float(value)` of a decoded JSON value.
fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        Value::String(s) => plexdb_store::py_strip(s).parse().ok(),
        _ => None,
    }
}

/// The one [`Jev`] that reaches the real service.
pub struct LiveJev {
    key: String,
    url: String,
    agent: ureq::Agent,
    backoff: Duration,
}

impl LiveJev {
    pub fn new(api_key: &str) -> Self {
        let url = std::env::var(URL_VAR)
            .ok()
            .map(|raw| raw.trim().to_string())
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| DEFAULT_URL.to_string());
        Self::with_url(api_key, &url, BACKOFF)
    }

    pub fn with_url(api_key: &str, url: &str, backoff: Duration) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .build()
            .into();
        LiveJev {
            key: api_key.to_string(),
            url: url.to_string(),
            agent,
            backoff,
        }
    }

    /// POST with backoff on 429; 400 and 422 are [`JevFailure::Rejected`]. Error messages
    /// carry the status code and the failure's kind only, never the request or the key.
    fn post(&self, body: &Value) -> Result<Value, JevFailure> {
        let url = &self.url;
        for attempt in 0..ATTEMPTS {
            let mut response = self
                .agent
                .post(url)
                .header("Authorization", &format!("Bearer {}", self.key))
                .send_json(body)
                .map_err(|err| {
                    JevFailure::Error(format!("cannot reach Jev at {url}: {}", error_kind(&err)))
                })?;
            let status = response.status().as_u16();
            if status == 429 {
                std::thread::sleep(self.backoff * 2u32.pow(attempt));
                continue;
            }
            if REJECTED_STATUSES.contains(&status) {
                return Err(JevFailure::Rejected(format!(
                    "Jev returned {status} for {url}"
                )));
            }
            if status != 200 {
                return Err(JevFailure::Error(format!(
                    "Jev returned {status} for {url}"
                )));
            }
            let not_json = || {
                JevFailure::Error(format!(
                    "Jev returned a response that is not JSON for {url}"
                ))
            };
            let text = response
                .body_mut()
                .read_to_string()
                .map_err(|_| not_json())?;
            return serde_json::from_str(&text).map_err(|_| not_json());
        }
        Err(JevFailure::Error(format!(
            "Jev answered 429 {ATTEMPTS} times in a row for {url}"
        )))
    }
}

fn model_of(answer: &Value) -> Option<String> {
    answer.get("model").map(crate::pyval::py_str)
}

impl Jev for LiveJev {
    fn judge(&self, tag_a: &str, tag_b: &str) -> Result<Verdict, JevFailure> {
        let body = json!({
            "state": {"tag_a": tag_a, "tag_b": tag_b},
            "model": MODEL,
            "questions": {
                "s": {
                    "type": "score",
                    "instructions": SCORE_INSTRUCTIONS,
                    "criteria": SCORE_CRITERIA,
                },
                "n": {"type": "noul", "instructions": NOUL_INSTRUCTIONS},
            },
        });
        let answer = self.post(&body)?;
        let parsed = answer
            .pointer("/answers/n/noul")
            .and_then(py_float)
            .zip(model_of(&answer));
        let Some((score, model)) = parsed else {
            return Err(JevFailure::Error(
                "Jev returned an answer without a noul score and a model".into(),
            ));
        };
        if !(0.0..=1.0).contains(&score) || model.is_empty() {
            return Err(JevFailure::Error(format!(
                "Jev returned a noul score of {score} from model {}",
                crate::decisions::py_repr(&model)
            )));
        }
        Ok(Verdict { score, model })
    }

    fn judge_roles(&self, tag: &str) -> Result<RoleVerdict, JevFailure> {
        let questions: serde_json::Map<String, Value> = ROLES
            .iter()
            .zip(ROLE_INSTRUCTIONS)
            .map(|(role, instructions)| {
                (
                    role.to_string(),
                    json!({"type": "noul", "instructions": instructions}),
                )
            })
            .collect();
        let body = json!({
            "state": {"tag": tag, "context": ROLE_CONTEXT},
            "model": MODEL,
            "questions": questions,
        });
        let answer = self.post(&body)?;
        let scores: Option<Vec<f64>> = ROLES
            .iter()
            .map(|role| {
                answer
                    .pointer(&format!("/answers/{role}/noul"))
                    .and_then(py_float)
            })
            .collect();
        let Some((scores, model)) = scores.zip(model_of(&answer)) else {
            return Err(JevFailure::Error(
                "Jev returned an answer without a noul score per role and a model".into(),
            ));
        };
        let scores: [f64; 5] = scores.try_into().expect("one score per role");
        if model.is_empty() || scores.iter().any(|s| !(0.0..=1.0).contains(s)) {
            return Err(JevFailure::Error(format!(
                "Jev returned role scores {scores:?} from model {}",
                crate::decisions::py_repr(&model)
            )));
        }
        Ok(RoleVerdict { scores, model })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn results_come_back_in_order_and_a_refusal_stays_in_its_place() {
        let inputs: Vec<u32> = (0..40).collect();
        let results = judge_all(
            |n: &u32| match n {
                7 => Err(JevFailure::Rejected("no".into())),
                n => Ok(*n * 2),
            },
            &inputs,
            8,
        )
        .expect("only a refusal");
        assert_eq!(results[6], Ok(12));
        assert_eq!(results[7], Err("no".to_string()));
        assert_eq!(results[39], Ok(78));
        assert_eq!(results.len(), 40);
    }

    #[test]
    fn any_other_failure_ends_the_call_with_that_failure() {
        let inputs: Vec<u32> = (0..40).collect();
        let outcome = judge_all(
            |n: &u32| match n {
                3 => Err(JevFailure::Error("down".into())),
                n => Ok(*n),
            },
            &inputs,
            1,
        );
        assert_eq!(outcome, Err("down".to_string()));
    }

    #[test]
    fn no_inputs_is_no_results() {
        let results = judge_all(|n: &u32| Ok::<_, JevFailure>(*n), &[], 8).expect("empty");
        assert!(results.is_empty());
    }
}
