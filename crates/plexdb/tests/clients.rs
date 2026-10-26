//! The Plex and Tautulli clients against a local server answering canned JSON: how
//! they page, how they fail, and what they keep out of an error message.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};

use plexdb::plex::{PlexClient, PlexSource};
use plexdb::tautulli::{TautulliClient, TautulliSource};
use serde_json::{Value, json};

/// A server on a free local port. `answer` gets the request target and returns the
/// status and body; every request it saw is kept, with its headers.
struct Server {
    base: String,
    seen: Arc<Mutex<Vec<String>>>,
}

fn serve(answer: impl Fn(&str) -> (u16, String) + Send + 'static) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
    let base = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("addr").port()
    );
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut raw = Vec::new();
            let mut buf = [0u8; 4096];
            while !raw.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = stream.read(&mut buf).unwrap_or(0);
                if n == 0 {
                    break;
                }
                raw.extend_from_slice(&buf[..n]);
            }
            let request = String::from_utf8_lossy(&raw).to_string();
            let target = request.split_whitespace().nth(1).unwrap_or("").to_string();
            log.lock().expect("log").push(request);
            let (status, body) = answer(&target);
            let reply = format!(
                "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    Server { base, seen }
}

/// A server answering every request with a 301 to `target`.
fn serve_redirect(target: &str) -> Server {
    let target = target.to_string();
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
    let base = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("addr").port()
    );
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut buf = [0u8; 4096];
            let _ = stream.read(&mut buf);
            let reply = format!(
                "HTTP/1.1 301 Moved\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            );
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    Server {
        base,
        seen: Arc::new(Mutex::new(Vec::new())),
    }
}

fn container(body: Value) -> (u16, String) {
    (200, json!({"MediaContainer": body}).to_string())
}

fn param(target: &str, name: &str) -> Option<String> {
    let query = target.split_once('?')?.1;
    query
        .split('&')
        .find_map(|pair| pair.strip_prefix(&format!("{name}=")).map(String::from))
}

#[test]
fn a_section_is_paged_until_total_size_is_reached_even_when_the_server_caps_the_page() {
    let server = serve(|target| {
        let start: usize = param(target, "X-Plex-Container-Start")
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let page: Vec<Value> = (start..(start + 2).min(5))
            .map(|n| json!({"ratingKey": n.to_string()}))
            .collect();
        container(json!({"totalSize": 5, "Metadata": page}))
    });
    let plex = PlexClient::new(&server.base, "secret-token").expect("client");

    let records = plex.items("1", 1).expect("items");

    let keys: Vec<&str> = records
        .iter()
        .map(|r| r["ratingKey"].as_str().expect("key"))
        .collect();
    assert_eq!(keys, ["0", "1", "2", "3", "4"]);
    let requests = server.seen.lock().expect("seen");
    assert_eq!(requests.len(), 3);
    assert!(requests[0].contains("includeGuids=1") && requests[0].contains("type=1"));
}

#[test]
fn the_plex_token_rides_in_a_header_and_never_in_the_url_or_an_error() {
    let server = serve(|_| (500, "{}".into()));
    let plex = PlexClient::new(&server.base, "secret-token").expect("client");

    let failure = plex.sections().expect_err("a 500 is an error");

    let requests = server.seen.lock().expect("seen");
    let first_line = requests[0].lines().next().expect("request line");
    assert!(!first_line.contains("secret-token"), "{first_line}");
    assert!(
        requests[0]
            .to_lowercase()
            .contains("x-plex-token: secret-token")
    );
    assert!(failure.to_string().contains("500"), "{failure}");
    assert!(!failure.to_string().contains("secret-token"));
}

#[test]
fn history_pages_by_total_size_and_sends_the_cutoff_as_plexs_own_operator() {
    let server = serve(|target| {
        let start: usize = param(target, "X-Plex-Container-Start")
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let page: Vec<Value> = (start..(start + 200).min(450))
            .map(|n| json!({"historyKey": n.to_string()}))
            .collect();
        container(json!({"totalSize": 450, "Metadata": page}))
    });
    let plex = PlexClient::new(&server.base, "t").expect("client");

    let events = plex.history(Some(1_700_000_000)).expect("history");

    assert_eq!(events.len(), 450);
    let requests = server.seen.lock().expect("seen");
    assert_eq!(requests.len(), 3);
    assert!(
        requests[0].contains("viewedAt%3E=1700000000"),
        "{}",
        requests[0]
    );
}

#[test]
fn a_body_that_is_not_a_json_object_is_a_plex_error_not_a_panic() {
    let server = serve(|_| (200, "<html>proxy error</html>".into()));
    let plex = PlexClient::new(&server.base, "t").expect("client");
    assert!(
        plex.sections()
            .expect_err("html")
            .to_string()
            .contains("not JSON")
    );

    let server = serve(|_| (200, "[1, 2]".into()));
    let plex = PlexClient::new(&server.base, "t").expect("client");
    assert!(
        plex.sections()
            .expect_err("array")
            .to_string()
            .contains("expected an object")
    );
}

fn tautulli_body(data: Value) -> (u16, String) {
    (
        200,
        json!({"response": {"result": "success", "data": data}}).to_string(),
    )
}

#[test]
fn tautulli_history_stops_on_a_short_page_and_pages_on_a_full_one() {
    let server = serve(|target| {
        let start: usize = param(target, "start")
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let rows: Vec<Value> = (start..(start + 1000).min(1500))
            .map(|n| json!({"id": n}))
            .collect();
        tautulli_body(json!({"data": rows}))
    });
    let tautulli = TautulliClient::new(&server.base, "api-key").expect("client");

    let rows = tautulli.history().expect("history");

    assert_eq!(rows.len(), 1500);
    assert_eq!(
        server.seen.lock().expect("seen").len(),
        2,
        "the short second page ends it"
    );
}

#[test]
fn tautulli_users_is_the_bare_list_and_a_failure_never_names_the_api_key() {
    let server = serve(|target| {
        if target.contains("cmd=get_users") {
            tautulli_body(json!([{"user_id": 1, "username": "owner"}]))
        } else {
            (
                200,
                json!({"response": {"result": "error", "message": "bad"}}).to_string(),
            )
        }
    });
    let tautulli = TautulliClient::new(&server.base, "api-key-123").expect("client");

    assert_eq!(tautulli.users().expect("users")[0]["username"], "owner");

    let down = serve(|_| (500, String::new()));
    let broken = TautulliClient::new(&down.base, "api-key-123").expect("client");
    for failure in [
        broken.history().expect_err("500"),
        tautulli.history().expect_err("failure"),
    ] {
        assert!(!failure.to_string().contains("api-key-123"), "{failure}");
    }
    assert!(broken.users().expect_err("500").to_string().contains("500"));
}

#[test]
fn a_redirect_is_an_error_and_is_never_followed_with_the_token() {
    let elsewhere = serve(|_| (200, json!({"MediaContainer": {}}).to_string()));
    let target = format!("{}/library/sections", elsewhere.base);
    let moved = serve_redirect(&target);
    let plex = PlexClient::new(&moved.base, "secret-token").expect("client");

    let failure = plex.sections().expect_err("a 301 is an error");

    assert!(failure.to_string().contains("301"), "{failure}");
    assert!(
        elsewhere.seen.lock().expect("seen").is_empty(),
        "the redirect target must see no request, and so no token"
    );
}
