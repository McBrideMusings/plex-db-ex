//! One JSON GET, for the Plex and Tautulli reads.

use std::time::Duration;

use serde_json::Value;

/// A GET agent with `timeout` for the whole request. A non-2xx answer is a status
/// the caller reads, not an error, so a message can leave a credential out.
pub fn agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .max_redirects(0)
        .http_status_as_error(false)
        .build()
        .into()
}

/// What a GET came back as.
pub enum Reply {
    /// The transport failed (no connection, a timeout, a malformed response).
    Unreachable(String),
    Status(u16),
    /// A 2xx response whose body was not JSON.
    /// Carries why the body could not be read: not JSON, too large, or cut off.
    NotJson(String),
    Json(Value),
}

/// One GET. A transport failure is tried once more on a fresh connection: a server
/// that closes an idle keep-alive connection (Plex does) fails the next request that
/// reuses it, and a GET is safe to repeat.
pub fn get(
    agent: &ureq::Agent,
    url: &str,
    query: &[(&str, String)],
    headers: &[(&str, &str)],
) -> Reply {
    match attempt(agent, url, query, headers) {
        Reply::Unreachable(_) => attempt(agent, url, query, headers),
        reply => reply,
    }
}

fn attempt(
    agent: &ureq::Agent,
    url: &str,
    query: &[(&str, String)],
    headers: &[(&str, &str)],
) -> Reply {
    let mut request = agent.get(url);
    for (key, value) in query {
        request = request.query(key, value);
    }
    for (name, value) in headers {
        request = request.header(*name, *value);
    }
    let mut response = match request.call() {
        Ok(response) => response,
        Err(err) => return Reply::Unreachable(err.to_string()),
    };
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        return Reply::Status(status);
    }
    match response.body_mut().read_json::<Value>() {
        Ok(body) => Reply::Json(body),
        Err(err) => Reply::NotJson(err.to_string()),
    }
}
