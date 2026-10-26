//! The two list clients against a server on a free local port: what they send, how they
//! page, and how each status becomes the error a harvest acts on. The recorded answers
//! are `tests/fixtures/mdblist/` and `tests/fixtures/letterboxd/`, the pages the live
//! services returned.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};

use plexdb::letterboxd::{FilmIds, LbSource, LiveLbClient, parse_list_page};
use plexdb::mdblist::{Kind, LiveMdbClient, MdbSource};

/// The requests a server saw, as `(path_and_query, user_agent)`.
type Seen = Arc<Mutex<Vec<(String, String)>>>;

struct Reply {
    status: u16,
    headers: Vec<(&'static str, String)>,
    body: String,
}

fn reply(status: u16, body: impl Into<String>) -> Reply {
    Reply {
        status,
        headers: Vec::new(),
        body: body.into(),
    }
}

/// Serves `answer(path_and_query, user_agent)` to every request; returns the base URL and
/// the requests seen, as `(path_and_query, user_agent)`.
fn serve(answer: impl Fn(&str) -> Reply + Send + 'static) -> (String, Seen) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let base = format!("http://{}", listener.local_addr().expect("addr"));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = Arc::clone(&seen);
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let mut reader = BufReader::new(stream.try_clone().expect("clone"));
            let mut request = String::new();
            reader.read_line(&mut request).expect("request line");
            let target = request.split_whitespace().nth(1).unwrap_or("/").to_string();
            let mut agent = String::new();
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).expect("header");
                if line.trim().is_empty() {
                    break;
                }
                if let Some(value) = line.to_lowercase().strip_prefix("user-agent:") {
                    agent = value.trim().to_string();
                }
            }
            log.lock().expect("log").push((target.clone(), agent));
            let reply = answer(&target);
            let mut head = format!(
                "HTTP/1.1 {} X\r\nContent-Length: {}\r\nConnection: close\r\n",
                reply.status,
                reply.body.len()
            );
            for (name, value) in &reply.headers {
                head.push_str(&format!("{name}: {value}\r\n"));
            }
            head.push_str("\r\n");
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(reply.body.as_bytes());
        }
    });
    (base, seen)
}

fn fixture(name: &str) -> String {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("run the tests through cargo");
    std::fs::read_to_string(format!("{manifest}/../../tests/fixtures/{name}")).expect("fixture")
}

#[test]
fn mdblist_pages_until_has_more_clears_and_sends_its_agent_and_key() {
    let (base, seen) = serve(|target| {
        if target.contains("offset=1000") {
            reply(200, fixture("mdblist/list_14_items_page2.json"))
        } else {
            reply(200, fixture("mdblist/list_14_items_page1.json"))
        }
    });
    let mut client = LiveMdbClient::with_base("sekrit", &base);
    let contents = client.list_entries(14).expect("a list");
    let seen = seen.lock().expect("log");
    assert_eq!(seen.len(), 2);
    assert!(
        seen[0]
            .0
            .starts_with("/lists/14/items?apikey=sekrit&limit=1000&offset=0"),
        "{}",
        seen[0].0
    );
    assert!(seen[1].0.contains("offset=1000"));
    assert!(seen.iter().all(|(_, agent)| agent == "plexdb"));
    assert_eq!(contents.status, 200);
    // The first entry is the Dark Knight, whose own rank (7) is not its position.
    assert_eq!(
        contents.entries[0].external_ids[0],
        ("imdb".into(), "tt0468569".into())
    );
    assert_eq!(contents.entries[0].media_type, "movie");
}

#[test]
fn mdblist_reads_the_top_lists_and_names_each_failure_without_the_key() {
    let (base, _) = serve(|target| match target.split('?').next().unwrap_or("") {
        "/lists/top" => reply(200, fixture("mdblist/lists_top.json")),
        "/lists/gone/x" => reply(404, "{}"),
        "/lists/denied/x" => reply(403, "error code: 1010"),
        "/lists/quota/x" => Reply {
            status: 429,
            headers: vec![
                ("x-ratelimit-remaining", "0".into()),
                ("x-ratelimit-reset", "1700000000".into()),
            ],
            body: String::new(),
        },
        "/lists/junk/x" => reply(200, "not json"),
        _ => reply(500, "{}"),
    });
    let mut client = LiveMdbClient::with_base("sekrit", &base);
    let top = client.top_lists().expect("the feed");
    assert_eq!(top[0].list_id, 2194);
    assert_eq!(top[0].user_name, "garycrawfordgc");
    assert_eq!((top[0].size, top[0].likes), (Some(300), Some(4509)));

    let gone = client.find_list("gone", "x").expect_err("404");
    assert_eq!(gone.status, Some(404));
    assert_eq!(
        gone.message,
        format!("MDBList returned 404 for {base}/lists/gone/x")
    );
    assert!(!gone.is_systemic());
    let denied = client.find_list("denied", "x").expect_err("403");
    assert!(denied.is_systemic() && denied.message.contains("error code: 1010"));
    let quota = client.find_list("quota", "x").expect_err("429");
    assert_eq!(quota.kind, Kind::Quota);
    assert!(
        quota
            .message
            .contains("x-ratelimit-remaining 0, resets at epoch 1700000000")
    );
    let junk = client.find_list("junk", "x").expect_err("not json");
    assert!(
        junk.message
            .starts_with("MDBList returned a response that is not JSON")
    );
    for err in [gone, denied, quota, junk] {
        assert!(!err.message.contains("sekrit"), "{}", err.message);
    }
}

#[test]
fn mdblist_that_never_answers_stops_the_run() {
    let mut client = LiveMdbClient::with_base("k", "http://127.0.0.1:9");
    let err = client.top_lists().expect_err("nothing listens");
    assert_eq!(err.kind, Kind::Unreachable);
    assert!(err.is_systemic());
}

const ROBOTS: &str = "User-agent: *\nDisallow: /ajax/\n";

fn letterboxd(extra: impl Fn(&str) -> Option<Reply> + Send + 'static) -> (LiveLbClient, Seen) {
    let (base, seen) = serve(move |target| {
        if target == "/robots.txt" {
            return reply(200, ROBOTS);
        }
        extra(target).unwrap_or_else(|| reply(404, "no"))
    });
    (LiveLbClient::with(&base, 0.0), seen)
}

#[test]
fn a_letterboxd_list_is_read_page_by_page_and_robots_is_asked_once() {
    let first = "/adaydreaming/list/feeling-lost-in-your-20s/";
    let (mut client, seen) = letterboxd(move |target| match target {
        t if t == first => Some(reply(200, fixture("letterboxd/list_page_1.html"))),
        t if t == format!("{first}page/2/") => {
            Some(reply(200, fixture("letterboxd/list_page_2.html")))
        }
        _ => None,
    });
    let contents = client
        .list_contents("adaydreaming", "feeling-lost-in-your-20s")
        .expect("a list");
    assert_eq!(
        (contents.pages, contents.entries.len(), contents.size),
        (2, 5, Some(5))
    );
    assert_eq!(contents.name, "feeling lost in your 20's");
    let films: Vec<(&str, &str, Option<i64>)> = contents
        .entries
        .iter()
        .map(|f| (f.slug.as_str(), f.title.as_str(), f.year))
        .collect();
    assert_eq!(
        films,
        [
            (
                "scott-pilgrim-vs-the-world",
                "Scott Pilgrim vs. the World",
                Some(2010)
            ),
            ("millennium-mambo", "Millennium Mambo", Some(2001)),
            ("the-devil-probably", "The Devil, Probably", Some(1977)),
            (
                "me-and-earl-and-the-dying-girl",
                "Me and Earl and the Dying Girl",
                Some(2015)
            ),
            ("juno", "Juno", Some(2007)),
        ]
    );
    let seen = seen.lock().expect("log");
    assert_eq!(seen.iter().filter(|(t, _)| t == "/robots.txt").count(), 1);
    assert_eq!(seen.len(), 3);
}

#[test]
fn a_challenge_a_missing_marker_and_a_robots_rule_each_fail_the_list_before_any_data() {
    let (mut client, seen) = letterboxd(|target| match target {
        "/ann/list/cf/" => Some(reply(403, "Just a moment")),
        "/ann/list/plain/" => Some(reply(200, "<html><body><h1>Hello</h1></body></html>")),
        _ => None,
    });
    let challenge = client.list_contents("ann", "cf").expect_err("403");
    assert_eq!(
        challenge.0,
        "Letterboxd answered 403 for /ann/list/cf/ (a Cloudflare challenge)"
    );
    let plain = client.list_contents("ann", "plain").expect_err("no marker");
    assert!(plain.0.contains("is not a Letterboxd list page"));
    let bad = client
        .list_contents("Ann", "best")
        .expect_err("a bad segment");
    assert_eq!(bad.0, "not a Letterboxd list: 'Ann' 'best'");
    let before = seen.lock().expect("log").len();
    let blocked = client
        .film_ids("x")
        .map(|_| ())
        .expect_err("404 is an error too");
    assert!(blocked.0.contains("404"));
    assert!(seen.lock().expect("log").len() > before);
}

#[test]
fn a_film_page_gives_its_tmdb_identity_and_a_redirect_is_followed_only_to_a_film() {
    let (mut client, _) = letterboxd(|target| match target {
        "/film/inception/" => Some(reply(200, fixture("letterboxd/film_inception.html"))),
        "/film/old-slug/" => Some(Reply {
            status: 301,
            headers: vec![("Location", "/film/inception/".into())],
            body: String::new(),
        }),
        "/film/elsewhere/" => Some(Reply {
            status: 302,
            headers: vec![("Location", "https://evil.test/film/inception/".into())],
            body: String::new(),
        }),
        _ => None,
    });
    let ids = FilmIds {
        media_type: "movie".into(),
        tmdb_id: Some("27205".into()),
    };
    assert_eq!(client.film_ids("inception").expect("a film"), ids);
    assert_eq!(client.film_ids("old-slug").expect("followed"), ids);
    let err = client.film_ids("elsewhere").expect_err("refused");
    assert!(
        err.0
            .starts_with("Letterboxd redirected /film/elsewhere/ to 'https://evil.test")
    );
}

#[test]
fn a_path_robots_disallows_is_never_requested() {
    let (base, seen) = serve(|target| {
        if target == "/robots.txt" {
            reply(200, "User-agent: *\nDisallow: /ann/\n")
        } else {
            reply(200, "ok")
        }
    });
    let mut client = LiveLbClient::with(&base, 0.0);
    let err = client.list_contents("ann", "best").expect_err("disallowed");
    assert_eq!(
        err.0,
        "robots.txt disallows /ann/list/best/; not requesting it"
    );
    assert_eq!(seen.lock().expect("log").len(), 1);
}

#[test]
fn a_recorded_list_page_is_a_list_page_only_with_both_markers() {
    let html = fixture("letterboxd/list_page_1.html");
    let page = parse_list_page(&html).expect("a list page");
    assert_eq!(
        page.next_path.as_deref(),
        Some("/adaydreaming/list/feeling-lost-in-your-20s/page/2/")
    );
    assert_eq!(page.entries.len(), 3);
    assert!(parse_list_page(&html.replace("list-page", "film-page")).is_none());
    assert!(parse_list_page(&html.replace("title-1", "title-9")).is_none());
}
