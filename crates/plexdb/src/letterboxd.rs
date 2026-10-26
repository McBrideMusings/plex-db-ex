//! Reading a Letterboxd list and the TMDB identity of its films — read-only, keyless and
//! scraped (`plexdb/letterboxd_client.py`).
//!
//! `GET /<user>/list/<slug>/` answers 200 with up to 100 entries a page and `/page/N/` for
//! the rest. The `<body>` carries the class `list-page`, the list's name is the `h1` with
//! class `title-1`, each entry is an element with `data-item-slug` and `data-item-name`
//! (`Title (Year)`), a link with class `next` leads to the next page, and the description
//! meta tag opens "A list of N films". An entry carries no TMDB id: its film page's
//! `<body>` does, with `data-tmdb-type` (`movie` or `tv`) beside it. A page without the
//! list markers is not a list page: a Cloudflare challenge looks like one.
//!
//! Every request path is checked against the `Disallow` rules robots.txt gives
//! `User-agent: *`, fetched once per client, and a disallowed path raises before anything
//! is sent. The client pauses after every request and honours a bounded `Retry-After`.

use std::time::Duration;

use crate::html::{Token, attr_text, tokenize};

pub const DEFAULT_BASE: &str = "https://letterboxd.com";
/// Points the client elsewhere, for a recorded server (`tools/parity.py`).
pub const BASE_VAR: &str = "LETTERBOXD_BASE_URL";
/// Seconds paused after every request.
pub const PAUSE_VAR: &str = "LETTERBOXD_PAUSE_SECONDS";
const USER_AGENT: &str = concat!(
    "plexdb/0.1.0 (https://github.com/McBrideMusings/plex-db-ex; ",
    "metadata store over a personal Plex library) ureq"
);
const TIMEOUT: Duration = Duration::from_secs(30);
const DEFAULT_PAUSE_SECONDS: f64 = 1.0;
const MAX_RETRIES: usize = 3;
const MAX_RETRY_AFTER: f64 = 120.0;
const MAX_LIST_PAGES: usize = 100;

/// `LetterboxdError`: the message is what a `list_attempt` row keeps.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct LbError(pub String);

/// One entry of a list as its page shows it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListFilm {
    pub slug: String,
    pub title: String,
    pub year: Option<i64>,
}

/// A whole list, every page read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListContents {
    pub name: String,
    pub size: Option<i64>,
    pub entries: Vec<ListFilm>,
    pub pages: usize,
}

/// The TMDB identity a film page carries: `media_type` is `movie` or `show`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilmIds {
    pub media_type: String,
    pub tmdb_id: Option<String>,
}

pub trait LbSource {
    fn list_contents(&mut self, user: &str, slug: &str) -> Result<ListContents, LbError>;
    fn film_ids(&mut self, slug: &str) -> Result<FilmIds, LbError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListPage {
    pub name: String,
    pub size: Option<i64>,
    pub entries: Vec<ListFilm>,
    pub next_path: Option<String>,
}

/// A TMDB id: digits only. Nothing else is spliced into a request path.
pub fn is_tmdb_id(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit())
}

/// `^[a-z0-9_.-]+$`
fn is_segment(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'.' | b'-')
        })
}

/// The `Disallow` values of every group naming `User-agent: *`. `Allow` lines are
/// ignored, which can only make the client stricter than the file.
pub fn disallowed_patterns(robots_txt: &str) -> Vec<String> {
    let mut patterns = Vec::new();
    let mut agents: Vec<String> = Vec::new();
    let mut in_rules = false;
    for raw in robots_txt.lines() {
        let line = raw.split('#').next().unwrap_or("").trim();
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        let (name, value) = (name.trim().to_lowercase(), value.trim());
        if name == "user-agent" {
            if in_rules {
                agents.clear();
                in_rules = false;
            }
            agents.push(value.to_string());
        } else if name == "allow" || name == "disallow" {
            in_rules = true;
            if name == "disallow" && !value.is_empty() && agents.iter().any(|a| a == "*") {
                patterns.push(value.to_string());
            }
        }
    }
    patterns
}

fn glob(parts: &[&str], text: &str, anchored: bool) -> bool {
    let Some(rest) = text.strip_prefix(parts[0]) else {
        return false;
    };
    if parts.len() == 1 {
        return !anchored || rest.is_empty();
    }
    (0..=rest.len())
        .filter(|k| rest.is_char_boundary(*k))
        .any(|k| glob(&parts[1..], &rest[k..], anchored))
}

/// Whether `path` matches any robots.txt pattern: `*` matches any run of characters, a
/// trailing `$` anchors the end, and every pattern anchors the start.
pub fn is_disallowed(path: &str, patterns: &[String]) -> bool {
    patterns.iter().any(|pattern| {
        let (body, anchored) = match pattern.strip_suffix('$') {
            Some(body) => (body, true),
            None => (pattern.as_str(), false),
        };
        let parts: Vec<&str> = body.split('*').collect();
        glob(&parts, path, anchored)
    })
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// `\bA list of ([\d,]+) films?\b` searched in a description.
fn list_size(description: &str) -> Option<i64> {
    const HEAD: &str = "A list of ";
    for (at, _) in description.match_indices(HEAD) {
        if description[..at].chars().next_back().is_some_and(is_word) {
            continue;
        }
        let rest = &description[at + HEAD.len()..];
        let digits = rest
            .chars()
            .take_while(|c| c.is_ascii_digit() || *c == ',')
            .count();
        // `[\d,]+` backtracks, but the text after it begins with a space, which it
        // cannot give up a digit for.
        let tail = &rest[digits..];
        let Some(tail) = tail.strip_prefix(" film") else {
            continue;
        };
        let tail = tail.strip_prefix('s').unwrap_or(tail);
        if digits == 0 || tail.chars().next().is_some_and(is_word) {
            continue;
        }
        return rest[..digits].replace(',', "").parse().ok();
    }
    None
}

/// `^(.*) \((\d{4})\)$` over a label: the title and the year, else the label whole.
fn split_item_name(label: &str) -> (String, Option<i64>) {
    let bytes = label.as_bytes();
    if bytes.len() >= 7 {
        let tail = &bytes[bytes.len() - 7..];
        if tail[0] == b' '
            && tail[1] == b'('
            && tail[6] == b')'
            && tail[2..6].iter().all(u8::is_ascii_digit)
        {
            let year = std::str::from_utf8(&tail[2..6])
                .ok()
                .and_then(|y| y.parse().ok());
            return (label[..label.len() - 7].to_string(), year);
        }
    }
    (label.to_string(), None)
}

fn classes(attrs: &[(String, Option<String>)]) -> Vec<&str> {
    attr_text(attrs, "class")
        .map(|c| {
            c.split(plexdb_store::is_py_space)
                .filter(|p| !p.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// One list page → its name, stated size, entries and next-page link, or `None` when the
/// page is not a list page (no `list-page` body class or no `title-1` heading).
pub fn parse_list_page(html: &str) -> Option<ListPage> {
    let mut is_list_page = false;
    let mut name: Option<String> = None;
    let mut entries = Vec::new();
    let mut next_href: Option<String> = None;
    let mut size: Option<i64> = None;
    let mut h1_text: Option<String> = None;
    for token in tokenize(html) {
        match token {
            Token::Start { tag, attrs, .. } => {
                let classes = classes(&attrs);
                match tag.as_str() {
                    "body" => is_list_page = classes.contains(&"list-page"),
                    "h1" if classes.contains(&"title-1") && name.is_none() => {
                        h1_text = Some(String::new());
                    }
                    "a" if classes.contains(&"next") => {
                        if let Some(href) = attr_text(&attrs, "href").filter(|h| !h.is_empty()) {
                            next_href = Some(href.to_string());
                        }
                    }
                    "meta" if attr_text(&attrs, "name") == Some("description") => {
                        if let Some(found) = list_size(attr_text(&attrs, "content").unwrap_or("")) {
                            size = Some(found);
                        }
                    }
                    _ => {}
                }
                let slug = attr_text(&attrs, "data-item-slug").filter(|s| !s.is_empty());
                let label = attr_text(&attrs, "data-item-name").filter(|s| !s.is_empty());
                if let (Some(slug), Some(label)) = (slug, label) {
                    let (title, year) = split_item_name(label);
                    entries.push(ListFilm {
                        slug: slug.to_string(),
                        title,
                        year,
                    });
                }
            }
            Token::End(tag) => {
                if tag == "h1"
                    && let Some(text) = h1_text.take()
                {
                    name = Some(
                        text.split(plexdb_store::is_py_space)
                            .filter(|p| !p.is_empty())
                            .collect::<Vec<_>>()
                            .join(" "),
                    );
                }
            }
            Token::Data(data) => {
                if let Some(text) = &mut h1_text {
                    text.push_str(&data);
                }
            }
        }
    }
    let name = name.filter(|n| !n.is_empty())?;
    is_list_page.then_some(ListPage {
        name,
        size,
        entries,
        next_path: next_href,
    })
}

/// A film page → its TMDB identity, or `None` when the `<body>` is not a film's. A film
/// Letterboxd has not matched to TMDB has no id and counts as a movie.
pub fn parse_film_ids(html: &str) -> Option<FilmIds> {
    let mut is_film = false;
    let mut tmdb_type: Option<String> = None;
    let mut tmdb_id: Option<String> = None;
    for token in tokenize(html) {
        if let Token::Start { tag, attrs, .. } = token
            && tag == "body"
        {
            is_film = attr_text(&attrs, "data-type") == Some("film");
            tmdb_type = attr_text(&attrs, "data-tmdb-type").map(str::to_string);
            tmdb_id = attr_text(&attrs, "data-tmdb-id").map(str::to_string);
        }
    }
    if !is_film {
        return None;
    }
    let tmdb_id = tmdb_id.filter(|id| !id.is_empty());
    if tmdb_id.as_deref().is_some_and(|id| !is_tmdb_id(id)) {
        return None;
    }
    let media_type = if tmdb_type.as_deref() == Some("tv") {
        "show"
    } else {
        "movie"
    };
    Some(FilmIds {
        media_type: media_type.into(),
        tmdb_id,
    })
}

struct Response {
    status: u16,
    location: String,
    text: String,
}

pub struct LiveLbClient {
    agent: ureq::Agent,
    base: String,
    host: String,
    pause: f64,
    disallowed: Option<Vec<String>>,
    /// Every path this client sent a request for, in order.
    pub requested: Vec<String>,
}

fn host_of(base: &str) -> String {
    let rest = base.split_once("://").map_or(base, |(_, rest)| rest);
    rest.split(['/', '?', '#']).next().unwrap_or("").to_string()
}

impl LiveLbClient {
    pub fn new() -> Self {
        let base = std::env::var(BASE_VAR)
            .ok()
            .map(|raw| raw.trim().trim_end_matches('/').to_string())
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| DEFAULT_BASE.to_string());
        let pause = std::env::var(PAUSE_VAR)
            .ok()
            .and_then(|raw| raw.trim().parse::<f64>().ok())
            .filter(|p| p.is_finite() && *p >= 0.0)
            .unwrap_or(DEFAULT_PAUSE_SECONDS);
        Self::with(&base, pause)
    }

    pub fn with(base: &str, pause: f64) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .max_redirects(0)
            .max_redirects_will_error(false)
            .user_agent(USER_AGENT)
            .build()
            .into();
        LiveLbClient {
            agent,
            host: host_of(base),
            base: base.to_string(),
            pause,
            disallowed: None,
            requested: Vec::new(),
        }
    }

    fn sleep(&self, seconds: f64) {
        if seconds > 0.0 {
            std::thread::sleep(Duration::from_secs_f64(seconds));
        }
    }

    fn robots(&mut self) -> Result<Vec<String>, LbError> {
        if self.disallowed.is_none() {
            let response = self.send("/robots.txt")?;
            if response.status != 200 {
                return Err(LbError(format!(
                    "Letterboxd returned {} for robots.txt",
                    response.status
                )));
            }
            let patterns = disallowed_patterns(&response.text);
            println!(
                "letterboxd: robots.txt disallows {} pattern(s) for User-agent: *",
                patterns.len()
            );
            self.disallowed = Some(patterns);
        }
        Ok(self.disallowed.clone().unwrap_or_default())
    }

    fn get(&mut self, path: &str) -> Result<Response, LbError> {
        if is_disallowed(path, &self.robots()?) {
            return Err(LbError(format!(
                "robots.txt disallows {path}; not requesting it"
            )));
        }
        self.send(path)
    }

    fn send(&mut self, path: &str) -> Result<Response, LbError> {
        for attempt in 0..=MAX_RETRIES {
            self.requested.push(path.to_string());
            let url = format!("{}{path}", self.base);
            let mut raw = self.agent.get(&url).call().map_err(|err| {
                LbError(format!(
                    "cannot reach Letterboxd at {url}: {}",
                    match err {
                        ureq::Error::Timeout(_) => "TimeoutException",
                        _ => "ConnectError",
                    }
                ))
            })?;
            let header = |name: &str| {
                raw.headers()
                    .get(name)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("")
                    .to_string()
            };
            let status = raw.status().as_u16();
            let location = header("location");
            let retry_after = header("retry-after");
            let text = raw
                .body_mut()
                .with_config()
                .limit(64 << 20)
                .read_to_string()
                .map_err(|_| LbError(format!("cannot reach Letterboxd at {url}: ReadError")))?;
            if status == 429 && attempt < MAX_RETRIES {
                let wait = retry_after_seconds(&retry_after)?;
                println!("letterboxd: 429 on {path}, waiting {wait:.0} s");
                self.sleep(wait);
                continue;
            }
            self.sleep(self.pause);
            if status == 429 {
                break;
            }
            if status == 403 {
                return Err(LbError(format!(
                    "Letterboxd answered 403 for {path} (a Cloudflare challenge)"
                )));
            }
            return Ok(Response {
                status,
                location,
                text,
            });
        }
        Err(LbError(format!(
            "Letterboxd kept answering 429 after {MAX_RETRIES} retries"
        )))
    }

    /// Where a redirect's `Location` points, as a path on this site, else the header as
    /// sent (`httpx.URL(BASE_URL).join(location)`).
    fn redirect_path(&self, location: &str) -> String {
        let strip = |p: &str| p.split(['?', '#']).next().unwrap_or("").to_string();
        if let Some((_, rest)) = location.split_once("://") {
            let (host, path) = rest.split_once('/').map_or((rest, ""), |(h, p)| (h, p));
            let host = host.split('@').next_back().unwrap_or(host);
            let host = host.split(':').next().unwrap_or(host);
            let own = self.host.split(':').next().unwrap_or(&self.host);
            if host == own {
                return strip(&format!("/{path}"));
            }
            return location.to_string();
        }
        if location.starts_with('/') {
            strip(location)
        } else {
            strip(&format!("/{location}"))
        }
    }
}

impl Default for LiveLbClient {
    fn default() -> Self {
        Self::new()
    }
}

fn retry_after_seconds(raw: &str) -> Result<f64, LbError> {
    let wait = raw
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|w| !w.is_nan())
        .unwrap_or(60.0);
    if wait > MAX_RETRY_AFTER {
        return Err(LbError(format!(
            "Letterboxd asked for a {wait:.0} s wait; giving up on this title"
        )));
    }
    Ok(wait.max(1.0))
}

/// `^/film/[^/?#]+/$`
fn is_film_path(path: &str) -> bool {
    path.strip_prefix("/film/")
        .and_then(|rest| rest.strip_suffix('/'))
        .is_some_and(|slug| !slug.is_empty() && !slug.contains(['/', '?', '#']))
}

impl LbSource for LiveLbClient {
    fn list_contents(&mut self, user: &str, slug: &str) -> Result<ListContents, LbError> {
        if !(is_segment(user) && is_segment(slug)) {
            return Err(LbError(format!(
                "not a Letterboxd list: {} {}",
                crate::decisions::py_repr(user),
                crate::decisions::py_repr(slug)
            )));
        }
        let first = format!("/{user}/list/{slug}/");
        let follows = |candidate: &str| {
            candidate
                .strip_prefix(&format!("{first}page/"))
                .and_then(|rest| rest.strip_suffix('/'))
                .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
        };
        let mut path = Some(first.clone());
        let mut entries = Vec::new();
        let mut name: Option<String> = None;
        let mut size: Option<i64> = None;
        let mut pages = 0;
        while let Some(current) = path {
            if pages >= MAX_LIST_PAGES {
                return Err(LbError(format!(
                    "{first} runs past {MAX_LIST_PAGES} pages; giving up"
                )));
            }
            let response = self.get(&current)?;
            if response.status != 200 {
                return Err(LbError(format!(
                    "Letterboxd returned {} for {current}",
                    response.status
                )));
            }
            let page = parse_list_page(&response.text).ok_or_else(|| {
                LbError(format!(
                    "{current} is not a Letterboxd list page (no list marker; a challenge page \
                     or a changed layout)"
                ))
            })?;
            pages += 1;
            println!(
                "letterboxd: {current} -> HTTP 200, {} entries on page {pages}",
                page.entries.len()
            );
            if name.as_deref().is_none_or(str::is_empty) {
                name = Some(page.name.clone());
            }
            size = size.or(page.size);
            entries.extend(page.entries);
            if let Some(next) = &page.next_path
                && !follows(next)
            {
                return Err(LbError(format!(
                    "{current} links to {}, not the next page of the list",
                    crate::decisions::py_repr(next)
                )));
            }
            path = page.next_path;
        }
        Ok(ListContents {
            name: name.unwrap_or_default(),
            size,
            entries,
            pages,
        })
    }

    fn film_ids(&mut self, slug: &str) -> Result<FilmIds, LbError> {
        if !is_segment(slug) {
            return Err(LbError(format!(
                "not a film slug: {}",
                crate::decisions::py_repr(slug)
            )));
        }
        let mut path = format!("/film/{slug}/");
        let mut response = self.get(&path)?;
        if matches!(response.status, 301 | 302 | 303 | 307 | 308) {
            let moved = self.redirect_path(&response.location);
            if !is_film_path(&moved) {
                return Err(LbError(format!(
                    "Letterboxd redirected {path} to {}",
                    crate::decisions::py_repr(&response.location)
                )));
            }
            path = moved;
            response = self.get(&path)?;
        }
        if response.status != 200 {
            return Err(LbError(format!(
                "Letterboxd returned {} for {path}",
                response.status
            )));
        }
        parse_film_ids(&response.text).ok_or_else(|| {
            LbError(format!(
                "{path} has no film marker (a challenge page or a changed layout)"
            ))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIST: &str = r#"<html><head>
        <meta name="description" content="A list of 1,234 films compiled by lina">
        </head><body class="x list-page y">
        <h1 class="title-1 prettify">  Top &amp;
           Best </h1>
        <ul><li data-item-slug="heat" data-item-name="Heat (1995)"></li>
        <li data-item-slug="m" data-item-name="M"></li></ul>
        <a class="next" href="/lina/list/top/page/2/">Older</a></body></html>"#;

    #[test]
    fn a_list_page_gives_its_name_size_entries_and_next_link() {
        let page = parse_list_page(LIST).expect("a list page");
        assert_eq!(page.name, "Top & Best");
        assert_eq!(page.size, Some(1234));
        assert_eq!(page.next_path.as_deref(), Some("/lina/list/top/page/2/"));
        assert_eq!(
            page.entries,
            [
                ListFilm {
                    slug: "heat".into(),
                    title: "Heat".into(),
                    year: Some(1995)
                },
                ListFilm {
                    slug: "m".into(),
                    title: "M".into(),
                    year: None
                },
            ]
        );
    }

    #[test]
    fn a_page_without_the_markers_is_not_a_list_page() {
        assert!(parse_list_page("<body><h1 class=\"title-1\">x</h1>").is_none());
        assert!(parse_list_page("<body class=\"list-page\"><h1>x</h1>").is_none());
    }

    #[test]
    fn a_film_page_gives_its_tmdb_identity_scoped_by_type() {
        let show = r#"<body data-type="film" data-tmdb-id="1396" data-tmdb-type="tv">"#;
        assert_eq!(
            parse_film_ids(show),
            Some(FilmIds {
                media_type: "show".into(),
                tmdb_id: Some("1396".into())
            })
        );
        let bare = r#"<body data-type="film">"#;
        assert_eq!(
            parse_film_ids(bare),
            Some(FilmIds {
                media_type: "movie".into(),
                tmdb_id: None
            })
        );
        assert_eq!(parse_film_ids(r#"<body data-type="list">"#), None);
        assert_eq!(
            parse_film_ids(r#"<body data-type="film" data-tmdb-id="12a">"#),
            None
        );
    }

    #[test]
    fn robots_rules_for_every_agent_apply_with_wildcards_and_anchors() {
        let rules = disallowed_patterns(
            "User-agent: Googlebot\nDisallow: /g/\n\nUser-agent: *\nUser-agent: other\n\
             Disallow: /ajax/\nDisallow: /*/by/best-match/$ # c\nAllow: /x\nDisallow:\n",
        );
        assert_eq!(rules, ["/ajax/", "/*/by/best-match/$"]);
        assert!(is_disallowed("/ajax/x", &rules));
        assert!(is_disallowed("/films/theme/a/by/best-match/", &rules));
        assert!(!is_disallowed("/films/theme/a/by/best-match/2/", &rules));
        assert!(!is_disallowed("/film/heat/", &rules));
    }

    #[test]
    fn a_description_states_a_size_only_in_its_own_words() {
        assert_eq!(list_size("A list of 1 film by x"), Some(1));
        assert_eq!(list_size("Not A list of 3 films"), Some(3));
        assert_eq!(list_size("xA list of 3 films"), None);
        assert_eq!(list_size("A list of 3 filmsy"), None);
    }

    #[test]
    fn only_a_film_path_is_followed_from_a_redirect() {
        let client = LiveLbClient::with("http://127.0.0.1:9", 0.0);
        assert_eq!(client.redirect_path("/film/heat/?x=1"), "/film/heat/");
        assert_eq!(
            client.redirect_path("http://127.0.0.1:9/film/heat/"),
            "/film/heat/"
        );
        assert_eq!(
            client.redirect_path("https://elsewhere.test/film/heat/"),
            "https://elsewhere.test/film/heat/"
        );
        assert!(is_film_path("/film/heat/"));
        assert!(!is_film_path("/film/heat/x/"));
        assert!(!is_film_path("/film//"));
    }

    #[test]
    fn a_retry_after_waits_at_least_a_second_and_never_over_the_cap() {
        assert_eq!(retry_after_seconds("0").ok(), Some(1.0));
        assert_eq!(retry_after_seconds("junk").ok(), Some(60.0));
        assert_eq!(retry_after_seconds("30").ok(), Some(30.0));
        assert!(retry_after_seconds("121").is_err());
    }
}
