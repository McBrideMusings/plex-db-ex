//! `LiveClient` against responses recorded from the real TMDB API
//! (`tests/fixtures/tmdb/`), served by a local socket so the request-building and
//! JSON-reading code is the code a live run uses.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;

use plexdb::tmdb::{LiveClient, TmdbSource};

const KEY: &str = "the-test-key";

fn fixture(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/tmdb")
        .join(name);
    std::fs::read(path).expect("recorded response")
}

/// Request path -> (status, body). Anything not listed is TMDB's recorded 404.
fn serve(routes: HashMap<&'static str, (u16, Vec<u8>)>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let base = format!(
        "http://127.0.0.1:{}/3",
        listener.local_addr().expect("addr").port()
    );
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let mut buffer = [0u8; 4096];
            let n = stream.read(&mut buffer).unwrap_or(0);
            let request = String::from_utf8_lossy(&buffer[..n]);
            let target = request.split_whitespace().nth(1).unwrap_or("").to_string();
            let (path, query) = target.split_once('?').unwrap_or((&target, ""));
            let (status, body) = if query != format!("api_key={KEY}") {
                (401, b"{\"status_message\":\"Invalid API key\"}".to_vec())
            } else {
                routes
                    .get(path)
                    .cloned()
                    .unwrap_or_else(|| (404, fixture("movie_not_found_404.json")))
            };
            let head = format!(
                "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(&body);
        }
    });
    base
}

fn recorded() -> HashMap<&'static str, (u16, Vec<u8>)> {
    [
        ("/3/movie/155/keywords", "movie_155_keywords.json"),
        ("/3/tv/1396/keywords", "tv_1396_keywords.json"),
        (
            "/3/movie/155/recommendations",
            "movie_155_recommendations.json",
        ),
        ("/3/movie/155/similar", "movie_155_similar.json"),
        ("/3/tv/1396/recommendations", "tv_1396_recommendations.json"),
        ("/3/movie/155", "movie_155_details.json"),
        ("/3/tv/1396", "tv_1396_details.json"),
        ("/3/movie/424783/keywords", "movie_zero_keywords.json"),
    ]
    .into_iter()
    .map(|(path, file)| (path, (200, fixture(file))))
    .collect()
}

fn json_names(file: &str, field: &str) -> Vec<String> {
    let body: serde_json::Value = serde_json::from_slice(&fixture(file)).expect("json");
    body[field]
        .as_array()
        .expect("array")
        .iter()
        .map(|e| e["name"].as_str().expect("name").to_string())
        .collect()
}

#[test]
fn keywords_come_from_the_array_a_movie_or_a_show_names_and_an_unknown_id_is_empty() {
    let client = LiveClient::with_base(KEY, &serve(recorded()));

    let movie = client.keywords("155", "movie").expect("movie");
    let show = client.keywords("1396", "tv").expect("show");

    assert_eq!(movie, json_names("movie_155_keywords.json", "keywords"));
    assert_eq!(show, json_names("tv_1396_keywords.json", "results"));
    assert!(!movie.is_empty() && !show.is_empty());
    assert_eq!(client.keywords("1", "movie"), Ok(vec![]));
    assert_eq!(client.keywords("424783", "movie"), Ok(vec![]));
}

#[test]
fn related_ids_keep_tmdbs_order_and_original_language_is_the_code_it_sent() {
    let client = LiveClient::with_base(KEY, &serve(recorded()));

    let recommended = client
        .recommendations("155", "movie")
        .expect("recommendations");
    let body: serde_json::Value =
        serde_json::from_slice(&fixture("movie_155_recommendations.json")).expect("json");
    let want: Vec<String> = body["results"]
        .as_array()
        .expect("results")
        .iter()
        .map(|e| e["id"].to_string())
        .collect();

    assert_eq!(recommended, want);
    assert!(!want.is_empty());
    assert_eq!(
        client.original_language("155", "movie"),
        Ok(Some("en".into()))
    );
    assert_eq!(client.original_language("9", "movie"), Ok(None));
    assert_eq!(client.similar("9", "movie"), Ok(vec![]));
}

#[test]
fn a_server_error_or_a_body_that_is_not_json_is_an_error_that_never_prints_the_key() {
    let mut routes = recorded();
    routes.insert("/3/movie/500/keywords", (500, b"oops".to_vec()));
    routes.insert(
        "/3/movie/200/keywords",
        (200, b"<html>maintenance</html>".to_vec()),
    );
    routes.insert("/3/movie/301/keywords", (302, b"".to_vec()));
    let base = serve(routes);
    let client = LiveClient::with_base(KEY, &base);

    let server_error = client.keywords("500", "movie").expect_err("500");
    let not_json = client.keywords("200", "movie").expect_err("not json");
    let redirect = client.keywords("301", "movie").expect_err("redirect");
    let unreachable = LiveClient::with_base(KEY, "http://127.0.0.1:1/3")
        .keywords("155", "movie")
        .expect_err("nothing listens");
    let bad_key = LiveClient::with_base("wrong", &base)
        .keywords("155", "movie")
        .expect_err("401");

    assert_eq!(
        server_error.0,
        format!("TMDB returned 500 for {base}/movie/500/keywords")
    );
    assert_eq!(
        not_json.0,
        format!("TMDB returned a response that is not JSON for {base}/movie/200/keywords")
    );
    assert!(
        redirect.0.starts_with("TMDB returned 302 for "),
        "{redirect}"
    );
    assert!(bad_key.0.starts_with("TMDB returned 401 for "), "{bad_key}");
    assert!(
        unreachable.0.starts_with("cannot reach TMDB at "),
        "{unreachable}"
    );
    for err in [server_error, not_json, redirect, bad_key, unreachable] {
        assert!(!err.0.contains(KEY) && !err.0.contains("wrong"), "{err}");
    }
}
