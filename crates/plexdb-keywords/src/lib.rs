//! Normalizing a keyword surface into the form the store keeps, and writing a
//! title's keywords (`plexdb/keywords.py`, ADR-0016).
//!
//! One namespace, `keywords`, holds every source's keyword facts; `source` in the
//! primary key tells them apart. [`normalize_keyword`] makes `Heists`,
//! `bank-heist` and `bank heist` one stored form: lowercase, whitespace, dashes
//! and `_` as word breaks, the punctuation at each word's edges stripped, then
//! each plural folded to its singular by simplemma's English lemma, for a word
//! ending in "s" only. [`write_title_keywords`] keeps every raw spelling in
//! `keyword_surfaces` and derives the `enrichment` rows from them, so a change
//! to normalization re-derives the store ([`rederive_keywords`]) instead of
//! fetching every title again.
//!
//! The rule is published, so `tests/fixtures/keyword_normalization.json` is its
//! specification: Python's output for every raw spelling the store held and every
//! English dictionary word ending in "s". The code and that file move together.

use std::collections::{BTreeMap, HashMap};

use rusqlite::{Connection, params};
use unicode_general_category::{GeneralCategory, get_general_category};

pub mod simplemma;

/// The source-agnostic namespace every keyword source writes to (ADR-0016).
pub const NAMESPACE: &str = "keywords";
/// The `enrichment.key` of an ordinary keyword, and of one its source flags as a
/// spoiler (AniList).
pub const KEYWORD_KEY: &str = "keyword";
pub const SPOILER_KEY: &str = "spoiler_keyword";

/// Words ending in "s" that are not plurals of the word simplemma would fold
/// them to: proper nouns, plurals whose meaning differs from the singular, and
/// abbreviations.
pub const NEVER_FOLD: [&str; 16] = [
    "aids",
    "alps",
    "arms",
    "cruces",
    "customs",
    "falls",
    "interspecies",
    "it's",
    "las",
    "mars",
    "ops",
    "ss",
    "states",
    "vis",
    "wales",
    "woods",
];

/// Python's `str.isspace`: Unicode `White_Space`, plus the four ASCII separators
/// (U+001C to U+001F) Python also counts.
pub fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.strip()` with no argument.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(is_py_space)
}

/// `word`'s singular when it is a plural simplemma knows, else `word`.
///
/// Only a word ending in "s" is looked up, and only a strictly shorter lemma is
/// taken: simplemma also maps verb forms to their base (`racing` → `race`,
/// `opera` → `opus`), which would merge tags that mean different things.
fn fold_plural(word: &str) -> String {
    if !word.ends_with('s') || NEVER_FOLD.contains(&word) {
        return word.to_string();
    }
    let lemma = simplemma::lemmatize(word).to_lowercase();
    if lemma.chars().count() < word.chars().count() {
        lemma
    } else {
        word.to_string()
    }
}

/// Punctuation that is part of the word it ends or starts — `c#`, `100%` — and so
/// is never stripped from a word's edge.
fn is_punctuation(c: char) -> bool {
    use GeneralCategory::*;
    !matches!(c, '#' | '%')
        && matches!(
            get_general_category(c),
            ConnectorPunctuation
                | DashPunctuation
                | OpenPunctuation
                | ClosePunctuation
                | InitialPunctuation
                | FinalPunctuation
                | OtherPunctuation
        )
}

fn is_word_break(c: char) -> bool {
    use GeneralCategory::*;
    is_py_space(c)
        || matches!(
            get_general_category(c),
            DashPunctuation | ConnectorPunctuation
        )
}

fn is_mark(c: char) -> bool {
    use GeneralCategory::*;
    matches!(
        get_general_category(c),
        NonspacingMark | SpacingMark | EnclosingMark
    )
}

/// A cluster is a character with the combining marks that follow it; a mark with
/// nothing before it is a cluster of its own. The edge test reads the first
/// character, so this returns each cluster's first character and its length.
fn is_edge_debris(c: char) -> bool {
    is_punctuation(c) || get_general_category(c) == GeneralCategory::Format || is_mark(c)
}

fn strip_edge_punctuation(word: &str) -> &str {
    // (byte offset, first character) of every cluster.
    let mut clusters: Vec<(usize, char)> = Vec::new();
    for (offset, c) in word.char_indices() {
        if clusters.is_empty() || !is_mark(c) {
            clusters.push((offset, c));
        }
    }
    let mut start = 0;
    let mut end = clusters.len();
    while start < end && is_edge_debris(clusters[start].1) {
        start += 1;
    }
    while end > start && is_edge_debris(clusters[end - 1].1) {
        end -= 1;
    }
    if start == end {
        return "";
    }
    let from = clusters[start].0;
    let to = clusters.get(end).map_or(word.len(), |(offset, _)| *offset);
    &word[from..to]
}

/// The stored form of one raw keyword spelling (`keywords.normalize_keyword`).
///
/// Lowercase and turn `’` into `'`, then whitespace, every dash and `_` break
/// words, each word loses the punctuation at its edges together with any
/// invisible format character and any combining mark on that punctuation or with
/// no character before it, and each word left has its plural folded. A surface
/// with no word left normalizes to `""`.
pub fn normalize_keyword(surface: &str) -> String {
    let text: String = surface
        .to_lowercase()
        .replace('\u{2019}', "'")
        .chars()
        .map(|c| if is_word_break(c) { ' ' } else { c })
        .collect();
    let words: Vec<String> = text
        .split(is_py_space)
        .filter(|word| !word.is_empty())
        .map(strip_edge_punctuation)
        .filter(|word| !word.is_empty())
        .map(fold_plural)
        .collect();
    words.join(" ")
}

/// Normalize `surface`, record the mapping in `keyword_forms`, and return the
/// stored form to write into `enrichment`. A surface that normalizes to `""`
/// records nothing.
pub fn upsert_keyword_form(conn: &Connection, surface: &str) -> rusqlite::Result<String> {
    let keyword = normalize_keyword(surface);
    if keyword.is_empty() {
        return Ok(keyword);
    }
    conn.execute(
        "INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?) \
         ON CONFLICT(surface) DO UPDATE SET keyword = excluded.keyword",
        params![surface, keyword],
    )?;
    Ok(keyword)
}

/// One keyword exactly as a source sent it for one title.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawKeyword {
    pub surface: String,
    /// The source's own rank, higher first; `None` when the source ranks nothing.
    pub rank: Option<i64>,
    /// The source flags the keyword as a spoiler (AniList).
    pub spoiler: bool,
}

impl RawKeyword {
    pub fn plain(surface: impl Into<String>) -> Self {
        RawKeyword {
            surface: surface.into(),
            rank: None,
            spoiler: false,
        }
    }
}

/// The higher of two ranks, where `None` means unranked rather than zero.
fn higher(a: Option<i64>, b: Option<i64>) -> Option<i64> {
    match (a, b) {
        (None, other) | (other, None) => other,
        (Some(a), Some(b)) => Some(a.max(b)),
    }
}

/// A derived `enrichment` row.
struct Derived {
    item_id: String,
    source: String,
    key: &'static str,
    value: String,
    fetched_at: String,
    rank: Option<i64>,
}

/// A `keyword_surfaces` row.
struct Surface {
    item_id: String,
    source: String,
    surface: String,
    rank: Option<i64>,
    spoiler: bool,
    fetched_at: String,
}

/// The `enrichment` rows `keyword_surfaces` rows derive: one per (title, source,
/// stored form), keeping the highest rank, the newest `fetched_at`, and
/// `SPOILER_KEY` when any surface that folded into it is a spoiler. Rows come out
/// in first-seen order, as the Python dict yields them.
fn derive(surfaces: Vec<Surface>, seen: &mut HashMap<String, String>) -> Vec<Derived> {
    type Held = (bool, String, Option<i64>);
    let mut order: Vec<(String, String, String)> = Vec::new();
    let mut rows: HashMap<(String, String, String), Held> = HashMap::new();
    for surface in surfaces {
        let value = seen
            .entry(surface.surface.clone())
            .or_insert_with(|| normalize_keyword(&surface.surface))
            .clone();
        if value.is_empty() {
            continue;
        }
        let key = (surface.item_id, surface.source, value);
        match rows.get_mut(&key) {
            None => {
                order.push(key.clone());
                rows.insert(key, (surface.spoiler, surface.fetched_at, surface.rank));
            }
            Some(held) => {
                held.0 = held.0 || surface.spoiler;
                if surface.fetched_at > held.1 {
                    held.1 = surface.fetched_at;
                }
                held.2 = higher(held.2, surface.rank);
            }
        }
    }
    order
        .into_iter()
        .map(|key| {
            let (spoiler, fetched_at, rank) =
                rows.remove(&key).expect("every ordered key has a row");
            Derived {
                item_id: key.0,
                source: key.1,
                key: if spoiler { SPOILER_KEY } else { KEYWORD_KEY },
                value: key.2,
                fetched_at,
                rank,
            }
        })
        .collect()
}

fn insert_derived(conn: &Connection, rows: &[Derived]) -> rusqlite::Result<()> {
    let mut insert = conn.prepare_cached(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at, rank) \
         VALUES (?, 'keywords', ?, ?, ?, ?, ?)",
    )?;
    for row in rows {
        insert.execute(params![
            row.item_id,
            row.source,
            row.key,
            row.value,
            row.fetched_at,
            row.rank
        ])?;
    }
    Ok(())
}

/// Replace `source`'s keywords on `item_id` with `raw`, and return each kept
/// surface's stored form. The caller owns the transaction.
///
/// The raw spellings go into `keyword_surfaces`, one row per distinct surface (two
/// copies of one surface keep the higher rank, and are a spoiler if either is); the
/// `enrichment` rows are then derived from them exactly as [`rederive_keywords`]
/// derives the whole store. A surface that normalizes to `""` is kept as a raw
/// fact but derives no row.
pub fn write_title_keywords(
    conn: &Connection,
    item_id: &str,
    source: &str,
    raw: impl IntoIterator<Item = RawKeyword>,
    fetched_at: &str,
) -> rusqlite::Result<BTreeMap<String, String>> {
    let mut order: Vec<String> = Vec::new();
    let mut merged: HashMap<String, RawKeyword> = HashMap::new();
    for keyword in raw {
        let surface = py_strip(&keyword.surface).to_string();
        if surface.is_empty() {
            continue;
        }
        match merged.get_mut(&surface) {
            None => {
                order.push(surface.clone());
                merged.insert(
                    surface.clone(),
                    RawKeyword {
                        surface,
                        rank: keyword.rank,
                        spoiler: keyword.spoiler,
                    },
                );
            }
            Some(held) => {
                held.rank = higher(held.rank, keyword.rank);
                held.spoiler = held.spoiler || keyword.spoiler;
            }
        }
    }
    conn.execute(
        "DELETE FROM keyword_surfaces WHERE item_id = ? AND source = ?",
        params![item_id, source],
    )?;
    let mut surfaces = Vec::with_capacity(order.len());
    {
        let mut insert = conn.prepare_cached(
            "INSERT INTO keyword_surfaces (item_id, source, surface, rank, spoiler, fetched_at) \
             VALUES (?, ?, ?, ?, ?, ?)",
        )?;
        for surface in &order {
            let k = &merged[surface];
            insert.execute(params![
                item_id,
                source,
                k.surface,
                k.rank,
                i64::from(k.spoiler),
                fetched_at
            ])?;
            surfaces.push(Surface {
                item_id: item_id.to_string(),
                source: source.to_string(),
                surface: k.surface.clone(),
                rank: k.rank,
                spoiler: k.spoiler,
                fetched_at: fetched_at.to_string(),
            });
        }
    }
    let mut stored = BTreeMap::new();
    for surface in &order {
        let keyword = upsert_keyword_form(conn, surface)?;
        if !keyword.is_empty() {
            stored.insert(surface.clone(), keyword);
        }
    }
    conn.execute(
        "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ? \
         AND key IN (?, ?)",
        params![item_id, NAMESPACE, source, KEYWORD_KEY, SPOILER_KEY],
    )?;
    let derived = derive(surfaces, &mut HashMap::new());
    insert_derived(conn, &derived)?;
    Ok(stored)
}

/// Rebuild every keyword `enrichment` row from `keyword_surfaces` with the current
/// [`normalize_keyword`], and re-point every `keyword_forms` surface at its current
/// stored form. Returns the number of keyword rows written. Nothing is fetched.
/// The caller owns the transaction.
pub fn rederive_keywords(conn: &Connection) -> rusqlite::Result<usize> {
    let mut seen: HashMap<String, String> = HashMap::new();
    conn.execute(
        "DELETE FROM enrichment WHERE namespace = ? AND key IN (?, ?)",
        params![NAMESPACE, KEYWORD_KEY, SPOILER_KEY],
    )?;
    let surfaces: Vec<Surface> = conn
        .prepare(
            "SELECT item_id, source, surface, rank, spoiler, fetched_at FROM keyword_surfaces",
        )?
        .query_map([], |row| {
            Ok(Surface {
                item_id: row.get(0)?,
                source: row.get(1)?,
                surface: row.get(2)?,
                rank: row.get(3)?,
                spoiler: row.get::<_, i64>(4)? != 0,
                fetched_at: row.get(5)?,
            })
        })?
        .collect::<rusqlite::Result<_>>()?;
    let rows = derive(surfaces, &mut seen);
    insert_derived(conn, &rows)?;
    let forms: Vec<String> = conn
        .prepare("SELECT surface FROM keyword_forms")?
        .query_map([], |row| row.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    for surface in &forms {
        seen.entry(surface.clone())
            .or_insert_with(|| normalize_keyword(surface));
    }
    conn.execute("DELETE FROM keyword_forms", [])?;
    let mut insert = conn.prepare("INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?)")?;
    for surface in &forms {
        let keyword = &seen[surface];
        if !keyword.is_empty() {
            insert.execute(params![surface, keyword])?;
        }
    }
    Ok(rows.len())
}

/// Whether some title carries `keyword`, as a keyword or a spoiler keyword.
pub fn is_carried(conn: &Connection, keyword: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM enrichment WHERE namespace = 'keywords' \
         AND key IN ('keyword', 'spoiler_keyword') AND value = ?)",
        [keyword],
        |row| row.get(0),
    )
}

/// For each keyword `keyword_forms` maps a raw spelling to, the spelling a person or a
/// judge reads for it: the shortest (by characters), ties by text. A keyword with no
/// row is absent.
pub fn readable_forms(conn: &Connection) -> rusqlite::Result<HashMap<String, String>> {
    let mut chosen: HashMap<String, String> = HashMap::new();
    let mut statement = conn.prepare("SELECT surface, keyword FROM keyword_forms")?;
    let rows = statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (surface, keyword) = row?;
        let key = (surface.chars().count(), &surface);
        match chosen.get(&keyword) {
            Some(best) if (best.chars().count(), best) <= key => {}
            _ => {
                chosen.insert(keyword, surface);
            }
        }
    }
    Ok(chosen)
}

/// Each keyword a title carries as `key = 'keyword'`, with its readable spelling
/// ([`readable_forms`]) or its own text when no `keyword_forms` row maps back to it.
pub fn readable_surfaces(conn: &Connection) -> rusqlite::Result<BTreeMap<String, String>> {
    let chosen = readable_forms(conn)?;
    let mut statement = conn
        .prepare("SELECT DISTINCT value FROM enrichment WHERE namespace = ? AND key = 'keyword'")?;
    let rows = statement.query_map([NAMESPACE], |row| row.get::<_, String>(0))?;
    let mut surfaces = BTreeMap::new();
    for row in rows {
        let keyword = row?;
        let surface = chosen
            .get(&keyword)
            .cloned()
            .unwrap_or_else(|| keyword.clone());
        surfaces.insert(keyword, surface);
    }
    Ok(surfaces)
}
