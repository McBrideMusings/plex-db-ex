//! One GET, for the Plex and Tautulli reads and the explorer's poster proxy.

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

/// A GET agent that bounds the connect and the wait for the answer at `wait` each and the
/// body at `body`, rather than the whole request at once: a poster of a few MB may take
/// longer to arrive than a server takes to start answering.
pub fn phased_agent(wait: Duration, body: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_connect(Some(wait))
        .timeout_send_request(Some(wait))
        .timeout_recv_response(Some(wait))
        .timeout_recv_body(Some(body))
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
    let mut response = match call(agent, url, query, headers) {
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

/// One GET's response, whatever its status, with the same one retry as [`get`]. A timeout is
/// not retried: a server that did not answer in time is not a closed keep-alive connection.
pub fn call(
    agent: &ureq::Agent,
    url: &str,
    query: &[(&str, String)],
    headers: &[(&str, &str)],
) -> Result<ureq::http::Response<ureq::Body>, ureq::Error> {
    match attempt(agent, url, query, headers) {
        Err(err) if !matches!(err, ureq::Error::Timeout(_)) => attempt(agent, url, query, headers),
        done => done,
    }
}

fn attempt(
    agent: &ureq::Agent,
    url: &str,
    query: &[(&str, String)],
    headers: &[(&str, &str)],
) -> Result<ureq::http::Response<ureq::Body>, ureq::Error> {
    let mut request = agent.get(url);
    for (key, value) in query {
        request = request.query(key, value);
    }
    for (name, value) in headers {
        request = request.header(*name, *value);
    }
    request.call()
}
