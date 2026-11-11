//! The Search tab's server side: the Frame index, searched from Plex DB-EX
//! (`plexdb/explore_search.py`).
//!
//! The page only draws (ADR-0017): it never reaches Plex, the encoder or a tokenizer.
//! Everything it shows comes through here.
//!
//! - **Searching and drawing** run `Reader::search_shots` and `Reader::draw`, the reader
//!   crate's one implementation, in this process, so the page, `plexdb search` and a Rust
//!   consumer cannot disagree. (`Reader::search_drawn` replaces the ranking with the picks;
//!   the page shows both, so the two halves are called here.)
//! - **Token boxes** come from the Hugging Face tokenizer matching `PLEXDB_FRAME_MODEL`.
//!   The same tokenizer cuts a phrase at the 75th token before it is searched, so the
//!   page's "not searched" line is true.
//! - **Frames and clips** are [`crate::explore_media`]; **why** is [`crate::explore_why`].
//!
//! Everything here blocks (SQLite, the encoder through the reader, ffmpeg), so the server
//! calls it from tokio's blocking pool only.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicUsize;
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use std::time::{Duration, Instant};

use plexdb_reader::{PhraseCache, Reader, ReaderError, Search, ShotHit, Url, cached_vector};
use plexdb_schema::frames::FRAME_DIMS;
use plexdb_store::{open_readonly, py_path, py_strip};
use rusqlite::{Connection, OpenFlags, params_from_iter};
use serde_json::Value;
use tokenizers::Tokenizer;

use crate::explore_media::{EXTRA_PART, MAX_CLIP_MS, Media};
use crate::explore_views::{ExploreError, MAX_ITEM_ID};
use crate::explore_why::{Explained, Neighbour, WhyCache, WhyUnavailable, explain};
use crate::frames::{
    FrameConfig, PriorityStatus, StatusInput, decoder_report, py_repr, queue, section_titles,
    status,
};
use crate::localtime::Local;
use crate::plex::PlexSource;
use crate::pyjson::Json;

/// CLIP reads at most this many tokens of a phrase (its context is 77, less the start and
/// end markers). A token past it never reaches the encoder.
pub const TOKEN_LIMIT: usize = 75;
/// Item_ids in one pool, negatives in one search, and characters in one text.
const MAX_POOL: usize = 5000;
const MAX_NEGATIVES: usize = 20;
const MAX_TEXT: usize = 4000;
/// Rows a search returns, best first; every drawn row is added to them.
const RESULT_ROWS: usize = 100;
/// Frames beside the title in a row.
const THUMBS: usize = 3;
/// Suggestions in the pool's add box.
const SUGGESTIONS: usize = 8;
/// Reader runs at once. Plex DB-EX has no login, so this bounds what an anonymous caller
/// can spend.
const SEARCH_SLOTS: usize = 2;
/// Seconds a failed tokenizer load is remembered before the next attempt.
const RETRY: Duration = Duration::from_secs(30);
const FORMULA: &str = "score = phrase similarity − weight × strongest negative similarity";
/// How many failed titles the Frame index panel lists; `failed_total` counts the rest.
const FAILED_SHOWN: usize = 20;
/// What the panel says of a title that failed before `frame_index_state.reason` existed.
const UNKNOWN_FAILURE: &str = "failed (see harvester log)";
/// How long the explorer keeps Plex's library names, and how soon it asks again after a failure.
const NAMES_TTL: Duration = Duration::from_secs(600);
const NAMES_RETRY: Duration = Duration::from_secs(30);

fn unavailable(message: impl Into<String>) -> ExploreError {
    ExploreError::Unavailable(message.into())
}

fn invalid(message: impl Into<String>) -> ExploreError {
    ExploreError::Invalid(message.into())
}

fn not_found(message: impl Into<String>) -> ExploreError {
    ExploreError::NotFound(message.into())
}

fn log(line: String) {
    eprintln!("explore: {line}");
}

// --- Python's numbers ---------------------------------------------------------------

/// The `float` Python holds after reading the reader's JSON: an f32 printed with its
/// shortest digits, then parsed as a double.
pub fn f32_json(x: f32) -> f64 {
    format!("{x}").parse().unwrap_or(f64::from(x))
}

/// Python's `round(x)`: to the nearest whole number, ties to even.
fn round_half_even(x: f64) -> i64 {
    let floor = x.floor();
    let diff = x - floor;
    let base = floor as i64;
    if diff > 0.5 || (diff == 0.5 && base % 2 != 0) {
        base + 1
    } else {
        base
    }
}

/// Python's `round(x, 1)`: the double's exact decimal expansion rounded to one place,
/// an exact tie going to the even digit.
fn py_round1(x: f64) -> f64 {
    let text = format!("{x:.60}");
    let (sign, digits) = match text.strip_prefix('-') {
        Some(rest) => (-1.0, rest),
        None => (1.0, text.as_str()),
    };
    let (whole, frac) = digits.split_once('.').unwrap_or((digits, ""));
    let mut tenths: i64 = whole.parse::<i64>().unwrap_or(0) * 10
        + i64::from(frac.as_bytes().first().map_or(0, |d| d - b'0'));
    let rest = &frac[1.min(frac.len())..];
    let up = match rest.as_bytes().first() {
        Some(b'6'..=b'9') => true,
        Some(b'5') => rest[1..].bytes().any(|b| b != b'0') || tenths % 2 != 0,
        _ => false,
    };
    if up {
        tenths += 1;
    }
    sign * (tenths as f64 / 10.0)
}

/// `_ms`: `h:mm:ss.t`.
fn ms_text(value: i64) -> String {
    let value = value.max(0);
    let (seconds, milli) = (value / 1000, value % 1000);
    let (minutes, seconds) = (seconds / 60, seconds % 60);
    let (hours, minutes) = (minutes / 60, minutes % 60);
    format!("{hours}:{minutes:02}:{seconds:02}.{}", milli / 100)
}

/// `_spread`: `count` offsets spread evenly across a Shot's frames, first to last.
fn spread(frames: &[(u64, f32)], count: usize) -> Vec<u64> {
    if frames.len() <= count {
        return frames.iter().map(|(t, _)| *t).collect();
    }
    let step = (frames.len() - 1) as f64 / (count - 1) as f64;
    (0..count)
        .map(|i| frames[round_half_even(i as f64 * step) as usize].0)
        .collect()
}

/// `_number`: a finite number in `[low, high]`, or `None` for a missing one.
fn number(
    value: Option<&Value>,
    name: &str,
    low: f64,
    high: f64,
) -> Result<Option<f64>, ExploreError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let Some(x) = value.as_f64().filter(|x| x.is_finite()) else {
        return Err(invalid(format!("{name} must be a number")));
    };
    if !(low <= x && x <= high) {
        return Err(invalid(format!(
            "{name} must be between {} and {}",
            crate::pyjson::float_repr(low),
            crate::pyjson::float_repr(high)
        )));
    }
    Ok(Some(x))
}

/// `_whole`: an integer (never a float or a bool) in `[low, high]`.
fn whole(value: Option<&Value>, name: &str, low: i128, high: i128) -> Result<i64, ExploreError> {
    let x: i128 = match value {
        Some(Value::Number(n)) if n.is_i64() => i128::from(n.as_i64().unwrap_or(0)),
        Some(Value::Number(n)) if n.is_u64() => i128::from(n.as_u64().unwrap_or(0)),
        _ => return Err(invalid(format!("{name} must be a whole number"))),
    };
    if !(low <= x && x <= high) {
        return Err(invalid(format!("{name} must be between {low} and {high}")));
    }
    Ok(x as i64)
}

/// Python's truthiness of a JSON value.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn py_list(items: &[String]) -> String {
    let quoted: Vec<String> = items.iter().map(|item| py_repr(item)).collect();
    format!("[{}]", quoted.join(", "))
}

// --- tokens --------------------------------------------------------------------

/// The tokenizer for `PLEXDB_FRAME_MODEL`, loaded on first use.
///
/// Loaded from `explicit` when that file is set; otherwise from the model's
/// `tokenizer-<model>.json` in `cache_dir`. Nothing is fetched: a missing file is reported
/// by `status`, so the container needs no outside network.
pub struct PhraseTokenizer {
    pub model: String,
    cache_dir: PathBuf,
    explicit: Option<PathBuf>,
    state: Mutex<TokenizerState>,
}

#[derive(Default)]
struct TokenizerState {
    tokenizer: Option<Arc<Tokenizer>>,
    failure: Option<(Instant, String)>,
}

impl PhraseTokenizer {
    pub fn new(model: &str, cache_dir: &Path, explicit: Option<PathBuf>) -> Self {
        PhraseTokenizer {
            model: model.to_string(),
            cache_dir: cache_dir.to_path_buf(),
            explicit,
            state: Mutex::new(TokenizerState::default()),
        }
    }

    fn cache_file(&self) -> PathBuf {
        self.cache_dir
            .join(format!("tokenizer-{}.json", self.model.replace('/', "--")))
    }

    /// Why the tokenizer cannot be used, or None once it can.
    pub fn status(&self) -> Option<String> {
        self.load().err()
    }

    fn load(&self) -> Result<Arc<Tokenizer>, String> {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(tokenizer) = &state.tokenizer {
            return Ok(tokenizer.clone());
        }
        // A failed load is not retried on every keystroke of every client.
        if let Some((at, message)) = &state.failure
            && at.elapsed() < RETRY
        {
            return Err(message.clone());
        }
        let outcome = if let Some(explicit) = &self.explicit {
            Tokenizer::from_file(explicit).map_err(|err| err.to_string())
        } else if self.model.is_empty() {
            Err(String::new())
        } else {
            Tokenizer::from_file(self.cache_file()).map_err(|err| err.to_string())
        };
        match outcome {
            Ok(tokenizer) => {
                let tokenizer = Arc::new(tokenizer);
                state.tokenizer = Some(tokenizer.clone());
                Ok(tokenizer)
            }
            Err(err) => {
                let message = if self.explicit.is_none() && self.model.is_empty() {
                    "PLEXDB_FRAME_MODEL is not set".to_string()
                } else {
                    let where_ = self.explicit.clone().unwrap_or_else(|| self.cache_file());
                    let model = if self.model.is_empty() {
                        "the encoder model"
                    } else {
                        self.model.as_str()
                    };
                    format!(
                        "no tokenizer for {model}: {err}. Set PLEXDB_FRAME_TOKENIZER to its \
                         tokenizer.json, or put it at {}",
                        where_.display()
                    )
                };
                state.failure = Some((Instant::now(), message.clone()));
                Err(message)
            }
        }
    }

    /// Each token as (text, start, end), offsets in characters of `text`.
    fn tokens(&self, text: &str) -> Result<Vec<(String, usize, usize)>, ExploreError> {
        let tokenizer = self.load().map_err(unavailable)?;
        let found = tokenizer
            .encode_char_offsets(text, false)
            .map_err(|err| unavailable(format!("the tokenizer failed: {err}")))?;
        let chars: Vec<char> = text.chars().collect();
        Ok(found
            .get_offsets()
            .iter()
            .map(|(start, end)| {
                let end = (*end).min(chars.len());
                let start = (*start).min(end);
                (chars[start..end].iter().collect(), start, end)
            })
            .collect())
    }

    fn analyse(&self, text: &str) -> Result<Json, ExploreError> {
        let pieces = self.tokens(text)?;
        Ok(Json::object([
            ("count", Json::Int(pieces.len() as i64)),
            ("limit", Json::Int(TOKEN_LIMIT as i64)),
            (
                "tokens",
                Json::Array(
                    pieces
                        .into_iter()
                        .enumerate()
                        .map(|(i, (piece, start, end))| {
                            Json::object([
                                ("text", Json::Str(piece)),
                                ("start", Json::Int(start as i64)),
                                ("end", Json::Int(end as i64)),
                                ("over", Json::Bool(i >= TOKEN_LIMIT)),
                            ])
                        })
                        .collect(),
                ),
            ),
        ]))
    }

    /// `text` up to the end of its 75th token: the part the encoder reads.
    pub fn searched(&self, text: &str) -> Result<String, ExploreError> {
        let pieces = self.tokens(text)?;
        if pieces.len() <= TOKEN_LIMIT {
            return Ok(text.to_string());
        }
        Ok(text.chars().take(pieces[TOKEN_LIMIT - 1].2).collect())
    }
}

// --- the tab -----------------------------------------------------------------------

/// A pool title: its row in `items` and whether the Frame index holds it.
type Title = (Option<String>, Option<i64>, String);

/// A file's modification time and size, or `(0, 0)` when it is absent.
fn stamp(path: &Path) -> (i64, u64) {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(path)
        .map(|meta| (meta.mtime() * 1_000_000_000 + meta.mtime_nsec(), meta.len()))
        .unwrap_or((0, 0))
}

type Sections = BTreeMap<String, BTreeSet<String>>;
/// The store's and its WAL's stamps, and the sections read at them.
type SectionsHeld = Option<([(i64, u64); 2], Arc<Sections>)>;
/// Until when the names hold, the names, and why Plex gave none.
type NamesHeld = Option<(Instant, Arc<BTreeMap<String, String>>, Option<String>)>;

/// Everything `/api/search/*` answers with.
pub struct Tab {
    config: FrameConfig,
    store: PathBuf,
    pub tokenizer: PhraseTokenizer,
    pub media: Media,
    why: WhyCache,
    sections: Mutex<SectionsHeld>,
    names: Mutex<NamesHeld>,
    /// Reader runs now; the server admits at most `SEARCH_SLOTS`.
    pub search_slots: AtomicUsize,
}

/// A search that could not run because the encoder refused its phrases: the page shows
/// the URL and the phrases beside the reason.
pub struct EncoderDown {
    pub url: String,
    pub phrases: Vec<String>,
    pub reason: String,
}

impl EncoderDown {
    pub fn message(&self) -> String {
        format!(
            "the encoder at {} refused {} phrase(s): {}",
            self.url,
            self.phrases.len(),
            self.reason
        )
    }
}

/// Why `/api/search/run` did not answer with results.
pub enum SearchFailure {
    Explore(ExploreError),
    EncoderDown(EncoderDown),
}

impl From<ExploreError> for SearchFailure {
    fn from(err: ExploreError) -> Self {
        SearchFailure::Explore(err)
    }
}

impl From<rusqlite::Error> for SearchFailure {
    fn from(err: rusqlite::Error) -> Self {
        SearchFailure::Explore(err.into())
    }
}

impl From<plexdb_store::StoreError> for SearchFailure {
    fn from(err: plexdb_store::StoreError) -> Self {
        SearchFailure::Explore(err.into())
    }
}

impl Tab {
    /// The tab for `store`, keeping its tokenizer and cuts under `data_dir`
    /// (`/explore-data` in the container).
    pub fn new(config: FrameConfig, store: PathBuf, data_dir: &Path) -> Self {
        let explicit = std::env::var("PLEXDB_FRAME_TOKENIZER")
            .ok()
            .map(|raw| py_strip(&raw).to_string())
            .filter(|raw| !raw.is_empty())
            .map(|raw| py_path(&raw));
        Tab {
            tokenizer: PhraseTokenizer::new(&config.model, data_dir, explicit),
            media: Media::new(config.clone(), store.clone(), data_dir),
            why: WhyCache::default(),
            sections: Mutex::new(None),
            names: Mutex::new(None),
            search_slots: AtomicUsize::new(0),
            config,
            store,
        }
    }

    /// Reader runs at once: a third search is told to try again.
    pub const SEARCH_SLOTS: usize = SEARCH_SLOTS;
    pub const BUSY: &'static str = "two searches are already running; try again in a moment";

    // -- configuration, shown to the person ------------------------------------------

    fn frames_file(&self) -> Result<PathBuf, ExploreError> {
        let Some(path) = self.config.frames_path() else {
            return Err(unavailable(
                "PLEXDB_FRAMES_DIR is not set, so there is no Frame index to search",
            ));
        };
        if !path.exists() {
            return Err(unavailable(format!(
                "no Frame index at {} yet — nothing has been indexed",
                path.display()
            )));
        }
        Ok(path)
    }

    fn frames_conn(&self) -> Result<Connection, ExploreError> {
        let path = self.frames_file()?;
        crate::frames::load_vec_extension();
        let conn = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        conn.busy_timeout(Duration::from_secs(10))?;
        Ok(conn)
    }

    fn store(&self) -> Result<Connection, ExploreError> {
        Ok(open_readonly(&self.store)?)
    }

    /// What the tab needs, found or missing, and the counts the empty page shows.
    pub fn status(&self) -> Result<Json, ExploreError> {
        let mut problems: Vec<(&str, Json)> = Vec::new();
        if self.config.encoder_url.is_empty() {
            problems.push(("encoder", Json::str("PLEXDB_FRAME_ENCODER_URL is not set")));
        }
        if let Some(why) = self.tokenizer.status() {
            problems.push(("tokenizer", Json::Str(why)));
        }
        if let Some(why) = self.media.problem() {
            problems.push(("media", Json::Str(why)));
        }
        let mut indexed: i64 = 0;
        match self.frames_conn() {
            Ok(frames) => match frames.query_row(
                "SELECT count(*) FROM frame_index_state WHERE error IS NULL",
                [],
                |row| row.get::<_, i64>(0),
            ) {
                Ok(count) => indexed = count,
                Err(err) => problems.push((
                    "frames",
                    Json::Str(format!("cannot read the Frame index: {err}")),
                )),
            },
            Err(ExploreError::Unavailable(why)) => problems.push(("frames", Json::Str(why))),
            Err(other) => return Err(other),
        }
        let movies: i64 = self.store()?.query_row(
            "SELECT count(*) FROM items WHERE type = 'movie'",
            [],
            |row| row.get(0),
        )?;
        Ok(Json::object([
            ("ready", Json::Bool(problems.is_empty())),
            ("problems", Json::object_from(problems)),
            ("model", Json::str(&self.config.model)),
            ("encoder_url", Json::str(&self.config.encoder_url)),
            ("movies", Json::Int(movies)),
            ("indexed", Json::Int(indexed)),
            ("limit", Json::Int(TOKEN_LIMIT as i64)),
        ]))
    }

    // -- tokens ------------------------------------------------------------------------

    pub fn tokenize(&self, texts: Option<&Value>) -> Result<Json, ExploreError> {
        let Some(Value::Array(texts)) = texts else {
            return Err(invalid(format!(
                "texts must be a list of at most {} strings",
                MAX_NEGATIVES + 1
            )));
        };
        if texts.len() > MAX_NEGATIVES + 1 {
            return Err(invalid(format!(
                "texts must be a list of at most {} strings",
                MAX_NEGATIVES + 1
            )));
        }
        let mut fields = Vec::new();
        for text in texts {
            let Some(text) = text.as_str().filter(|t| t.chars().count() <= MAX_TEXT) else {
                return Err(invalid(format!(
                    "each text must be a string of at most {MAX_TEXT} characters"
                )));
            };
            fields.push(self.tokenizer.analyse(text)?);
        }
        Ok(Json::object([
            ("fields", Json::Array(fields)),
            ("limit", Json::Int(TOKEN_LIMIT as i64)),
            ("model", Json::str(&self.tokenizer.model)),
        ]))
    }

    // -- the pool ------------------------------------------------------------------------

    /// Titles and collections whose name contains `q`, for the pool's add box.
    pub fn suggest(&self, q: &str) -> Result<Json, ExploreError> {
        let q = py_strip(q);
        if q.chars().count() < 2 {
            return Err(invalid("q must be at least 2 characters"));
        }
        let like = format!(
            "%{}%",
            q.replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_")
        );
        let low = q.to_lowercase();
        let conn = self.store()?;
        let mut titles: Vec<(String, Option<String>, Option<i64>, String)> = conn
            .prepare(
                "SELECT item_id, title, year, type FROM items \
                 WHERE type IN ('movie', 'show') AND title LIKE ?1 ESCAPE '\\'",
            )?
            .query_map([&like], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?
            .collect::<Result<_, _>>()?;
        let mut lists: Vec<(String, Option<String>, i64)> = conn
            .prepare(
                "SELECT c.collection_id, c.name, count(m.item_id) FROM collection c \
                 JOIN collection_membership m USING (collection_id) \
                 WHERE c.name LIKE ?1 ESCAPE '\\' GROUP BY c.collection_id",
            )?
            .query_map([&like], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        let key = |name: &Option<String>| {
            let lower = name.as_deref().unwrap_or("").to_lowercase();
            (!lower.starts_with(&low), lower)
        };
        titles.sort_by(|a, b| {
            let (ka, kb) = (key(&a.1), key(&b.1));
            (ka.0, &ka.1, a.2.unwrap_or(0)).cmp(&(kb.0, &kb.1, b.2.unwrap_or(0)))
        });
        lists.sort_by_key(|a| key(&a.1));
        Ok(Json::object([
            (
                "titles",
                Json::Array(
                    titles
                        .into_iter()
                        .take(SUGGESTIONS)
                        .map(|(item_id, title, year, kind)| {
                            Json::object([
                                ("item_id", Json::Str(item_id)),
                                ("title", Json::opt_str(title)),
                                ("year", year.map_or(Json::Null, Json::Int)),
                                ("kind", Json::Str(kind)),
                            ])
                        })
                        .collect(),
                ),
            ),
            (
                "collections",
                Json::Array(
                    lists
                        .into_iter()
                        .take(SUGGESTIONS)
                        .map(|(collection_id, name, members)| {
                            Json::object([
                                ("collection_id", Json::Str(collection_id)),
                                ("name", Json::opt_str(name)),
                                ("members", Json::Int(members)),
                            ])
                        })
                        .collect(),
                ),
            ),
        ]))
    }

    pub fn collection(&self, collection_id: &str) -> Result<Json, ExploreError> {
        let conn = self.store()?;
        let known: bool = conn
            .query_row(
                "SELECT 1 FROM collection WHERE collection_id = ?1",
                [collection_id],
                |_| Ok(()),
            )
            .map(|_| true)
            .or_else(|err| match err {
                rusqlite::Error::QueryReturnedNoRows => Ok(false),
                other => Err(other),
            })?;
        if !known {
            return Err(not_found(format!(
                "no collection {}",
                py_repr(collection_id)
            )));
        }
        let ids: Vec<String> = conn
            .prepare(
                "SELECT m.item_id FROM collection_membership m \
                 JOIN items i USING (item_id) WHERE m.collection_id = ?1 \
                 ORDER BY m.rank IS NULL, m.rank, i.title",
            )?
            .query_map([collection_id], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        Ok(Json::object([(
            "item_ids",
            Json::Array(ids.into_iter().take(MAX_POOL).map(Json::Str).collect()),
        )]))
    }

    fn ids(raw: Option<&Value>) -> Result<Vec<String>, ExploreError> {
        let Some(Value::Array(raw)) = raw else {
            return Err(invalid("item_ids must be a non-empty list"));
        };
        if raw.is_empty() {
            return Err(invalid("item_ids must be a non-empty list"));
        }
        if raw.len() > MAX_POOL {
            return Err(invalid(format!("a pool holds at most {MAX_POOL} titles")));
        }
        let mut ids: Vec<String> = Vec::new();
        let mut seen: std::collections::HashSet<&str> = std::collections::HashSet::new();
        for item in raw {
            let Some(item) = item
                .as_str()
                .filter(|s| !s.is_empty() && s.chars().count() <= MAX_ITEM_ID)
            else {
                return Err(invalid(format!(
                    "each item_id must be a string of 1-{MAX_ITEM_ID} characters"
                )));
            };
            if seen.insert(item) {
                ids.push(item.to_string());
            }
        }
        Ok(ids)
    }

    /// The movies and episodes in each Plex section, read from the store once and kept
    /// until the store file or its WAL changes (the sweep is the only writer).
    fn section_titles(&self) -> Result<Arc<Sections>, ExploreError> {
        let mut wal = self.store.as_os_str().to_owned();
        wal.push("-wal");
        let now = [stamp(&self.store), stamp(Path::new(&wal))];
        let mut held = self.sections.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some((at, sections)) = held.as_ref()
            && *at == now
        {
            return Ok(sections.clone());
        }
        let sections = Arc::new(section_titles(&self.store()?)?);
        *held = Some((now, sections.clone()));
        Ok(sections)
    }

    /// Plex's name for each library section, and why it could not be read (else None).
    ///
    /// Cached: names are read at most once per `NAMES_TTL`, a failure for `NAMES_RETRY`, so
    /// the panel's 30-second poll does not ask Plex each time.
    fn library_names(&self) -> (Arc<BTreeMap<String, String>>, Option<String>) {
        {
            let held = self.names.lock().unwrap_or_else(PoisonError::into_inner);
            if let Some((until, names, error)) = held.as_ref()
                && Instant::now() < *until
            {
                return (names.clone(), error.clone());
            }
        }
        let mut names = BTreeMap::new();
        let mut error = None;
        if self.config.plex_url.is_empty() || self.config.plex_token.is_empty() {
            error = Some("PLEX_URL and PLEX_TOKEN are not set".to_string());
        } else {
            match self.media.plex().and_then(|plex| plex.sections()) {
                Ok(sections) => {
                    for section in sections {
                        if !section.title.is_empty() {
                            names.insert(section.key, section.title);
                        }
                    }
                }
                Err(err) => {
                    log(format!("library names: {err}"));
                    error = Some("Plex did not answer".to_string());
                }
            }
        }
        let names = Arc::new(names);
        let keep = if error.is_some() {
            NAMES_RETRY
        } else {
            NAMES_TTL
        };
        *self.names.lock().unwrap_or_else(PoisonError::into_inner) =
            Some((Instant::now() + keep, names.clone(), error.clone()));
        (names, error)
    }

    /// The Frame index panel: what is indexing now, how far each section has got, and the
    /// newest failed titles with their reasons, never the full error (`failed_total` counts
    /// them all). `frames::status` is the one source, the same report `plexdb frames status`
    /// prints.
    pub fn frames_health(&self) -> Result<Json, ExploreError> {
        let frames_file = self.frames_file()?;
        let sections = self.section_titles()?;
        let (names, names_error) = self.library_names();
        let frames = self.frames_conn()?;
        let report = status(
            &frames,
            &sections,
            &StatusInput {
                path: &frames_file,
                priority: PriorityStatus::default(),
                hours: &self.config.frame_hours,
                now: Local::now(),
                encoder_reachable: None,
                ffmpeg_found: true,
                decoder: decoder_report(&frames_file, &self.config.decode),
            },
            Some(FAILED_SHOWN),
        )?;
        let waiting: BTreeSet<String> = frames
            .prepare("SELECT item_id FROM frame_queue")?
            .query_map([], |row| row.get::<_, rusqlite::types::Value>(0))?
            .collect::<Result<Vec<_>, _>>()?
            .into_iter()
            .map(|value| match value {
                rusqlite::types::Value::Text(text) => text,
                rusqlite::types::Value::Integer(n) => n.to_string(),
                other => format!("{other:?}"),
            })
            .collect();
        let failed: Vec<&Json> = match field(&report, "failed") {
            Some(Json::Array(items)) => items.iter().collect(),
            _ => Vec::new(),
        };
        let indexing = field(&report, "indexing").filter(|v| !matches!(v, Json::Null));
        let mut wanted: Vec<String> = failed.iter().filter_map(|f| text(f, "item_id")).collect();
        if let Some(indexing) = indexing
            && let Some(item_id) = text(indexing, "item_id")
        {
            wanted.push(item_id);
        }
        let titles = self.titles(&wanted)?;
        let named = |item_id: String| -> Vec<(&str, Json)> {
            let (title, year) = match titles.get(&item_id) {
                Some((title, year, _)) => (title.clone(), *year),
                None => (Some(item_id.clone()), None),
            };
            vec![
                ("item_id", Json::Str(item_id)),
                ("title", Json::opt_str(title)),
                ("year", year.map_or(Json::Null, Json::Int)),
            ]
        };
        let indexing_json = match indexing {
            None => Json::Null,
            Some(indexing) => {
                let mut pairs = named(text(indexing, "item_id").unwrap_or_default());
                pairs.push((
                    "since",
                    field(indexing, "since").cloned().unwrap_or(Json::Null),
                ));
                Json::object_from(pairs)
            }
        };
        let sections_json = match field(&report, "sections") {
            Some(Json::Array(items)) => items
                .iter()
                .map(|sec| {
                    let mut pairs: Vec<(&str, Json)> = match sec {
                        Json::Object(pairs) => {
                            pairs.iter().map(|(k, v)| (k.as_str(), v.clone())).collect()
                        }
                        _ => Vec::new(),
                    };
                    let id = text(sec, "section_id").unwrap_or_default();
                    pairs.push(("name", Json::opt_str(names.get(&id).cloned())));
                    Json::object_from(pairs)
                })
                .collect(),
            _ => Vec::new(),
        };
        let failed_json = failed
            .iter()
            .map(|f| {
                let item_id = text(f, "item_id").unwrap_or_default();
                let mut pairs = named(item_id.clone());
                let reason = text(f, "reason").filter(|r| !r.is_empty());
                pairs.push((
                    "reason",
                    Json::str(reason.as_deref().unwrap_or(UNKNOWN_FAILURE)),
                ));
                pairs.push(("at", field(f, "at").cloned().unwrap_or(Json::Null)));
                pairs.push(("queued", Json::Bool(waiting.contains(&item_id))));
                Json::object_from(pairs)
            })
            .collect();
        Ok(Json::object([
            ("indexing", indexing_json),
            ("sections", Json::Array(sections_json)),
            ("names_error", Json::opt_str(names_error)),
            (
                "queued",
                field(&report, "queued").cloned().unwrap_or(Json::Null),
            ),
            (
                "frame_hours",
                field(&report, "frame_hours").cloned().unwrap_or(Json::Null),
            ),
            (
                "decoder",
                field(&report, "decoder").cloned().unwrap_or(Json::Null),
            ),
            (
                "failed_total",
                field(&report, "failed_total")
                    .cloned()
                    .unwrap_or(Json::Null),
            ),
            ("failed", Json::Array(failed_json)),
        ]))
    }

    fn title_json(item_id: &str, title: Option<&Title>, state: &str) -> Json {
        let (name, year, kind) = match title {
            Some((name, year, kind)) => (name.clone(), *year, kind.clone()),
            None => (Some(item_id.to_string()), None, String::new()),
        };
        Json::object([
            ("item_id", Json::str(item_id)),
            ("title", Json::opt_str(name)),
            ("year", year.map_or(Json::Null, Json::Int)),
            ("kind", Json::Str(kind)),
            ("state", Json::str(state)),
        ])
    }

    /// Each pool title with whether the Frame index holds it.
    pub fn pool(&self, raw: Option<&Value>) -> Result<Json, ExploreError> {
        let ids = Self::ids(raw)?;
        let states = self.states(&ids)?;
        let titles = self.titles(&ids)?;
        let found: Vec<&String> = ids.iter().filter(|i| titles.contains_key(*i)).collect();
        let indexed = found
            .iter()
            .filter(|i| states.get(**i).map(String::as_str) == Some("indexed"))
            .count();
        Ok(Json::object([
            (
                "titles",
                Json::Array(
                    found
                        .iter()
                        .map(|i| {
                            Self::title_json(
                                i,
                                titles.get(*i),
                                states.get(*i).map_or("none", String::as_str),
                            )
                        })
                        .collect(),
                ),
            ),
            (
                "unknown",
                Json::Array(
                    ids.iter()
                        .filter(|i| !titles.contains_key(*i))
                        .map(|i| Json::str(i.as_str()))
                        .collect(),
                ),
            ),
            ("indexed", Json::Int(indexed as i64)),
        ]))
    }

    /// Every title the Frame index can search, as the grid shows a search over no pool.
    pub fn indexed_titles(&self) -> Result<Json, ExploreError> {
        let ids = self.indexed()?;
        let titles = self.titles(&ids)?;
        let mut found: Vec<&String> = ids.iter().filter(|i| titles.contains_key(*i)).collect();
        let key = |i: &String| {
            let (title, year, _) = &titles[i];
            (
                crate::saved_queries::casefold(title.as_deref().unwrap_or("")),
                year.unwrap_or(0),
                i.clone(),
            )
        };
        found.sort_by_cached_key(|i| key(i));
        Ok(Json::object([(
            "titles",
            Json::Array(
                found
                    .iter()
                    .map(|i| Self::title_json(i, titles.get(*i), "indexed"))
                    .collect(),
            ),
        )]))
    }

    fn titles(&self, ids: &[String]) -> Result<HashMap<String, Title>, ExploreError> {
        let mut out = HashMap::new();
        let conn = self.store()?;
        for chunk in ids.chunks(500) {
            let marks = vec!["?"; chunk.len()].join(",");
            let mut stmt = conn.prepare(&format!(
                "SELECT item_id, title, year, type FROM items WHERE item_id IN ({marks})"
            ))?;
            let rows = stmt.query_map(params_from_iter(chunk.iter()), |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    (row.get(1)?, row.get(2)?, row.get(3)?),
                ))
            })?;
            for row in rows {
                let (item_id, title) = row?;
                out.insert(item_id, title);
            }
        }
        Ok(out)
    }

    /// The Plex part each title was indexed from; a title outside the Frame index is absent.
    /// An extra has no Plex part and reads as `EXTRA_PART`.
    fn parts(&self, ids: &[String]) -> Result<HashMap<String, i64>, ExploreError> {
        let mut out = HashMap::new();
        let conn = match self.frames_conn() {
            Ok(conn) => conn,
            Err(ExploreError::Unavailable(_)) => return Ok(out),
            Err(other) => return Err(other),
        };
        for chunk in ids.chunks(500) {
            let marks = vec!["?"; chunk.len()].join(",");
            let mut stmt = conn.prepare(&format!(
                "SELECT item_id, part_id FROM frame_index_state \
                 WHERE item_id IN ({marks}) AND model IS NOT NULL"
            ))?;
            let rows = stmt.query_map(params_from_iter(chunk.iter()), |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?))
            })?;
            for row in rows {
                let (item_id, part) = row?;
                out.insert(item_id, part.unwrap_or(EXTRA_PART));
            }
        }
        Ok(out)
    }

    /// Every title the Frame index can search: the pool of a search given none.
    fn indexed(&self) -> Result<Vec<String>, ExploreError> {
        Ok(self
            .frames_conn()?
            .prepare(
                "SELECT item_id FROM frame_index_state WHERE model IS NOT NULL ORDER BY item_id",
            )?
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?)
    }

    fn states(&self, ids: &[String]) -> Result<HashMap<String, String>, ExploreError> {
        let mut out = HashMap::new();
        let conn = match self.frames_conn() {
            Ok(conn) => conn,
            Err(ExploreError::Unavailable(_)) => return Ok(out),
            Err(other) => return Err(other),
        };
        for chunk in ids.chunks(500) {
            let marks = vec!["?"; chunk.len()].join(",");
            let queued: Vec<String> = conn
                .prepare(&format!(
                    "SELECT item_id FROM frame_queue WHERE item_id IN ({marks})"
                ))?
                .query_map(params_from_iter(chunk.iter()), |row| row.get(0))?
                .collect::<Result<_, _>>()?;
            for item_id in queued {
                out.insert(item_id, "queued".to_string());
            }
            let indexed: Vec<(String, Option<String>)> = conn
                .prepare(&format!(
                    "SELECT item_id, error FROM frame_index_state WHERE item_id IN ({marks})"
                ))?
                .query_map(params_from_iter(chunk.iter()), |row| {
                    Ok((row.get(0)?, row.get(1)?))
                })?
                .collect::<Result<_, _>>()?;
            for (item_id, error) in indexed {
                let state = if error.is_some_and(|e| !e.is_empty()) {
                    "failed"
                } else {
                    "indexed"
                };
                out.insert(item_id, state.to_string());
            }
        }
        Ok(out)
    }

    /// Queue every pool title the Frame index does not hold, as `plexdb frames queue`.
    pub fn queue_missing(&self, raw: Option<&Value>) -> Result<Json, ExploreError> {
        let ids = Self::ids(raw)?;
        let states = self.states(&ids)?;
        let wanted: Vec<&String> = ids
            .iter()
            .filter(|i| matches!(states.get(*i).map(String::as_str), None | Some("failed")))
            .collect();
        let known = self.titles(&wanted.iter().map(|i| (*i).clone()).collect::<Vec<_>>())?;
        let frames = crate::frames::open_frames(&self.frames_file()?)?;
        let mut added = 0i64;
        let now = Local::now();
        for item_id in wanted {
            if !known.contains_key(item_id) {
                continue;
            }
            if queue(&frames, item_id, true, "explore", &now)? {
                added += 1;
            }
        }
        let waiting: i64 =
            frames.query_row("SELECT count(*) FROM frame_queue", [], |row| row.get(0))?;
        log(format!("search queued {added} title(s) for frames"));
        Ok(Json::object([
            ("queued", Json::Int(added)),
            ("waiting", Json::Int(waiting)),
        ]))
    }

    // -- searching -----------------------------------------------------------------------

    fn phrase_cache(&self, frames_file: &Path) -> Result<PhraseCache, ExploreError> {
        if self.config.encoder_url.is_empty() {
            return Err(unavailable(
                "PLEXDB_FRAME_ENCODER_URL is not set, so a new phrase cannot be embedded",
            ));
        }
        if self.config.model.is_empty() {
            return Err(unavailable(
                "PLEXDB_FRAME_MODEL is not set, so a new phrase cannot be embedded",
            ));
        }
        Ok(PhraseCache {
            path: self
                .config
                .phrase_cache_path()
                .unwrap_or_else(|| frames_file.with_file_name("phrases.db")),
            encoder_url: Url::parse(&self.config.encoder_url).map_err(|err| {
                unavailable(format!("encoder URL {}: {err}", self.config.encoder_url))
            })?,
            request_model: self.config.model.clone(),
        })
    }

    /// `_run_reader`'s reading of the reader's failure: what `plexdb-read` would have
    /// printed, classified the way the Python tab classifies it.
    fn reader_failed(&self, err: &ReaderError, phrases: &[String]) -> SearchFailure {
        let printed = format!("error: {err}");
        let first = printed
            .lines()
            .find(|line| !line.trim().is_empty())
            .unwrap_or("")
            .trim();
        let message: String = first.chars().take(300).collect();
        if message.to_lowercase().contains("encoder") {
            /// `^(error: )?encoder \w+( at \S+?)?: `, the prefix Python strips from the reader's line.
            static PREFIX: LazyLock<regex::Regex> = LazyLock::new(|| {
                regex::Regex::new(r"^(error: )?encoder \w+( at \S+?)?: ").expect("regex")
            });
            let prefix = &*PREFIX;
            let reason = prefix.replace(&message, "").into_owned();
            return SearchFailure::EncoderDown(EncoderDown {
                url: self.config.encoder_url.clone(),
                phrases: phrases.to_vec(),
                reason,
            });
        }
        SearchFailure::Explore(unavailable(message))
    }

    pub fn search(&self, body: &serde_json::Map<String, Value>) -> Result<Json, SearchFailure> {
        let Some(phrase) = body
            .get("phrase")
            .and_then(Value::as_str)
            .filter(|p| !py_strip(p).is_empty())
        else {
            return Err(invalid("phrase must be text").into());
        };
        if phrase.chars().count() > MAX_TEXT {
            return Err(invalid(format!("phrase is over {MAX_TEXT} characters")).into());
        }
        let raw_negatives: Vec<&Value> = match body.get("negatives") {
            None => Vec::new(),
            Some(Value::Array(items)) if items.len() <= MAX_NEGATIVES => items.iter().collect(),
            _ => {
                return Err(invalid(format!(
                    "negatives must be a list of at most {MAX_NEGATIVES} strings"
                ))
                .into());
            }
        };
        if !raw_negatives
            .iter()
            .all(|n| n.as_str().is_some_and(|s| s.chars().count() <= MAX_TEXT))
        {
            return Err(invalid("each negative must be text").into());
        }
        let weight = number(body.get("weight"), "weight", 0.0, 10.0)?;
        let temperature = number(body.get("temperature"), "temperature", 0.0, 100.0)?;
        if temperature == Some(0.0) {
            return Err(invalid("temperature must be above 0").into());
        }
        let mut draw: Option<(i64, i64)> = None;
        if let Some(asked) = body.get("draw").filter(|d| !d.is_null()) {
            let Value::Object(asked) = asked else {
                return Err(invalid("draw must be an object with n and seed").into());
            };
            let n = whole(asked.get("n"), "draw.n", 1, RESULT_ROWS as i128)?;
            let seed = match asked.get("seed") {
                None => 0,
                Some(seed) => whole(Some(seed), "draw.seed", 0, i128::from(i64::MAX))?,
            };
            draw = Some((n, seed));
        }
        let ids = if truthy(body.get("pool")) {
            Self::ids(body.get("pool"))?
        } else {
            self.indexed()?
        };
        if ids.is_empty() {
            return Err(invalid("no title is indexed yet").into());
        }
        let frames_file = self.frames_file()?;
        // What the encoder reads: each text cut at its 75th token, blanks dropped.
        let searched = self.tokenizer.searched(py_strip(phrase))?;
        let mut negatives = Vec::new();
        for text in &raw_negatives {
            let cut = self
                .tokenizer
                .searched(py_strip(text.as_str().unwrap_or("")))?;
            if !cut.is_empty() {
                negatives.push(cut);
            }
        }
        let phrases: Vec<String> = std::iter::once(searched.clone())
            .chain(negatives.iter().cloned())
            .collect();
        if phrases.iter().any(|text| text.starts_with("--")) {
            return Err(invalid("a phrase or negative cannot start with --").into());
        }
        let cache = self.phrase_cache(&frames_file)?;
        let reader = Reader::open(&self.store, Some(frames_file.as_path()), Some(cache))
            .map_err(|err| self.reader_failed(&err, &phrases))?;
        let pool: Vec<&str> = ids.iter().map(String::as_str).collect();
        let negative_refs: Vec<&str> = negatives.iter().map(String::as_str).collect();
        let query = Search {
            pool: &pool,
            phrase: &searched,
            negatives: &negative_refs,
            weight: weight.map(|w| w as f32),
            temperature: temperature.map(|t| t as f32),
        };
        let found = reader
            .search_shots(&query)
            .map_err(|err| self.reader_failed(&err, &phrases))?;
        let mut picks: HashMap<(String, u64), i64> = HashMap::new();
        if let Some((n, seed)) = draw {
            for (order, shot) in reader
                .draw(&found, n as usize, seed as u64)
                .into_iter()
                .enumerate()
            {
                picks.insert((shot.item_id, shot.in_ms), order as i64 + 1);
            }
        }
        Ok(self.shape(
            &found,
            &searched,
            &negatives,
            weight,
            temperature,
            draw,
            &picks,
        )?)
    }

    #[allow(clippy::too_many_arguments)]
    fn shape(
        &self,
        found: &plexdb_reader::ShotSearch,
        searched: &str,
        negatives: &[String],
        weight: Option<f64>,
        temperature: Option<f64>,
        draw: Option<(i64, i64)>,
        picks: &HashMap<(String, u64), i64>,
    ) -> Result<Json, ExploreError> {
        let shots: &[ShotHit] = &found.shots;
        let keep: Vec<(usize, &ShotHit)> = shots
            .iter()
            .enumerate()
            .map(|(i, shot)| (i + 1, shot))
            .filter(|(rank, shot)| {
                *rank <= RESULT_ROWS || picks.contains_key(&(shot.item_id.clone(), shot.in_ms))
            })
            .collect();
        let shown: Vec<String> = keep
            .iter()
            .map(|(_, shot)| shot.item_id.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let titles = self.titles(&shown)?;
        let parts = self.parts(&shown)?;
        let rows = keep
            .iter()
            .map(|(rank, shot)| {
                let sims = &shot.negative_sims;
                let worst = (0..sims.len()).fold(None, |best: Option<usize>, i| match best {
                    Some(b) if sims[i] <= sims[b] => Some(b),
                    _ => Some(i),
                });
                let (title, year, kind) = match titles.get(&shot.item_id) {
                    Some((title, year, kind)) => (title.clone(), *year, kind.clone()),
                    None => (Some(shot.item_id.clone()), None, String::new()),
                };
                let frames = Json::Array(
                    shot.frames
                        .iter()
                        .map(|(t, sim)| {
                            Json::object([
                                ("t_ms", Json::Uint(*t)),
                                ("sim", Json::Float(f32_json(*sim))),
                            ])
                        })
                        .collect(),
                );
                let seconds = py_round1((shot.out_ms as f64 - shot.in_ms as f64) / 1000.0);
                Json::object([
                    ("rank", Json::Int(*rank as i64)),
                    ("item_id", Json::str(shot.item_id.as_str())),
                    ("title", Json::opt_str(title)),
                    ("year", year.map_or(Json::Null, Json::Int)),
                    ("kind", Json::Str(kind)),
                    (
                        "part_id",
                        parts
                            .get(&shot.item_id)
                            .map_or(Json::Null, |p| Json::Int(*p)),
                    ),
                    ("in_ms", Json::Uint(shot.in_ms)),
                    ("out_ms", Json::Uint(shot.out_ms)),
                    ("in", Json::Str(ms_text(shot.in_ms as i64))),
                    ("out", Json::Str(ms_text(shot.out_ms as i64))),
                    ("seconds", Json::Float(seconds)),
                    ("frames", frames),
                    (
                        "thumbs",
                        Json::Array(
                            spread(&shot.frames, THUMBS)
                                .into_iter()
                                .map(Json::Uint)
                                .collect(),
                        ),
                    ),
                    ("phrase_sim", Json::Float(f32_json(shot.phrase_sim))),
                    ("score", Json::Float(f32_json(shot.score))),
                    ("p", shot.p.map_or(Json::Null, |p| Json::Float(f32_json(p)))),
                    (
                        "strongest_negative",
                        match worst {
                            None => Json::Null,
                            Some(w) => Json::object([
                                ("phrase", Json::str(negatives[w].as_str())),
                                ("sim", Json::Float(f32_json(sims[w]))),
                            ]),
                        },
                    ),
                    (
                        "drawn",
                        picks
                            .get(&(shot.item_id.clone(), shot.in_ms))
                            .map_or(Json::Null, |d| Json::Int(*d)),
                    ),
                ])
            })
            .collect();
        Ok(Json::object([
            ("shots", Json::Array(rows)),
            ("total", Json::Int(shots.len() as i64)),
            ("indexed", Json::Int(found.indexed as i64)),
            ("pool", Json::Int(found.pool as i64)),
            ("model", Json::str(found.model.as_str())),
            (
                "searched",
                Json::object([
                    ("phrase", Json::str(searched)),
                    (
                        "negatives",
                        Json::Array(negatives.iter().map(|n| Json::str(n.as_str())).collect()),
                    ),
                ]),
            ),
            ("weight", Json::Float(weight.unwrap_or(0.5))),
            ("temperature", temperature.map_or(Json::Null, Json::Float)),
            (
                "draw",
                match draw {
                    None => Json::Null,
                    Some((n, seed)) => Json::object([
                        ("n", Json::Int(n)),
                        ("seed", Json::Int(seed)),
                        ("picks", Json::Int(picks.len() as i64)),
                    ]),
                },
            ),
            ("formula", Json::str(FORMULA)),
        ]))
    }

    // -- why a Shot matched -------------------------------------------------------------

    /// The numbers behind one result row: its score against the whole library, the phrase
    /// across its title, and the frames it looks most like (`explore_why`).
    pub fn why(&self, body: &serde_json::Map<String, Value>) -> Result<Json, ExploreError> {
        let Some(phrase) = body
            .get("phrase")
            .and_then(Value::as_str)
            .filter(|p| !py_strip(p).is_empty() && p.chars().count() <= MAX_TEXT)
        else {
            return Err(invalid("phrase must be the searched text"));
        };
        let Some(item_id) = body
            .get("item_id")
            .and_then(Value::as_str)
            .filter(|i| !i.is_empty() && i.chars().count() <= MAX_ITEM_ID)
        else {
            return Err(invalid("item_id is required"));
        };
        let in_ms = whole(body.get("in_ms"), "in_ms", 0, 1 << 40)?;
        let frames = self.frames_conn()?;
        let state: Option<(String, Option<String>)> = frames
            .query_row(
                "SELECT model, model_version FROM frame_index_state \
                 WHERE item_id = ?1 AND model IS NOT NULL AND error IS NULL",
                [item_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map(Some)
            .or_else(none_when_absent)?;
        let shot: Option<i64> = frames
            .query_row(
                "SELECT shot_ix FROM shots WHERE item_id = ?1 AND in_ms = ?2",
                rusqlite::params![item_id, in_ms],
                |row| row.get(0),
            )
            .map(Some)
            .or_else(none_when_absent)?;
        let (Some((model, version)), Some(shot_ix)) = (state, shot) else {
            return Err(not_found(format!(
                "{item_id} has no Shot starting at {in_ms} ms in the Frame index"
            )));
        };
        drop(frames);
        let frames_file = self.frames_file()?;
        let searched = self.tokenizer.searched(py_strip(phrase))?;
        let began = Instant::now();
        let out = (|| -> Result<Explained, WhyUnavailable> {
            let vector = phrase_vector(
                self.config.phrase_cache_path().as_deref(),
                &searched,
                &model,
                version.as_deref().unwrap_or(""),
            )?;
            let library = self.why.library(&frames_file)?;
            explain(&library, &vector, item_id, shot_ix)
        })()
        .map_err(|WhyUnavailable(message)| unavailable(message))?;
        let mut names: BTreeSet<String> = BTreeSet::from([item_id.to_string()]);
        for entry in out.same.iter().chain(&out.other) {
            names.insert(entry.item_id.clone());
        }
        let names: Vec<String> = names.into_iter().collect();
        let titles = self.titles(&names)?;
        let parts = self.parts(&names)?;
        let titled = |entry: &Neighbour| {
            let (title, year) = match titles.get(&entry.item_id) {
                Some((title, year, _)) => (title.clone(), *year),
                None => (Some(entry.item_id.clone()), None),
            };
            Json::object([
                ("item_id", Json::str(entry.item_id.as_str())),
                ("t_ms", Json::Int(entry.t_ms)),
                ("image_sim", Json::Float(f64::from(entry.image_sim))),
                ("phrase_sim", Json::Float(f64::from(entry.phrase_sim))),
                ("title", Json::opt_str(title)),
                ("year", year.map_or(Json::Null, Json::Int)),
                (
                    "part_id",
                    parts
                        .get(&entry.item_id)
                        .map_or(Json::Null, |p| Json::Int(*p)),
                ),
            ])
        };
        let (title, year, kind) = match titles.get(item_id) {
            Some((title, year, kind)) => (title.clone(), *year, kind.clone()),
            None => (Some(item_id.to_string()), None, String::new()),
        };
        log(format!(
            "why {} in_ms={in_ms} frames={} median={:.3} top1={:.3} best={:.3} ms={}",
            py_repr(item_id),
            out.frames,
            out.median,
            out.top1,
            out.best.1,
            began.elapsed().as_millis()
        ));
        Ok(Json::object([
            ("frames", Json::Int(out.frames as i64)),
            ("titles", Json::Int(out.titles as i64)),
            ("median", Json::Float(f64::from(out.median))),
            ("top1", Json::Float(f64::from(out.top1))),
            ("highest", Json::Float(f64::from(out.highest))),
            ("is_highest", Json::Bool(out.is_highest)),
            (
                "best",
                Json::object([
                    ("t_ms", Json::Int(out.best.0)),
                    ("sim", Json::Float(f64::from(out.best.1))),
                ]),
            ),
            (
                "series",
                Json::Array(
                    out.series
                        .iter()
                        .map(|(t, s)| Json::Array(vec![Json::Int(*t), Json::Float(f64::from(*s))]))
                        .collect(),
                ),
            ),
            (
                "peaks",
                Json::Array(
                    out.peaks
                        .iter()
                        .map(|(t, s)| {
                            Json::object([
                                ("t_ms", Json::Int(*t)),
                                ("sim", Json::Float(f64::from(*s))),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("same", Json::Array(out.same.iter().map(titled).collect())),
            ("other", Json::Array(out.other.iter().map(titled).collect())),
            (
                "title",
                Json::object([
                    ("item_id", Json::str(item_id)),
                    ("title", Json::opt_str(title)),
                    ("year", year.map_or(Json::Null, Json::Int)),
                    ("kind", Json::Str(kind)),
                ]),
            ),
            (
                "part_id",
                parts.get(item_id).map_or(Json::Null, |p| Json::Int(*p)),
            ),
        ]))
    }

    // -- frames and clips -----------------------------------------------------------------

    /// The Plex part the title was indexed from and its last video frame's ms, or None.
    ///
    /// `asked` is the part the page's URL names. A replaced file makes it stale, and a
    /// stale URL gets no cut, so the browser never files the new file's frame under the
    /// old URL.
    fn part(&self, item_id: &str, asked: i64) -> Result<(i64, Option<i64>), ExploreError> {
        let frames = match self.frames_conn() {
            Ok(conn) => conn,
            Err(ExploreError::Unavailable(why)) => return Err(not_found(why)),
            Err(other) => return Err(other),
        };
        let row: Option<(Option<i64>, Option<i64>)> = frames
            .query_row(
                "SELECT part_id, duration_ms FROM frame_index_state \
                 WHERE item_id = ?1 AND model IS NOT NULL",
                [item_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map(Some)
            .or_else(none_when_absent)?;
        let Some((part, length)) = row else {
            return Err(not_found(format!("{item_id} is not in the Frame index")));
        };
        let part = part.unwrap_or(EXTRA_PART);
        if part != asked {
            return Err(not_found(format!(
                "{item_id} was re-indexed from another file; search again"
            )));
        }
        Ok((part, length))
    }

    pub fn frame(
        &self,
        item_id: &str,
        part_id: i64,
        t_ms: i64,
        large: bool,
    ) -> Result<PathBuf, ExploreError> {
        let (part, length) = self.part(item_id, part_id)?;
        if let Some(length) = length
            && t_ms > length
        {
            return Err(invalid(format!(
                "t_ms is past the end of the title ({length} ms)"
            )));
        }
        self.media.frame(item_id, part, t_ms, large)
    }

    pub fn clip(
        &self,
        item_id: &str,
        part_id: i64,
        in_ms: i64,
        out_ms: i64,
    ) -> Result<PathBuf, ExploreError> {
        let (part, length) = self.part(item_id, part_id)?;
        if !(0 <= in_ms && in_ms < out_ms) {
            return Err(invalid("the clip window is empty"));
        }
        if out_ms - in_ms > MAX_CLIP_MS || length.is_some_and(|length| out_ms > length) {
            return Err(invalid(format!(
                "the clip window must lie inside the title and be at most {MAX_CLIP_MS} ms"
            )));
        }
        self.media.clip(item_id, part, in_ms, out_ms)
    }
}

fn none_when_absent<T>(err: rusqlite::Error) -> Result<Option<T>, rusqlite::Error> {
    match err {
        rusqlite::Error::QueryReturnedNoRows => Ok(None),
        other => Err(other),
    }
}

/// The vector the reader cached for `phrase`, which it embeds before ranking.
fn phrase_vector(
    cache_file: Option<&Path>,
    phrase: &str,
    model: &str,
    version: &str,
) -> Result<Vec<f32>, WhyUnavailable> {
    let Some(cache_file) = cache_file.filter(|p| p.exists()) else {
        return Err(WhyUnavailable(
            "the Phrase cache is missing; search again".into(),
        ));
    };
    cached_vector(cache_file, phrase, model, version, FRAME_DIMS)
        .map_err(|err| WhyUnavailable(err.to_string()))?
        .ok_or_else(|| {
            WhyUnavailable("this phrase is not in the Phrase cache; search again".into())
        })
}

/// One named field of a JSON object.
fn field<'a>(value: &'a Json, name: &str) -> Option<&'a Json> {
    match value {
        Json::Object(pairs) => pairs.iter().find(|(k, _)| k == name).map(|(_, v)| v),
        _ => None,
    }
}

fn text(value: &Json, name: &str) -> Option<String> {
    match field(value, name) {
        Some(Json::Str(text)) => Some(text.clone()),
        _ => None,
    }
}

/// `explore: search …` as the Python tab logs it.
pub fn search_log(out: &Json, began: Instant) -> String {
    let searched = field(out, "searched");
    let phrase = searched.and_then(|s| text(s, "phrase")).unwrap_or_default();
    let negatives: Vec<String> = match searched.and_then(|s| field(s, "negatives")) {
        Some(Json::Array(items)) => items
            .iter()
            .filter_map(|n| match n {
                Json::Str(s) => Some(s.clone()),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let int = |name: &str| match field(out, name) {
        Some(Json::Int(n)) => n.to_string(),
        _ => "0".to_string(),
    };
    let draw = match field(out, "draw") {
        Some(Json::Object(pairs)) => {
            let parts: Vec<String> = pairs
                .iter()
                .map(|(k, v)| {
                    format!(
                        "'{k}': {}",
                        match v {
                            Json::Int(n) => n.to_string(),
                            _ => "None".to_string(),
                        }
                    )
                })
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
        _ => "None".to_string(),
    };
    format!(
        "search phrase={} negatives={} pool={} shots={} draw={draw} ms={}",
        py_repr(&phrase),
        py_list(&negatives),
        int("pool"),
        int("total"),
        began.elapsed().as_millis()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_follows_python() {
        assert_eq!(round_half_even(2.5), 2);
        assert_eq!(round_half_even(3.5), 4);
        assert_eq!(round_half_even(1.4999), 1);
        // round(2.25, 1) is an exact tie -> 2.2; 2.35 sits above its tie in binary -> 2.4
        assert_eq!(py_round1(2.25), 2.2);
        assert_eq!(py_round1(2.35), 2.4);
        assert_eq!(py_round1(2.45), 2.5);
        assert_eq!(py_round1(2.55), 2.5);
        assert_eq!(py_round1(5.0), 5.0);
        assert_eq!(py_round1(0.05), 0.1);
    }

    #[test]
    fn f32_values_print_as_python_read_them() {
        assert_eq!(crate::pyjson::float_repr(f32_json(0.1598)), "0.1598");
        assert_eq!(crate::pyjson::float_repr(f32_json(1.0)), "1.0");
    }

    #[test]
    fn ms_text_and_spread_match_the_python_helpers() {
        assert_eq!(ms_text(1_451_400), "0:24:11.4");
        assert_eq!(ms_text(-5), "0:00:00.0");
        let frames: Vec<(u64, f32)> = (0..6).map(|i| (i * 100, 0.0)).collect();
        // step 2.5: round(2.5) is 2 in Python, round(5.0) is 5
        assert_eq!(spread(&frames, 3), vec![0, 200, 500]);
        assert_eq!(spread(&frames[..2], 3), vec![0, 100]);
    }
}
