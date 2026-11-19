//! The store API's one table of calls: [`Call::parse`] reads a request — a method, a
//! path, a query string and a JSON body — into a [`Call`], and [`Reader::answer`] runs it
//! and returns the reply as JSON text.
//!
//! Plex DB-EX's store routes and `plexdb-read` both go through it (ADR-0031), so the HTTP
//! API and the command line take the same paths and print the same bytes. The routes and
//! their replies are specified in `docs/api.md`.

use percent_encoding::percent_decode_str;
use serde::Serialize;
use serde_json::Value;

use crate::{Reader, ReaderError, Search};

/// The rows a page holds when the request names no `limit`.
pub const DEFAULT_LIMIT: usize = 100;
/// The most rows a page may hold.
pub const MAX_LIMIT: usize = 1000;
/// The longest `item_id` a request may name.
const MAX_ID: usize = 200;
/// The longest keyword, namespace, tag, type, surface, phrase or cursor a request may name.
const MAX_TEXT: usize = 500;
/// The most ids a search pool may name: above the 13,210 movies of the 2026-11-18 store.
const MAX_POOL: usize = 20_000;

/// Where a page starts and how long it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageArgs {
    pub limit: usize,
    /// The `next` of the page before; `None` for the first page.
    pub after: Option<String>,
}

/// Which titles `/items` lists.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ItemFilter {
    /// `items.type`: `movie`, `show`, `episode`, `extra`.
    pub kind: Option<String>,
    /// A Plex library section id: titles with an entry in it.
    pub library: Option<String>,
    /// A show's `item_id`: its episodes.
    pub show: Option<String>,
}

/// The related facts each listed title carries.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Include {
    pub enrichment: bool,
    /// The one namespace `enrichment` holds; every namespace when `None`.
    pub namespace: Option<String>,
    pub external_ids: bool,
    pub audio_facts: bool,
    pub plex: bool,
}

/// Which end of an edge a title is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Out,
    In,
}

/// One request the store API answers.
#[derive(Debug, Clone, PartialEq)]
pub enum Call {
    Items {
        filter: ItemFilter,
        include: Include,
        page: PageArgs,
    },
    Item {
        item_id: String,
        include: Include,
    },
    Enrichment {
        item_id: String,
        namespace: Option<String>,
    },
    Edges {
        item_id: String,
        edge_type: Option<String>,
        direction: Direction,
    },
    Collections {
        item_id: String,
    },
    AudioFacts {
        item_id: String,
    },
    ExternalIds {
        item_id: String,
    },
    Plex {
        item_id: String,
    },
    MediaParts {
        item_id: String,
    },
    ItemPlays {
        item_id: String,
    },
    RoleStatements {
        item_id: String,
    },
    Cuts {
        item_id: String,
    },
    Chapters {
        item_id: String,
    },
    Plays {
        account: Option<i64>,
        since: Option<i64>,
        page: PageArgs,
    },
    Accounts,
    /// One account's taste vector, or the house's with no account.
    Taste {
        account: Option<i64>,
    },
    /// One account's watched units, or the house's with no account.
    Watched {
        account: Option<i64>,
        page: PageArgs,
    },
    Keywords {
        surface: Option<String>,
        page: PageArgs,
    },
    Keyword {
        keyword: String,
    },
    KeywordPairs {
        keyword: String,
    },
    KeywordRoles {
        keyword: String,
    },
    Pairs {
        decision: Option<String>,
        page: PageArgs,
    },
    TagNeighbours {
        kind: String,
        tag: String,
    },
    Capabilities,
    /// [`Reader::search_drawn`]: the ranking, or with `draw` the `draw` picks.
    Search {
        pool: Vec<String>,
        phrase: String,
        negatives: Vec<String>,
        weight: Option<f32>,
        temperature: Option<f32>,
        draw: Option<usize>,
        seed: u64,
    },
}

/// Which files a [`Reader`] must be opened on to answer a [`Call`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Needs {
    /// The store alone.
    Store,
    /// `frames.db` attached.
    Frames,
    /// `frames.db` and a Phrase cache.
    Search,
    /// Whatever there is: the answer reports which.
    Any,
}

fn bad(message: impl Into<String>) -> ReaderError {
    ReaderError::BadRequest(message.into())
}

/// A query string's parameters, each name at most once, a blank value read as absent.
struct Params(Vec<(String, String)>);

impl Params {
    fn parse(query: &str, allowed: &[&str]) -> Result<Self, ReaderError> {
        let mut pairs: Vec<(String, String)> = Vec::new();
        for (name, value) in url::form_urlencoded::parse(query.as_bytes()) {
            if !allowed.contains(&name.as_ref()) {
                return Err(bad(if allowed.is_empty() {
                    format!("unknown parameter {name}: this route takes none")
                } else {
                    format!(
                        "unknown parameter {name}: this route takes {}",
                        allowed.join(", ")
                    )
                }));
            }
            if pairs.iter().any(|(n, _)| *n == name) {
                return Err(bad(format!("{name} is given twice")));
            }
            if !value.is_empty() {
                pairs.push((name.into_owned(), value.into_owned()));
            }
        }
        Ok(Params(pairs))
    }

    fn text(&self, name: &str) -> Result<Option<String>, ReaderError> {
        match self.0.iter().find(|(n, _)| n == name) {
            Some((_, value)) if value.chars().count() > MAX_TEXT => {
                Err(bad(format!("{name} is over {MAX_TEXT} characters")))
            }
            Some((_, value)) => Ok(Some(value.clone())),
            None => Ok(None),
        }
    }

    fn required(&self, name: &str) -> Result<String, ReaderError> {
        self.text(name)?
            .ok_or_else(|| bad(format!("{name} is required")))
    }

    fn whole(&self, name: &str) -> Result<Option<i64>, ReaderError> {
        self.text(name)?
            .map(|raw| {
                raw.parse::<i64>()
                    .map_err(|_| bad(format!("{name} must be a whole number, got {raw:?}")))
            })
            .transpose()
    }

    fn page(&self) -> Result<PageArgs, ReaderError> {
        let limit = match self.text("limit")? {
            None => DEFAULT_LIMIT,
            Some(raw) => raw
                .parse::<usize>()
                .ok()
                .filter(|n| (1..=MAX_LIMIT).contains(n))
                .ok_or_else(|| {
                    bad(format!(
                        "limit must be a whole number from 1 to {MAX_LIMIT}, got {raw:?}"
                    ))
                })?,
        };
        Ok(PageArgs {
            limit,
            after: self.text("after")?,
        })
    }

    fn include(&self) -> Result<Include, ReaderError> {
        let mut include = Include::default();
        for token in self.text("include")?.unwrap_or_default().split(',') {
            match token.trim() {
                "" => {}
                "enrichment" => include.enrichment = true,
                "external_ids" => include.external_ids = true,
                "audio_facts" => include.audio_facts = true,
                "plex" => include.plex = true,
                other => {
                    return Err(bad(format!(
                        "include takes enrichment, external_ids, audio_facts and plex, got {other:?}"
                    )));
                }
            }
        }
        include.namespace = self.text("namespace")?;
        if include.namespace.is_some() && !include.enrichment {
            return Err(bad(
                "namespace narrows include=enrichment, which is not asked for",
            ));
        }
        Ok(include)
    }
}

/// The parameters every list takes beside its own.
const PAGE: [&str; 2] = ["limit", "after"];

fn with_page<'a>(own: &[&'a str]) -> Vec<&'a str> {
    own.iter().copied().chain(PAGE).collect()
}

fn item_id(segment: &str) -> Result<String, ReaderError> {
    if segment.chars().count() > MAX_ID {
        return Err(bad(format!("an item_id is at most {MAX_ID} characters")));
    }
    Ok(segment.to_string())
}

fn text_segment(what: &str, segment: &str) -> Result<String, ReaderError> {
    if segment.chars().count() > MAX_TEXT {
        return Err(bad(format!("a {what} is at most {MAX_TEXT} characters")));
    }
    Ok(segment.to_string())
}

fn account(segment: &str) -> Result<i64, ReaderError> {
    segment.parse::<i64>().map_err(|_| {
        bad(format!(
            "an account id is a Plex account id, a whole number, got {segment:?}"
        ))
    })
}

/// A path's segments, each percent-decoded after the split, so an encoded `/` inside a
/// keyword stays part of it.
fn segments(path: &str) -> Result<Vec<String>, ReaderError> {
    let path = path.strip_prefix('/').unwrap_or(path);
    path.split('/')
        .map(|segment| {
            percent_decode_str(segment)
                .decode_utf8()
                .map(|s| s.into_owned())
                .map_err(|_| bad(format!("the path segment {segment:?} is not UTF-8")))
        })
        .collect()
}

impl Call {
    /// Read a request into the call it names. `path` is the route's own path, such as
    /// `/items/imdb:tt1/enrichment`, still percent-encoded; `query` the text after `?`;
    /// `body` the JSON body of a POST.
    ///
    /// A path no route answers is [`ReaderError::NotFound`], a method the route does not
    /// take [`ReaderError::MethodNotAllowed`], and anything else wrong with the request
    /// [`ReaderError::BadRequest`] naming the parameter.
    pub fn parse(
        method: &str,
        path: &str,
        query: &str,
        body: Option<&Value>,
    ) -> Result<Call, ReaderError> {
        let segments = segments(path)?;
        let parts: Vec<&str> = segments.iter().map(String::as_str).collect();
        let no_route = || ReaderError::NotFound(format!("no store route answers {path}"));
        if parts.iter().any(|s| s.is_empty()) {
            return Err(no_route());
        }
        let post = matches!(parts.as_slice(), ["searches"]);
        let wanted = if post { "POST" } else { "GET" };
        if !method.eq_ignore_ascii_case(wanted) {
            return Err(ReaderError::MethodNotAllowed(format!(
                "{path} answers {wanted} only"
            )));
        }
        let q = |allowed: &[&str]| Params::parse(query, allowed);
        Ok(match parts.as_slice() {
            ["items"] => {
                let q = q(&with_page(&[
                    "type",
                    "library",
                    "show",
                    "include",
                    "namespace",
                ]))?;
                Call::Items {
                    filter: ItemFilter {
                        kind: q.text("type")?,
                        library: q.text("library")?,
                        show: q.text("show")?,
                    },
                    include: q.include()?,
                    page: q.page()?,
                }
            }
            ["items", id] => Call::Item {
                item_id: item_id(id)?,
                include: q(&["include", "namespace"])?.include()?,
            },
            ["items", id, "enrichment"] => Call::Enrichment {
                item_id: item_id(id)?,
                namespace: q(&["namespace"])?.text("namespace")?,
            },
            ["items", id, "edges"] => {
                let q = q(&["type", "direction"])?;
                Call::Edges {
                    item_id: item_id(id)?,
                    edge_type: q.text("type")?,
                    direction: match q.text("direction")?.as_deref() {
                        None | Some("out") => Direction::Out,
                        Some("in") => Direction::In,
                        Some(other) => {
                            return Err(bad(format!("direction is out or in, got {other:?}")));
                        }
                    },
                }
            }
            ["items", id, sub] => {
                q(&[])?;
                let item_id = item_id(id)?;
                match *sub {
                    "collections" => Call::Collections { item_id },
                    "audio-facts" => Call::AudioFacts { item_id },
                    "external-ids" => Call::ExternalIds { item_id },
                    "plex" => Call::Plex { item_id },
                    "media-parts" => Call::MediaParts { item_id },
                    "plays" => Call::ItemPlays { item_id },
                    "role-statements" => Call::RoleStatements { item_id },
                    "cuts" => Call::Cuts { item_id },
                    "chapters" => Call::Chapters { item_id },
                    _ => return Err(no_route()),
                }
            }
            ["plays"] => {
                let q = q(&with_page(&["account", "since"]))?;
                Call::Plays {
                    account: q.whole("account")?,
                    since: q.whole("since")?,
                    page: q.page()?,
                }
            }
            ["accounts"] => {
                q(&[])?;
                Call::Accounts
            }
            ["accounts", id, "taste"] => {
                q(&[])?;
                Call::Taste {
                    account: Some(account(id)?),
                }
            }
            ["accounts", id, "watched"] => Call::Watched {
                account: Some(account(id)?),
                page: q(&PAGE)?.page()?,
            },
            ["taste"] => {
                q(&[])?;
                Call::Taste { account: None }
            }
            ["watched"] => Call::Watched {
                account: None,
                page: q(&PAGE)?.page()?,
            },
            ["keywords"] => {
                let q = q(&with_page(&["surface"]))?;
                Call::Keywords {
                    surface: q.text("surface")?,
                    page: q.page()?,
                }
            }
            ["keywords", k] => {
                q(&[])?;
                Call::Keyword {
                    keyword: text_segment("keyword", k)?,
                }
            }
            ["keywords", k, "pairs"] => {
                q(&[])?;
                Call::KeywordPairs {
                    keyword: text_segment("keyword", k)?,
                }
            }
            ["keywords", k, "roles"] => {
                q(&[])?;
                Call::KeywordRoles {
                    keyword: text_segment("keyword", k)?,
                }
            }
            ["keyword-pairs"] => {
                let q = q(&with_page(&["decision"]))?;
                let decision = q.text("decision")?;
                if let Some(d) = decision.as_deref()
                    && !["accepted", "rejected", "undecided"].contains(&d)
                {
                    return Err(bad(format!(
                        "decision is accepted, rejected or undecided, got {d:?}"
                    )));
                }
                Call::Pairs {
                    decision,
                    page: q.page()?,
                }
            }
            ["tags", tag, "neighbours"] => {
                let q = q(&["kind"])?;
                Call::TagNeighbours {
                    kind: q.required("kind")?,
                    tag: text_segment("tag", tag)?,
                }
            }
            ["capabilities"] => {
                q(&[])?;
                Call::Capabilities
            }
            ["searches"] => {
                q(&[])?;
                search(body.ok_or_else(|| bad("a search needs a JSON body"))?)?
            }
            _ => return Err(no_route()),
        })
    }

    #[must_use]
    pub fn needs(&self) -> Needs {
        match self {
            Call::Search { .. } => Needs::Search,
            Call::Cuts { .. } | Call::Chapters { .. } => Needs::Frames,
            Call::Capabilities => Needs::Any,
            _ => Needs::Store,
        }
    }

    /// The title a call under `/items/{id}/` reads, which must exist.
    fn under_item(&self) -> Option<&str> {
        match self {
            Call::Enrichment { item_id, .. }
            | Call::Edges { item_id, .. }
            | Call::Collections { item_id }
            | Call::AudioFacts { item_id }
            | Call::ExternalIds { item_id }
            | Call::Plex { item_id }
            | Call::MediaParts { item_id }
            | Call::ItemPlays { item_id }
            | Call::RoleStatements { item_id }
            | Call::Cuts { item_id }
            | Call::Chapters { item_id } => Some(item_id),
            _ => None,
        }
    }
}

/// A body field holding text of at most [`MAX_TEXT`] characters.
fn body_text(body: &serde_json::Map<String, Value>, name: &str) -> Result<String, ReaderError> {
    match body.get(name).and_then(Value::as_str) {
        Some(text) if !text.is_empty() && text.chars().count() <= MAX_TEXT => Ok(text.to_string()),
        _ => Err(bad(format!(
            "{name} must be text of at most {MAX_TEXT} characters"
        ))),
    }
}

fn body_number(
    body: &serde_json::Map<String, Value>,
    name: &str,
) -> Result<Option<f32>, ReaderError> {
    match body.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_f64()
            .map(|n| Some(n as f32))
            .ok_or_else(|| bad(format!("{name} must be a number"))),
    }
}

/// `POST /searches`'s body as the search it asks for.
fn search(body: &Value) -> Result<Call, ReaderError> {
    let Value::Object(body) = body else {
        return Err(bad("a search body is a JSON object"));
    };
    const FIELDS: [&str; 6] = [
        "pool",
        "phrase",
        "negatives",
        "weight",
        "temperature",
        "draw",
    ];
    if let Some(field) = body.keys().find(|k| !FIELDS.contains(&k.as_str())) {
        return Err(bad(format!(
            "unknown field {field}: a search takes {}",
            FIELDS.join(", ")
        )));
    }
    let pool_error = || {
        bad(format!(
            "pool must be a list of at most {MAX_POOL} item ids"
        ))
    };
    let pool = match body.get("pool") {
        Some(Value::Array(items)) if items.len() <= MAX_POOL => items
            .iter()
            .map(|item| match item.as_str() {
                Some(id) if !id.is_empty() && id.chars().count() <= MAX_ID => Ok(id.to_string()),
                _ => Err(pool_error()),
            })
            .collect::<Result<Vec<_>, _>>()?,
        _ => return Err(pool_error()),
    };
    let negatives = match body.get("negatives") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .filter(|t| t.chars().count() <= MAX_TEXT)
                    .map(str::to_string)
            })
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| bad("negatives must be a list of text"))?,
        Some(_) => return Err(bad("negatives must be a list of text")),
    };
    let (draw, seed) = match body.get("draw") {
        None | Some(Value::Null) => (None, 0),
        Some(Value::Object(draw)) => {
            let n = draw
                .get("n")
                .and_then(Value::as_u64)
                .and_then(|n| usize::try_from(n).ok())
                .ok_or_else(|| bad("draw.n must be a whole number"))?;
            let seed = match draw.get("seed") {
                None | Some(Value::Null) => 0,
                Some(seed) => seed
                    .as_u64()
                    .ok_or_else(|| bad("draw.seed must be a whole number"))?,
            };
            (Some(n), seed)
        }
        Some(_) => return Err(bad("draw must be an object with n and seed")),
    };
    Ok(Call::Search {
        pool,
        phrase: body_text(body, "phrase")?,
        negatives,
        weight: body_number(body, "weight")?,
        temperature: body_number(body, "temperature")?,
        draw,
        seed,
    })
}

/// Serialized straight to text: going through `serde_json::Value` would widen every `f32`
/// to an `f64` and print digits the similarity never had.
fn text<T: Serialize>(value: &T) -> Result<String, ReaderError> {
    serde_json::to_string(value).map_err(ReaderError::Reply)
}

impl Reader {
    /// Run `call` and return its reply as JSON. A title, account, keyword or tag the store
    /// does not hold is [`ReaderError::NotFound`]; an empty list is `[]`.
    pub fn answer(&self, call: &Call) -> Result<String, ReaderError> {
        if let Some(item_id) = call.under_item()
            && !self.item_exists(item_id)?
        {
            return Err(no_item(item_id));
        }
        match call {
            Call::Items {
                filter,
                include,
                page,
            } => text(&self.items(filter, include, page)?),
            Call::Item { item_id, include } => text(
                &self
                    .item(item_id, include)?
                    .ok_or_else(|| no_item(item_id))?,
            ),
            Call::Enrichment { item_id, namespace } => {
                text(&self.enrichment_for(item_id, namespace.as_deref())?)
            }
            Call::Edges {
                item_id,
                edge_type,
                direction,
            } => text(&self.edges(item_id, edge_type.as_deref(), *direction)?),
            Call::Collections { item_id } => text(&self.collections_for(item_id)?),
            Call::AudioFacts { item_id } => text(&self.audio_facts_for(item_id)?),
            Call::ExternalIds { item_id } => text(&self.external_ids_for(item_id)?),
            Call::Plex { item_id } => text(&self.plex_for(item_id)?),
            Call::MediaParts { item_id } => text(&self.media_parts_for(item_id)?),
            Call::ItemPlays { item_id } => text(&self.plays_for(item_id)?),
            Call::RoleStatements { item_id } => text(&self.role_statements_for(item_id)?),
            Call::Cuts { item_id } => text(&self.cuts(item_id)?),
            Call::Chapters { item_id } => text(&self.chapters(item_id)?),
            Call::Plays {
                account,
                since,
                page,
            } => text(&self.plays(*account, *since, page)?),
            Call::Accounts => text(&self.accounts()?),
            Call::Taste { account } => match account {
                Some(account) => {
                    self.known_account(*account)?;
                    text(&self.taste_vector_for(*account)?)
                }
                None => text(&self.pooled_taste_vector()?),
            },
            Call::Watched { account, page } => {
                if let Some(account) = account {
                    self.known_account(*account)?;
                }
                text(&self.watched_units(*account, page.after.as_deref(), page.limit)?)
            }
            Call::Keywords { surface, page } => text(&self.keywords(surface.as_deref(), page)?),
            Call::Keyword { keyword } => text(&self.known_keyword(keyword)?),
            Call::KeywordPairs { keyword } => {
                self.known_keyword(keyword)?;
                text(&self.pairs_for(keyword)?)
            }
            Call::KeywordRoles { keyword } => {
                self.known_keyword(keyword)?;
                text(&self.roles_for(keyword)?)
            }
            Call::Pairs { decision, page } => {
                let after = page
                    .after
                    .as_deref()
                    .map(|raw| {
                        serde_json::from_str::<(String, String)>(raw).map_err(|_| {
                            bad(format!(
                                "after is the next of a keyword-pairs page, a JSON pair of keywords, got {raw:?}"
                            ))
                        })
                    })
                    .transpose()?;
                let after = after.as_ref().map(|(a, b)| (a.as_str(), b.as_str()));
                text(&self.pairs(decision.as_deref(), after, page.limit)?)
            }
            Call::TagNeighbours { kind, tag } => {
                if !self.tag_exists(kind, tag)? {
                    return Err(ReaderError::NotFound(format!(
                        "the {kind} tag network holds no tag {tag:?}"
                    )));
                }
                text(&self.tag_neighbours(kind, tag)?)
            }
            Call::Capabilities => text(&self.capabilities()),
            Call::Search {
                pool,
                phrase,
                negatives,
                weight,
                temperature,
                draw,
                seed,
            } => {
                let pool: Vec<&str> = pool.iter().map(String::as_str).collect();
                let negatives: Vec<&str> = negatives.iter().map(String::as_str).collect();
                let q = Search {
                    pool: &pool,
                    phrase,
                    negatives: &negatives,
                    weight: *weight,
                    temperature: *temperature,
                };
                text(&self.search_drawn(&q, *draw, *seed)?)
            }
        }
    }

    fn known_account(&self, account: i64) -> Result<(), ReaderError> {
        if self.account_exists(account)? {
            Ok(())
        } else {
            Err(ReaderError::NotFound(format!(
                "the store has no plays by account {account}"
            )))
        }
    }

    fn known_keyword(&self, keyword: &str) -> Result<crate::Keyword, ReaderError> {
        self.keyword(keyword)?.ok_or_else(|| {
            ReaderError::NotFound(format!(
                "the store records no spelling or name for the keyword {keyword:?}"
            ))
        })
    }
}

fn no_item(item_id: &str) -> ReaderError {
    ReaderError::NotFound(format!("the store holds no item {item_id}"))
}
