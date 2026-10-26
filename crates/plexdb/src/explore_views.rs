//! What Plex DB-EX's read-only keyword routes answer with: the tag table and one tag's
//! co-tags, the titles carrying a tag, the Title map, the Tag network and one title's card
//! (`plexdb/explore.py` `build_index`, `cotags_json`, `titles_tagged`, `stored_map`,
//! `title_map`, `tag_network`, `title_details`). Every function here reads a store
//! connection and returns a value; the server in [`crate::explore`] owns the caching, the
//! slots and the HTTP.
//!
//! Every keyword view reads `keyword_rows`, which folds a merged keyword into the one it
//! merges with, so a merge shows the same way in all of them.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use plexdb_keywords::normalize_keyword;
use rusqlite::Connection;

use crate::decisions::{self, py_repr};
use crate::keyword_views::{KINDS, keyword_rows, merge_map};
use crate::name_decisions::SPEC as NAME_SPEC;
use crate::pyjson::Json;
use crate::refresh_map::{Label, SEED, draw_excluding, map_fingerprint, round};
use crate::refresh_tagnetwork::{Network, layout, network_fingerprint, prepare};

/// How many related tags each table row names.
const CO_TAGS: usize = 5;

/// The evidence the table's related-tag chips demand, as a z in [`cautious_lift`].
const CO_STRENGTH: f64 = 2.0;

/// How many similar titles one card lists, and the longest item id a lookup takes.
const SIMILAR_LIMIT: usize = 12;
pub const MAX_ITEM_ID: usize = 200;

/// Why a route could not answer; the server turns each into its status.
#[derive(Debug)]
pub enum ExploreError {
    /// 400: the request is wrong.
    Invalid(String),
    /// 404 with this message.
    NotFound(String),
    /// 429: more of this request are running than the route allows.
    Busy(String),
    /// 503: the store cannot be read.
    Unavailable(String),
    /// 500: a file the explorer owns is damaged.
    Internal(String),
}

impl From<rusqlite::Error> for ExploreError {
    fn from(err: rusqlite::Error) -> Self {
        ExploreError::Unavailable(format!("could not read the store: {err}"))
    }
}

impl From<plexdb_store::StoreError> for ExploreError {
    fn from(err: plexdb_store::StoreError) -> Self {
        ExploreError::Unavailable(err.to_string())
    }
}

impl From<decisions::DecisionsFileError> for ExploreError {
    fn from(err: decisions::DecisionsFileError) -> Self {
        ExploreError::Internal(format!("decisions file: {err}"))
    }
}

pub fn check_kind(kind: &str) -> Result<(), ExploreError> {
    if KINDS.contains(&kind) {
        Ok(())
    } else {
        Err(ExploreError::Invalid(format!(
            "kind must be one of {}, not {}",
            KINDS.join(", "),
            py_repr(kind)
        )))
    }
}

fn year(value: Option<i64>) -> Json {
    value.map_or(Json::Null, Json::Int)
}

// ---------------------------------------------------------------------------------------
// The tag table

/// `keyword_rows` both ways round, with every keyword and title as an index.
pub struct KeywordSets {
    pub tags: Vec<String>,
    by_tag: Vec<Vec<u32>>,
    by_title: Vec<Vec<u32>>,
}

impl KeywordSets {
    /// `rows` ordered by title, each pair once (what `keyword_rows` returns).
    pub fn new(rows: &[(String, String)]) -> Self {
        let mut vocab: HashMap<&str, u32> = HashMap::new();
        let mut tags: Vec<&str> = Vec::new();
        let mut by_tag: Vec<Vec<u32>> = Vec::new();
        let mut by_title: Vec<Vec<u32>> = Vec::new();
        let mut last: Option<&str> = None;
        for (item_id, value) in rows {
            if last != Some(item_id.as_str()) {
                by_title.push(Vec::new());
                last = Some(item_id);
            }
            let title = (by_title.len() - 1) as u32;
            let tag = *vocab.entry(value).or_insert_with(|| {
                tags.push(value);
                by_tag.push(Vec::new());
                (tags.len() - 1) as u32
            });
            by_tag[tag as usize].push(title);
            by_title[title as usize].push(tag);
        }
        KeywordSets {
            tags: tags.into_iter().map(str::to_string).collect(),
            by_tag,
            by_title,
        }
    }

    /// N: titles carrying at least one keyword.
    pub fn titles(&self) -> usize {
        self.by_title.len()
    }

    fn df(&self, tag: usize) -> usize {
        self.by_tag[tag].len()
    }

    /// Every keyword sharing a title with `tag`, with the number of titles the two share.
    fn together(&self, tag: usize, counts: &mut [u32]) -> Vec<(u32, u32)> {
        let mut touched = Vec::new();
        for &title in &self.by_tag[tag] {
            for &other in &self.by_title[title as usize] {
                if other as usize == tag {
                    continue;
                }
                if counts[other as usize] == 0 {
                    touched.push(other);
                }
                counts[other as usize] += 1;
            }
        }
        touched
            .into_iter()
            .map(|other| (other, std::mem::take(&mut counts[other as usize])))
            .collect()
    }
}

/// The lift we can be confident of with `z` standard errors of evidence: the share of the
/// anchor's titles carrying `other` taken at the Wilson lower bound instead of
/// `shared / df_anchor`, over `other`'s share of all titles (`explore.cautious_lift`).
pub fn cautious_lift(
    shared: usize,
    titles: usize,
    df_anchor: usize,
    df_other: usize,
    z: f64,
) -> f64 {
    let (shared, titles, df_anchor, df_other) = (
        shared as f64,
        titles as f64,
        df_anchor as f64,
        df_other as f64,
    );
    let p = shared / df_anchor;
    let z2 = z * z;
    let centre = p + z2 / (2.0 * df_anchor);
    let margin = z * (p * (1.0 - p) / df_anchor + z2 / (4.0 * df_anchor * df_anchor)).sqrt();
    let lower = (centre - margin) / (1.0 + z2 / df_anchor);
    lower * titles / df_other
}

/// The index of one kind as `/api/tags` sends it, plus the sets `/api/cotags` counts from.
pub struct TagIndex {
    pub json: Json,
    pub sets: KeywordSets,
    /// `df` of every tag, for the co-tags route.
    df: HashMap<String, usize>,
}

pub fn build_index(kind: &str, rows: &[(String, String)], stamp: &str) -> TagIndex {
    let sets = KeywordSets::new(rows);
    let n = sets.titles();
    let mut order: Vec<usize> = (0..sets.tags.len()).collect();
    order.sort_by(|&a, &b| {
        sets.df(b)
            .cmp(&sets.df(a))
            .then_with(|| sets.tags[a].cmp(&sets.tags[b]))
    });
    let mut counts = vec![0u32; sets.tags.len()];
    let mut tags = Vec::with_capacity(order.len());
    for tag in order {
        let df = sets.df(tag);
        let mut ranked: Vec<(usize, usize, f64)> = sets
            .together(tag, &mut counts)
            .into_iter()
            .map(|(other, shared)| {
                let other = other as usize;
                let lift = cautious_lift(shared as usize, n, df, sets.df(other), CO_STRENGTH);
                (other, shared as usize, lift)
            })
            .filter(|r| r.2 > 1.0)
            .collect();
        ranked.sort_by(|a, b| {
            b.2.total_cmp(&a.2)
                .then_with(|| b.1.cmp(&a.1))
                .then_with(|| sets.tags[a.0].cmp(&sets.tags[b.0]))
        });
        let related = ranked
            .into_iter()
            .take(CO_TAGS)
            .map(|(other, shared, lift)| {
                Json::Array(vec![
                    Json::str(sets.tags[other].clone()),
                    Json::Int(shared as i64),
                    Json::Float(round(lift, 3)),
                ])
            })
            .collect();
        tags.push(Json::object([
            ("value", Json::str(sets.tags[tag].clone())),
            ("df", Json::Int(df as i64)),
            (
                "idf",
                Json::Float(round(1.0 + (n as f64 / df as f64).ln(), 4)),
            ),
            ("related", Json::Array(related)),
        ]));
    }
    let df = sets
        .tags
        .iter()
        .enumerate()
        .map(|(i, tag)| (tag.clone(), sets.df(i)))
        .collect();
    TagIndex {
        json: Json::object([
            ("kind", Json::str(kind)),
            ("titles", Json::Int(n as i64)),
            ("tags", Json::Array(tags)),
            ("stamp", Json::str(stamp)),
        ]),
        sets,
        df,
    }
}

/// `/api/cotags`: every tag sharing a title with `value`, most shared first, each as
/// `[tag, shared, df]`. Nothing is filtered or ranked: the page's slider sets the strength.
pub fn cotags_json(kind: &str, index: &TagIndex, value: &str, stamp: &str) -> Json {
    let anchor = index.df.get(value).copied().unwrap_or(0);
    let mut co = Vec::new();
    if anchor > 0 {
        let sets = &index.sets;
        let tag = sets
            .tags
            .iter()
            .position(|t| t == value)
            .expect("a tag with a df is in the sets");
        let mut counts = vec![0u32; sets.tags.len()];
        let mut pairs = sets.together(tag, &mut counts);
        pairs.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| sets.tags[a.0 as usize].cmp(&sets.tags[b.0 as usize]))
        });
        co = pairs
            .into_iter()
            .map(|(other, shared)| {
                let name = &sets.tags[other as usize];
                Json::Array(vec![
                    Json::str(name.clone()),
                    Json::Int(shared as i64),
                    Json::Int(index.df[name] as i64),
                ])
            })
            .collect();
    }
    Json::object([
        ("kind", Json::str(kind)),
        ("tag", Json::str(value)),
        ("df", Json::Int(anchor as i64)),
        ("titles", Json::Int(index.sets.titles() as i64)),
        ("co", Json::Array(co)),
        ("stamp", Json::str(stamp)),
    ])
}

/// `/api/titles`: every title of `kind` carrying `value`, or a keyword merged into it, by
/// title; each title's keyword count counts a merged group once.
pub fn titles_json(conn: &Connection, kind: &str, value: &str) -> Result<Json, ExploreError> {
    check_kind(kind)?;
    let into = merge_map(conn)?;
    let mut group = vec![value.to_string()];
    group.extend(
        into.iter()
            .filter(|(_, target)| target.as_str() == value)
            .map(|(keyword, _)| keyword.clone()),
    );
    let marks = vec!["?"; group.len()].join(",");
    let mut statement = conn.prepare(&format!(
        "SELECT DISTINCT i.item_id, i.title, i.year FROM enrichment e JOIN items i USING (item_id) \
         WHERE e.namespace = ? AND e.key = ? AND e.value IN ({marks}) AND i.type = ? \
         ORDER BY COALESCE(i.title_sort, i.title) COLLATE NOCASE, i.year"
    ))?;
    let mut params: Vec<&str> = vec!["keywords", "keyword"];
    params.extend(group.iter().map(String::as_str));
    params.push(kind);
    let found: Vec<(String, String, Option<i64>)> = statement
        .query_map(rusqlite::params_from_iter(params), |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?
        .collect::<Result<_, _>>()?;
    let mut carried: HashMap<String, HashSet<String>> = HashMap::new();
    for chunk in found.chunks(500) {
        let marks = vec!["?"; chunk.len()].join(",");
        let mut statement = conn.prepare(&format!(
            "SELECT DISTINCT item_id, value FROM enrichment WHERE namespace = ? AND key = ? \
             AND item_id IN ({marks})"
        ))?;
        let mut params: Vec<&str> = vec!["keywords", "keyword"];
        params.extend(chunk.iter().map(|r| r.0.as_str()));
        let rows = statement.query_map(rusqlite::params_from_iter(params), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (item_id, keyword) = row?;
            let keyword = into.get(&keyword).cloned().unwrap_or(keyword);
            carried.entry(item_id).or_default().insert(keyword);
        }
    }
    let titles = found
        .into_iter()
        .map(|(item_id, title, y)| {
            let keywords = carried.get(&item_id).map_or(0, HashSet::len);
            Json::object([
                ("item_id", Json::str(item_id)),
                ("title", Json::str(title)),
                ("year", year(y)),
                ("keywords", Json::Int(keywords as i64)),
            ])
        })
        .collect();
    Ok(Json::object([
        ("kind", Json::str(kind)),
        ("tag", Json::str(value)),
        ("titles", Json::Array(titles)),
    ]))
}

// ---------------------------------------------------------------------------------------
// The Title map

pub struct MapPoint {
    pub item_id: String,
    pub title: String,
    pub year: Option<i64>,
    pub x: f64,
    pub y: f64,
}

/// A map as `/api/map` sends it, before the display labels are laid over it.
pub struct MapPayload {
    pub kind: String,
    pub unplaced: i64,
    pub stale: bool,
    pub points: Vec<MapPoint>,
    pub labels: Vec<Label>,
    /// A stored map keeps `stale` before its points; a drawn one keeps it after.
    stored: bool,
}

/// The default map the writer stored, with `stale` set when it was drawn from other keyword
/// rows; `None` when there is none, or it is stale and `stale_ok` is false.
pub fn stored_map(
    conn: &Connection,
    kind: &str,
    stale_ok: bool,
) -> Result<Option<MapPayload>, ExploreError> {
    check_kind(kind)?;
    conn.execute_batch("BEGIN")?;
    let read = read_stored_map(conn, kind, stale_ok);
    let _ = conn.execute_batch("COMMIT");
    match read {
        Ok(payload) => Ok(payload),
        Err(rusqlite::Error::SqliteFailure(..)) => Ok(None),
        Err(other) => Err(other.into()),
    }
}

fn read_stored_map(
    conn: &Connection,
    kind: &str,
    stale_ok: bool,
) -> rusqlite::Result<Option<MapPayload>> {
    let state: Option<(String, i64)> = optional(conn.query_row(
        "SELECT fingerprint, unplaced FROM title_map_state WHERE kind = ?",
        [kind],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ))?;
    let Some((fingerprint, unplaced)) = state else {
        return Ok(None);
    };
    let stale = fingerprint != map_fingerprint(conn, kind)?;
    if stale && !stale_ok {
        return Ok(None);
    }
    let points = conn
        .prepare(
            "SELECT m.item_id, i.title, i.year, m.x, m.y FROM title_map m \
             JOIN items i USING (item_id) WHERE m.kind = ? ORDER BY m.item_id",
        )?
        .query_map([kind], |row| {
            Ok(MapPoint {
                item_id: row.get(0)?,
                title: row.get(1)?,
                year: row.get(2)?,
                x: row.get(3)?,
                y: row.get(4)?,
            })
        })?
        .collect::<Result<_, _>>()?;
    let labels = conn
        .prepare(
            "SELECT node, x, y, width, height, titles, value FROM title_map_label \
             WHERE kind = ? ORDER BY node",
        )?
        .query_map([kind], |row| {
            Ok(Label {
                node: row.get(0)?,
                x: row.get(1)?,
                y: row.get(2)?,
                width: row.get(3)?,
                height: row.get(4)?,
                titles: row.get(5)?,
                tag: row.get(6)?,
            })
        })?
        .collect::<Result<_, _>>()?;
    Ok(Some(MapPayload {
        kind: kind.to_string(),
        unplaced,
        stale,
        points,
        labels,
        stored: true,
    }))
}

fn optional<T>(result: rusqlite::Result<T>) -> rusqlite::Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(other) => Err(other),
    }
}

/// Every title of `kind` placed live, with the tags in `exclude` left out of the vectors.
pub fn draw_map(
    conn: &Connection,
    kind: &str,
    exclude: &HashSet<String>,
) -> Result<MapPayload, ExploreError> {
    check_kind(kind)?;
    let folded = merge_map(conn)?;
    let rows = keyword_rows(conn, kind, &folded)?;
    let drawn = draw_excluding(&rows, exclude, SEED).map_err(ExploreError::Internal)?;
    let mut about: HashMap<String, (String, Option<i64>)> = HashMap::new();
    let mut statement = conn.prepare("SELECT item_id, title, year FROM items WHERE type = ?")?;
    for row in statement.query_map([kind], |row| {
        Ok((row.get::<_, String>(0)?, row.get(1)?, row.get(2)?))
    })? {
        let (item_id, title, y) = row?;
        about.insert(item_id, (title, y));
    }
    let points = drawn
        .points
        .into_iter()
        .map(|p| {
            let (title, y) = about.get(&p.item_id).cloned().unwrap_or_default();
            MapPoint {
                item_id: p.item_id,
                title,
                year: y,
                x: p.x,
                y: p.y,
            }
        })
        .collect();
    Ok(MapPayload {
        kind: kind.to_string(),
        unplaced: drawn.unplaced as i64,
        stale: false,
        points,
        labels: drawn.labels,
        stored: false,
    })
}

/// `payload` as `/api/map` sends it, each region label's text taken from `labels`.
pub fn map_json(payload: &MapPayload, labels: &HashMap<String, String>) -> Json {
    let points = Json::Array(
        payload
            .points
            .iter()
            .map(|p| {
                Json::Array(vec![
                    Json::str(p.item_id.clone()),
                    Json::str(p.title.clone()),
                    year(p.year),
                    Json::Float(p.x),
                    Json::Float(p.y),
                ])
            })
            .collect(),
    );
    let regions = Json::Array(
        payload
            .labels
            .iter()
            .map(|l| {
                Json::Array(vec![
                    Json::Int(l.node),
                    Json::Float(l.x),
                    Json::Float(l.y),
                    Json::Float(l.width),
                    Json::Float(l.height),
                    Json::Int(l.titles),
                    Json::str(labels.get(&l.tag).unwrap_or(&l.tag).clone()),
                    Json::str(l.tag.clone()),
                ])
            })
            .collect(),
    );
    let kind = ("kind", Json::str(payload.kind.clone()));
    let unplaced = ("unplaced", Json::Int(payload.unplaced));
    let stale = ("stale", Json::Bool(payload.stale));
    let points = ("points", points);
    let regions = ("labels", regions);
    if payload.stored {
        Json::object([kind, unplaced, stale, points, regions])
    } else {
        Json::object([kind, unplaced, points, stale, regions])
    }
}

// ---------------------------------------------------------------------------------------
// The Tag network

/// The default network the writer stored, or `None` when there is none or it was drawn from
/// other keyword rows.
pub fn stored_network(conn: &Connection, kind: &str) -> Result<Option<Network>, ExploreError> {
    check_kind(kind)?;
    match read_stored_network(conn, kind) {
        Ok(network) => Ok(network),
        Err(rusqlite::Error::SqliteFailure(..)) => Ok(None),
        Err(other) => Err(other.into()),
    }
}

fn read_stored_network(conn: &Connection, kind: &str) -> rusqlite::Result<Option<Network>> {
    let state: Option<String> = optional(conn.query_row(
        "SELECT fingerprint FROM tag_network_state WHERE kind = ?",
        [kind],
        |row| row.get(0),
    ))?;
    match state {
        Some(fingerprint) if fingerprint == network_fingerprint(conn, kind)? => {}
        _ => return Ok(None),
    }
    let nodes = conn
        .prepare("SELECT value, df, x, y FROM tag_network WHERE kind = ? ORDER BY value")?
        .query_map([kind], |row| {
            Ok(crate::refresh_tagnetwork::Node {
                value: row.get(0)?,
                df: row.get::<_, i64>(1)? as usize,
                x: row.get(2)?,
                y: row.get(3)?,
            })
        })?
        .collect::<Result<_, _>>()?;
    let edges = conn
        .prepare(
            "SELECT a, b, shared FROM tag_network_edge WHERE kind = ? \
             ORDER BY shared DESC, a, b",
        )?
        .query_map([kind], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get::<_, i64>(2)? as usize))
        })?
        .collect::<Result<_, _>>()?;
    Ok(Some(Network { nodes, edges }))
}

pub fn edges_json(network_edges: &[(String, String, usize)]) -> Json {
    Json::Array(
        network_edges
            .iter()
            .map(|(a, b, n)| {
                Json::Array(vec![
                    Json::str(a.clone()),
                    Json::str(b.clone()),
                    Json::Int(*n as i64),
                ])
            })
            .collect(),
    )
}

/// The last line of `/api/tagnetwork`: the laid-out network and the store stamp.
pub fn network_done_json(kind: &str, network: &Network, stamp: &str) -> Json {
    let nodes = network
        .nodes
        .iter()
        .map(|n| {
            Json::Array(vec![
                Json::str(n.value.clone()),
                Json::Int(n.df as i64),
                Json::Float(n.x),
                Json::Float(n.y),
            ])
        })
        .collect();
    Json::object([
        ("stage", Json::str("done")),
        ("kind", Json::str(kind)),
        ("nodes", Json::Array(nodes)),
        ("edges", edges_json(&network.edges)),
        ("stamp", Json::str(stamp)),
    ])
}

/// Draw the network with `exclude` left out, handing `emit` the vocabulary line and then the
/// edges line as each is ready, and returning the laid-out network (the slow stage).
pub fn draw_network(
    conn: &Connection,
    kind: &str,
    exclude: &HashSet<String>,
    emit: &mut dyn FnMut(Json),
) -> Result<Network, ExploreError> {
    check_kind(kind)?;
    let folded = merge_map(conn)?;
    let rows = keyword_rows(conn, kind, &folded)?;
    let prepared = prepare(&rows, exclude);
    let nodes = prepared
        .values
        .iter()
        .zip(&prepared.df)
        .map(|(value, df)| Json::Array(vec![Json::str(value.clone()), Json::Int(*df as i64)]))
        .collect();
    emit(Json::object([
        ("stage", Json::str("vocab")),
        ("nodes", Json::Array(nodes)),
    ]));
    emit(Json::object([
        ("stage", Json::str("edges")),
        ("edges", edges_json(&prepared.edges)),
    ]));
    layout(prepared, SEED).map_err(ExploreError::Internal)
}

// ---------------------------------------------------------------------------------------
// Display names

/// The names a person gave keywords in `name_decisions.json`, by stored keyword: the name
/// (`None` where the file clears it) and when it was decided. The last entry for a keyword
/// wins; an entry whose spelling normalizes to nothing names no keyword.
pub struct Overlay(HashMap<String, (Option<String>, String)>);

impl Overlay {
    pub fn read(path: &Path) -> Result<Self, ExploreError> {
        let mut latest = HashMap::new();
        for entry in decisions::load(&NAME_SPEC, path)?.unwrap_or_default() {
            let keyword = normalize_keyword(&entry.key[0]);
            if keyword.is_empty() {
                continue;
            }
            let name = (entry.decision != "cleared").then(|| entry.values[0].clone());
            latest.insert(keyword, (name, entry.decided_at));
        }
        Ok(Overlay(latest))
    }
}

/// For each keyword with a `keyword_forms` row, the spelling a person reads: the shortest
/// surface, ties by text.
fn readable_forms(conn: &Connection) -> rusqlite::Result<HashMap<String, String>> {
    let mut chosen: HashMap<String, String> = HashMap::new();
    let mut statement = conn.prepare("SELECT surface, keyword FROM keyword_forms")?;
    for row in statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })? {
        let (surface, keyword) = row?;
        let better = chosen
            .get(&keyword)
            .is_none_or(|best| (surface.chars().count(), &surface) < (best.chars().count(), best));
        if better {
            chosen.insert(keyword, surface);
        }
    }
    Ok(chosen)
}

/// Each keyword with a name in the store or in the overlay, with its decision time; a name
/// the file clears is absent.
fn named(
    conn: &Connection,
    overlay: &Overlay,
) -> rusqlite::Result<HashMap<String, (String, String)>> {
    let mut out: HashMap<String, (Option<String>, String)> = HashMap::new();
    let mut stored: HashMap<String, String> = HashMap::new();
    let mut statement = conn.prepare("SELECT keyword, name, decided_at FROM keyword_names")?;
    for row in statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })? {
        let (keyword, name, decided_at) = row?;
        stored.insert(keyword.clone(), name.clone());
        out.insert(keyword, (Some(name), decided_at));
    }
    for (keyword, (name, decided_at)) in &overlay.0 {
        if name.is_none() && !stored.contains_key(keyword) {
            continue;
        }
        out.insert(keyword.clone(), (name.clone(), decided_at.clone()));
    }
    Ok(out
        .into_iter()
        .filter_map(|(keyword, (name, at))| name.map(|name| (keyword, (name, at))))
        .collect())
}

/// For each keyword with a `keyword_forms` row or a name, the text to show a person: its own
/// name, else its readable spelling; and a merge group whose head has no name shows the newest
/// name any of its members has (`names.display_labels`). A keyword absent here shows as its
/// stored form.
pub fn display_labels(
    conn: &Connection,
    overlay: &Overlay,
    folded: &HashMap<String, String>,
) -> rusqlite::Result<HashMap<String, String>> {
    let mut labels = readable_forms(conn)?;
    let names = named(conn, overlay)?;
    labels.extend(names.iter().map(|(k, (name, _))| (k.clone(), name.clone())));
    if names.is_empty() {
        return Ok(labels);
    }
    let mut newest: HashMap<&str, &(String, String)> = HashMap::new();
    for (member, head) in folded {
        let Some(held) = names.get(member) else {
            continue;
        };
        if names.contains_key(head) {
            continue;
        }
        match newest.get(head.as_str()) {
            Some(current) if held.1 <= current.1 => {}
            _ => {
                newest.insert(head, held);
            }
        }
    }
    labels.extend(
        newest
            .into_iter()
            .map(|(head, v)| (head.to_string(), v.0.clone())),
    );
    Ok(labels)
}

// ---------------------------------------------------------------------------------------
// A title's card

/// Every title of one kind as an IDF-weighted, unit-length keyword vector, so a title's
/// nearest titles are one sparse product away (`explore.KeywordSpace`).
pub struct KeywordSpace {
    row: HashMap<String, usize>,
    ids: Vec<String>,
    /// Each title's `(column, weight)` pairs.
    vectors: Vec<Vec<(u32, f64)>>,
    /// Each column's `(title, weight)` pairs.
    columns: Vec<Vec<(u32, f64)>>,
}

impl KeywordSpace {
    pub fn new(rows: &[(String, String)]) -> Self {
        let sets = KeywordSets::new(rows);
        let n = sets.titles();
        let mut ids: Vec<String> = Vec::with_capacity(n);
        let mut last: Option<&str> = None;
        for (item_id, _) in rows {
            if last != Some(item_id.as_str()) {
                ids.push(item_id.clone());
                last = Some(item_id);
            }
        }
        let weight: Vec<f64> = (0..sets.tags.len())
            .map(|tag| 1.0 + (n as f64 / sets.df(tag) as f64).ln())
            .collect();
        let mut columns: Vec<Vec<(u32, f64)>> = vec![Vec::new(); sets.tags.len()];
        let vectors = sets
            .by_title
            .iter()
            .enumerate()
            .map(|(title, tags)| {
                let norm = tags
                    .iter()
                    .map(|&t| weight[t as usize] * weight[t as usize])
                    .sum::<f64>()
                    .sqrt();
                tags.iter()
                    .map(|&t| {
                        let w = weight[t as usize] / norm;
                        columns[t as usize].push((title as u32, w));
                        (t, w)
                    })
                    .collect()
            })
            .collect();
        KeywordSpace {
            row: ids
                .iter()
                .cloned()
                .enumerate()
                .map(|(i, id)| (id, i))
                .collect(),
            ids,
            vectors,
            columns,
        }
    }

    /// The `limit` titles with the highest keyword cosine to `item_id`, highest first (ties by
    /// item id), each with that cosine rounded to four places; none share no keyword with it.
    pub fn nearest(&self, item_id: &str, limit: usize) -> Vec<(String, f64)> {
        let Some(&row) = self.row.get(item_id) else {
            return Vec::new();
        };
        let mut scores: HashMap<u32, f64> = HashMap::new();
        for &(column, weight) in &self.vectors[row] {
            for &(title, other) in &self.columns[column as usize] {
                *scores.entry(title).or_default() += weight * other;
            }
        }
        scores.remove(&(row as u32));
        let mut ranked: Vec<(u32, f64)> = scores.into_iter().filter(|(_, s)| *s > 0.0).collect();
        ranked.sort_by(|a, b| {
            b.1.total_cmp(&a.1)
                .then_with(|| self.ids[a.0 as usize].cmp(&self.ids[b.0 as usize]))
        });
        ranked
            .into_iter()
            .take(limit)
            .map(|(title, score)| (self.ids[title as usize].clone(), round(score, 4)))
            .collect()
    }
}

/// The kind of the title `item_id`, when it has one the explorer lays out.
pub fn title_kind(conn: &Connection, item_id: &str) -> rusqlite::Result<Option<String>> {
    let kind: Option<Option<String>> = optional(conn.query_row(
        "SELECT type FROM items WHERE item_id = ?",
        [item_id],
        |row| row.get(0),
    ))?;
    Ok(kind.flatten().filter(|k| KINDS.contains(&k.as_str())))
}

/// An item's `(title, year, studio, content rating)`.
type ItemFacts = (String, Option<i64>, Option<String>, Option<String>);

/// What a title's card shows: its facts, its keywords labelled by `labels`, and its nearest
/// titles in `space` (its own kind's); none without a space.
pub fn title_details(
    conn: &Connection,
    item_id: &str,
    labels: &HashMap<String, String>,
    folded: &HashMap<String, String>,
    space: Option<&KeywordSpace>,
) -> Result<Json, ExploreError> {
    let row: Option<ItemFacts> = optional(conn.query_row(
        "SELECT title, year, studio, content_rating FROM items WHERE item_id = ?",
        [item_id],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
    ))?;
    let Some((title, y, studio, content_rating)) = row else {
        return Err(ExploreError::NotFound(format!(
            "no title with item_id {}",
            py_repr(item_id)
        )));
    };
    let nearest = space.map_or_else(Vec::new, |s| s.nearest(item_id, SIMILAR_LIMIT));
    let mut about: HashMap<String, (String, Option<i64>)> = HashMap::new();
    for (other, _) in &nearest {
        let found: Option<(String, Option<i64>)> = optional(conn.query_row(
            "SELECT title, year FROM items WHERE item_id = ?",
            [other],
            |row| Ok((row.get(0)?, row.get(1)?)),
        ))?;
        if let Some(found) = found {
            about.insert(other.clone(), found);
        }
    }
    let mut keywords: Vec<String> = conn
        .prepare(
            "SELECT DISTINCT value FROM enrichment WHERE item_id = ? AND namespace = ? AND key = ?",
        )?
        .query_map(rusqlite::params![item_id, "keywords", "keyword"], |row| {
            row.get::<_, String>(0)
        })?
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(|v| folded.get(&v).cloned().unwrap_or(v))
        .collect();
    keywords.sort();
    keywords.dedup();
    let text = |value: Option<String>| value.map_or(Json::Null, Json::Str);
    Ok(Json::object([
        ("item_id", Json::str(item_id)),
        ("title", Json::str(title)),
        ("year", year(y)),
        ("studio", text(studio)),
        ("content_rating", text(content_rating)),
        (
            "keywords",
            Json::Array(
                keywords
                    .into_iter()
                    .map(|value| {
                        let label = labels.get(&value).unwrap_or(&value).clone();
                        Json::object([("value", Json::Str(value)), ("label", Json::Str(label))])
                    })
                    .collect(),
            ),
        ),
        (
            "similar",
            Json::Array(
                nearest
                    .into_iter()
                    .map(|(other, score)| {
                        let (title, y) = about.get(&other).cloned().unwrap_or_default();
                        Json::object([
                            ("item_id", Json::Str(other)),
                            ("title", Json::Str(title)),
                            ("year", year(y)),
                            ("score", Json::Float(score)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ]))
}
