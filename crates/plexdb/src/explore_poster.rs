//! `/api/poster`: one title's Plex thumbnail, proxied so `PLEX_TOKEN` never reaches the
//! browser (`plexdb/explore.py`'s `PosterProxy`). Plex DB-EX has no login, so the token stays
//! server-side, and a count-capped cache and a fixed number of fetch slots bound how much of
//! Plex an anonymous caller can spend.
//!
//! Every store read and Plex call here blocks, so the server calls into this module from
//! tokio's blocking pool only.

use std::collections::{BTreeMap, HashMap};
use std::io::Read as _;
use std::sync::atomic::AtomicUsize;
use std::sync::{Mutex, PoisonError};
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::http::{HeaderValue, StatusCode};
use rusqlite::types::Value;

use crate::decisions::py_repr;
use crate::explore::Slot;
use crate::explore_views::ExploreError;
use crate::http;

/// A poster from Plex's `/library/metadata/<rating_key>/thumb` is a few hundred KB; a larger
/// answer is refused rather than buffered.
pub const MAX_BYTES: usize = 4 << 20;

/// Concurrent fetches from Plex. A cached poster answers without one; a Query tab page load
/// asks for a couple dozen uncached posters at once.
pub const SLOTS: usize = 16;

/// Posters held in memory, keyed by rating key. A rating key's image does not change, so the
/// cap is a count rather than an age.
pub const CACHE_ENTRIES: usize = 2000;

/// httpx's `Timeout(10.0)`: to connect, and to the first byte of the answer.
const TIMEOUT: Duration = Duration::from_secs(10);
/// The whole body: httpx bounds each read at 10 s, ureq bounds the body as a whole.
const BODY_TIMEOUT: Duration = Duration::from_secs(60);

const DEFAULT_TYPE: &str = "image/jpeg";

/// A poster as it goes out: the bytes and Plex's `Content-Type`.
#[derive(Clone)]
pub struct Poster {
    pub data: Bytes,
    pub content_type: HeaderValue,
}

/// Least recently used posters, at most `cap` of them.
struct Cache {
    cap: usize,
    tick: u64,
    entries: HashMap<String, (u64, Poster)>,
    order: BTreeMap<u64, String>,
}

impl Cache {
    fn new(cap: usize) -> Self {
        Cache {
            cap,
            tick: 0,
            entries: HashMap::new(),
            order: BTreeMap::new(),
        }
    }

    fn get(&mut self, key: &str) -> Option<Poster> {
        self.tick += 1;
        let tick = self.tick;
        let (used, poster) = self.entries.get_mut(key)?;
        self.order.remove(used);
        *used = tick;
        self.order.insert(tick, key.to_string());
        Some(poster.clone())
    }

    fn put(&mut self, key: &str, poster: Poster) {
        self.tick += 1;
        if let Some((used, _)) = self.entries.remove(key) {
            self.order.remove(&used);
        }
        self.entries.insert(key.to_string(), (self.tick, poster));
        self.order.insert(self.tick, key.to_string());
        while self.entries.len() > self.cap {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.entries.remove(&oldest);
        }
    }
}

pub struct Posters {
    base: String,
    token: String,
    agent: ureq::Agent,
    cache: Mutex<Cache>,
    in_flight: AtomicUsize,
}

fn unavailable(message: impl Into<String>) -> ExploreError {
    ExploreError::Unavailable(message.into())
}

impl Posters {
    /// `plex_url` and `plex_token` as `FrameConfig` reads them, already stripped.
    pub fn new(plex_url: &str, plex_token: &str) -> Self {
        let agent = http::phased_agent(TIMEOUT, BODY_TIMEOUT);
        Posters {
            base: plex_url.trim_end_matches('/').to_string(),
            token: plex_token.to_string(),
            agent,
            cache: Mutex::new(Cache::new(CACHE_ENTRIES)),
            in_flight: AtomicUsize::new(0),
        }
    }

    /// The poster of `item_id`, from the cache or from Plex (`PosterProxy.get`).
    pub fn get(
        &self,
        open: impl FnOnce() -> Result<rusqlite::Connection, ExploreError>,
        item_id: &str,
    ) -> Result<Poster, ExploreError> {
        if self.base.is_empty() || self.token.is_empty() {
            return Err(unavailable("PLEX_URL/PLEX_TOKEN are not configured"));
        }
        let rating_key = {
            let conn = open()?;
            let found: Option<Value> = conn
                .query_row(
                    "SELECT rating_key FROM plex_items WHERE item_id = ? \
                     ORDER BY last_seen DESC LIMIT 1",
                    [item_id],
                    |row| row.get(0),
                )
                .map(Some)
                .or_else(|err| match err {
                    rusqlite::Error::QueryReturnedNoRows => Ok(None),
                    err => Err(err),
                })?;
            match found {
                Some(Value::Text(text)) => text,
                Some(Value::Integer(n)) => n.to_string(),
                // Python formats a NULL key into the URL as `None`.
                Some(Value::Null) => "None".to_string(),
                _ => {
                    return Err(ExploreError::NotFound(format!(
                        "no Plex rating key on file for {}",
                        py_repr(item_id)
                    )));
                }
            }
        };
        if let Some(hit) = self.lock().get(&rating_key) {
            return Ok(hit);
        }
        let Some(_slot) = Slot::take(&self.in_flight, SLOTS) else {
            return Err(unavailable(
                "too many poster fetches in flight; try again in a moment",
            ));
        };
        let began = Instant::now();
        let url = format!("{}/library/metadata/{rating_key}/thumb", self.base);
        let fetched = self.fetch(&url);
        match &fetched {
            Ok(poster) => eprintln!(
                "explore: poster rating_key={rating_key} bytes={} ms={}",
                poster.data.len(),
                began.elapsed().as_millis()
            ),
            Err(ExploreError::Unavailable(why)) => eprintln!(
                "explore: poster rating_key={rating_key} failed ms={}: {why}",
                began.elapsed().as_millis()
            ),
            Err(_) => {}
        }
        let poster = fetched?;
        self.lock().put(&rating_key, poster.clone());
        Ok(poster)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Cache> {
        self.cache.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// One GET of the thumbnail, with `http::call`'s one retry for a stale keep-alive.
    fn fetch(&self, url: &str) -> Result<Poster, ExploreError> {
        let mut response = http::call(&self.agent, url, &[], &[("X-Plex-Token", &self.token)])
            .map_err(|err| unavailable(unreachable(&err)))?;
        let status = response.status();
        if !status.is_success() {
            let location = response
                .headers()
                .get("location")
                .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
            return Err(unavailable(format!(
                "could not reach Plex: {}",
                status_message(status, url, location.as_deref())
            )));
        }
        let content_type = response
            .headers()
            .get("content-type")
            .cloned()
            .unwrap_or_else(|| HeaderValue::from_static(DEFAULT_TYPE));
        let mut data = Vec::new();
        response
            .body_mut()
            .as_reader()
            .take(MAX_BYTES as u64 + 1)
            .read_to_end(&mut data)
            .map_err(|err| unavailable(format!("could not reach Plex: {}", io_message(&err))))?;
        if data.len() > MAX_BYTES {
            return Err(unavailable(format!("poster is over {MAX_BYTES} bytes")));
        }
        Ok(Poster {
            data: Bytes::from(data),
            content_type,
        })
    }
}

/// httpx's `raise_for_status` message for a non-2xx answer.
fn status_message(status: StatusCode, url: &str, location: Option<&str>) -> String {
    let code = status.as_u16();
    let kind = match code / 100 {
        1 => "Informational response",
        3 => "Redirect response",
        4 => "Client error",
        5 => "Server error",
        _ => "Invalid status code",
    };
    let reason = status.canonical_reason().unwrap_or("");
    let redirect = match location {
        Some(location) if matches!(code, 301 | 302 | 303 | 307 | 308) => {
            format!("Redirect location: '{location}'\n")
        }
        _ => String::new(),
    };
    format!(
        "{kind} '{code} {reason}' for url '{url}'\n{redirect}\
         For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{code}"
    )
}

/// What httpx says for a transport failure: an OS error as Python prints one, a timeout as
/// "timed out".
fn unreachable(err: &ureq::Error) -> String {
    let message = match err {
        ureq::Error::Io(io) => io_message(io),
        ureq::Error::Timeout(_) => "timed out".to_string(),
        other => other.to_string(),
    };
    format!("could not reach Plex: {message}")
}

/// `str(OSError)`: `[Errno 61] Connection refused`.
fn io_message(err: &std::io::Error) -> String {
    if err.kind() == std::io::ErrorKind::TimedOut {
        return "timed out".to_string();
    }
    match err.raw_os_error() {
        Some(code) => {
            let text = std::io::Error::from_raw_os_error(code).to_string();
            let strerror = text
                .strip_suffix(&format!(" (os error {code})"))
                .unwrap_or(&text);
            format!("[Errno {code}] {strerror}")
        }
        None => err.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::Ordering;

    /// A store file holding one `plex_items` row: imdb:tt1 at rating key 77.
    fn seeded() -> std::path::PathBuf {
        static NEXT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let n = NEXT.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("plexdb-poster-{}-{n}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("plexdb.db");
        let _ = std::fs::remove_file(&path);
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE plex_items (rating_key TEXT PRIMARY KEY, item_id TEXT, last_seen TEXT);
             INSERT INTO plex_items VALUES ('77', 'imdb:tt1', '2026-01-01');",
        )
        .unwrap();
        path
    }

    fn poster(byte: u8) -> Poster {
        Poster {
            data: Bytes::from(vec![byte]),
            content_type: HeaderValue::from_static(DEFAULT_TYPE),
        }
    }

    #[test]
    fn the_cache_drops_the_least_recently_used() {
        let mut cache = Cache::new(2);
        cache.put("a", poster(1));
        cache.put("b", poster(2));
        assert!(cache.get("a").is_some());
        cache.put("c", poster(3));
        assert!(cache.get("b").is_none());
        assert_eq!(cache.get("a").unwrap().data[0], 1);
        assert_eq!(cache.get("c").unwrap().data[0], 3);
        assert_eq!(cache.entries.len(), 2);
        assert_eq!(cache.order.len(), 2);
    }

    #[test]
    fn a_seventeenth_fetch_is_refused() {
        let posters = Posters::new("http://127.0.0.1:9", "token");
        let slots: Vec<_> = (0..SLOTS)
            .map(|_| Slot::take(&posters.in_flight, SLOTS).unwrap())
            .collect();
        let refused = posters.get(
            || plexdb_store::open_readonly(&seeded()).map_err(Into::into),
            "imdb:tt1",
        );
        assert!(
            matches!(refused, Err(ExploreError::Unavailable(m)) if m.starts_with("too many poster fetches"))
        );
        drop(slots);
        assert_eq!(posters.in_flight.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn status_messages_read_as_httpx_writes_them() {
        assert_eq!(
            status_message(StatusCode::NOT_FOUND, "http://p/x", None),
            "Client error '404 Not Found' for url 'http://p/x'\n\
             For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
        );
        assert_eq!(
            status_message(StatusCode::FOUND, "http://p/x", Some("/y")),
            "Redirect response '302 Found' for url 'http://p/x'\nRedirect location: '/y'\n\
             For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/302"
        );
    }

    #[test]
    fn an_os_error_reads_as_python_prints_it() {
        let refused = std::io::Error::from_raw_os_error(libc::ECONNREFUSED);
        assert_eq!(
            io_message(&refused),
            format!("[Errno {}] Connection refused", libc::ECONNREFUSED)
        );
    }

    /// A one-shot HTTP server on a local port that answers `reply` to the first request and
    /// hands back the request's head.
    fn plex_once(reply: Vec<u8>) -> (String, std::thread::JoinHandle<String>) {
        use std::io::{BufRead as _, Write as _};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let served = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = std::io::BufReader::new(stream);
            let mut head = String::new();
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" || line.is_empty() {
                    break;
                }
                head.push_str(&line);
            }
            reader.get_mut().write_all(&reply).unwrap();
            head
        });
        (url, served)
    }

    fn answer(status: &str, content_type: Option<&str>, body: &[u8]) -> Vec<u8> {
        let mut out = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\n", body.len());
        if let Some(kind) = content_type {
            out.push_str(&format!("Content-Type: {kind}\r\n"));
        }
        out.push_str("Connection: close\r\n\r\n");
        let mut out = out.into_bytes();
        out.extend_from_slice(body);
        out
    }

    fn store() -> Result<rusqlite::Connection, ExploreError> {
        Ok(plexdb_store::open_readonly(&seeded())?)
    }

    #[test]
    fn a_poster_comes_back_with_the_token_sent_and_a_default_type() {
        let (url, served) = plex_once(answer("200 OK", None, b"jpeg"));
        let posters = Posters::new(&url, "sekrit");
        let poster = posters.get(store, "imdb:tt1").unwrap();
        assert_eq!(&poster.data[..], b"jpeg");
        assert_eq!(poster.content_type, "image/jpeg");
        let head = served.join().unwrap().to_ascii_lowercase();
        assert!(
            head.starts_with("get /library/metadata/77/thumb "),
            "{head}"
        );
        assert!(head.contains("x-plex-token: sekrit"), "{head}");
        // The second ask is a cache hit: the one-shot server is gone.
        assert_eq!(&posters.get(store, "imdb:tt1").unwrap().data[..], b"jpeg");
    }

    #[test]
    fn a_poster_over_the_cap_is_refused() {
        let body = vec![0u8; MAX_BYTES + 1];
        let (url, served) = plex_once(answer("200 OK", Some("image/png"), &body));
        let err = Posters::new(&url, "t")
            .get(store, "imdb:tt1")
            .err()
            .unwrap();
        assert!(matches!(err, ExploreError::Unavailable(m) if m == "poster is over 4194304 bytes"));
        let _ = served.join();
    }

    #[test]
    fn a_plex_404_reads_as_httpx_reports_it() {
        let (url, served) = plex_once(answer("404 Not Found", Some("text/plain"), b"nope"));
        let base = url.trim_end_matches('/').to_string();
        let err = Posters::new(&url, "t")
            .get(store, "imdb:tt1")
            .err()
            .unwrap();
        let want = format!(
            "could not reach Plex: Client error '404 Not Found' for url '{base}/library/metadata/77/thumb'\n\
             For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
        );
        assert!(matches!(err, ExploreError::Unavailable(m) if m == want));
        served.join().unwrap();
    }

    #[test]
    fn unconfigured_plex_is_refused_before_the_store_is_read() {
        let posters = Posters::new("", "token");
        let err = posters
            .get(|| panic!("the store was opened"), "imdb:tt1")
            .err()
            .unwrap();
        assert!(
            matches!(err, ExploreError::Unavailable(m) if m == "PLEX_URL/PLEX_TOKEN are not configured")
        );
    }
}
