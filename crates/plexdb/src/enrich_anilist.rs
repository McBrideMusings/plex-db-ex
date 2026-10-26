//! Fetching AniList's tags for anime titles into `keywords` and `keyword_roles`, with the
//! rank AniList gives each tag, cached per title (`plexdb/enrich_anilist.py`).
//!
//! AniList has no TMDB, IMDb or TVDB id, so a title reaches it through Fribb's mapping
//! ([`crate::anilist`]): the title's TMDB id (matched on movie vs. show), else its IMDb id,
//! else, for a show, its TVDB id, first hit wins. A walked title none of those map is not
//! anime for this source: it costs no request and gets no cursor.
//!
//! **One title, several AniList entries.** AniList lists each season of a show as its own
//! entry, and a Plex show covers them all, so a show maps to every entry its id names. Their
//! tags merge into the one title: a tag keeps its highest rank across the entries, and is a
//! spoiler if any entry flags it as one.
//!
//! Each tag goes through the same normalization TMDB's keywords do, under
//! `source = 'anilist'`, with AniList's 0-100 rank in `enrichment.rank` verbatim. A spoiler
//! tag is stored under key `spoiler_keyword` instead of `keyword`, so a reader of key
//! `keyword` never sees it. A tag's category states a role through [`role_for`], written to
//! `keyword_roles` with score, model and error NULL (ADR-0019). A role row is per keyword,
//! not per title, so a title's refresh leaves it alone; `--rewipe` clears them.
//!
//! Each title's rows under this source are a snapshot. One `enrichment_cursor` row per title
//! (namespace `keywords`, source `anilist`, key `fetched`) is written even when AniList had no
//! tags (ADR-0013). A title that drops out of the mapping keeps the rows it has until
//! `--rewipe`. Titles are asked in batches of up to [`crate::anilist::PAGE_SIZE`] AniList
//! ids; a batch that fails writes nothing for any of its titles, so a re-run retries them.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use plexdb_keywords::{NAMESPACE, RawKeyword, state_role, write_title_keywords};
use rusqlite::{Connection, TransactionBehavior};
use serde_json::Value;

use crate::anilist::{AniListSource, PAGE_SIZE, Tag};
use crate::cursors::{FETCHED_KEY, delete_cursors, load_fetched, write_fetched};
use crate::staleness::{Now, is_stale};
use crate::sweep::{SweepError, abort_message};

/// This writer's source name, in `keywords`, `keyword_roles` and `enrichment_cursor`.
pub const SOURCE: &str = "anilist";

/// AniList tag category -> the role it states. A category matches its own entry or, failing
/// that, its nearest `-`-separated parent: `Theme-Other-Organisations` falls back to
/// `Theme-Other`, then `Theme`. A category with no match states no role.
pub const CATEGORY_ROLES: [(&str, &str); 3] = [
    ("Theme", "theme"),
    ("Setting-Time", "era"),
    ("Cast-Traits", "character_trait"),
];

/// Failed batches in a row that abort the sweep.
pub const MAX_CONSECUTIVE_FAILURES: usize = 3;

pub fn role_for(category: Option<&str>) -> Option<&'static str> {
    let mut category = category?;
    while !category.is_empty() {
        if let Some((_, role)) = CATEGORY_ROLES.iter().find(|(name, _)| *name == category) {
            return Some(role);
        }
        category = category.rsplit_once('-').map_or("", |(parent, _)| parent);
    }
    None
}

/// Fribb's mapping, turned into lookups from the ids a title carries.
#[derive(Debug, Default)]
pub struct AnimeIndex {
    /// `(item type, TMDB id)` -> AniList ids. TMDB's movie and TV ids overlap, so the kind is
    /// part of the key.
    tmdb: HashMap<(String, String), Vec<i64>>,
    imdb: HashMap<String, Vec<i64>>,
    /// TVDB series ids; a movie's TVDB id is a different id space and never looked up.
    tvdb: HashMap<String, Vec<i64>>,
    /// Mapping entries with an `anilist_id`.
    pub size: usize,
}

impl AnimeIndex {
    /// Every AniList id the first id namespace that hits maps this title to: TMDB, then IMDb,
    /// then TVDB for a show.
    pub fn lookup(
        &self,
        kind: &str,
        tmdb_ids: &[String],
        imdb_ids: &[String],
        tvdb_ids: &[String],
    ) -> Vec<i64> {
        let tmdb: Vec<i64> = tmdb_ids
            .iter()
            .flat_map(|t| {
                self.tmdb
                    .get(&(kind.to_string(), t.clone()))
                    .into_iter()
                    .flatten()
            })
            .copied()
            .collect();
        let imdb: Vec<i64> = imdb_ids
            .iter()
            .flat_map(|i| self.imdb.get(i).into_iter().flatten())
            .copied()
            .collect();
        let tvdb: Vec<i64> = if kind == "show" {
            tvdb_ids
                .iter()
                .flat_map(|t| self.tvdb.get(t).into_iter().flatten())
                .copied()
                .collect()
        } else {
            Vec::new()
        };
        for hits in [tmdb, imdb, tvdb] {
            if !hits.is_empty() {
                return hits
                    .into_iter()
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect();
            }
        }
        Vec::new()
    }
}

/// Every id inside one Fribb field: a scalar, a list, or an object keyed by media type
/// (`{"tv": 31911}`), nested in any of those shapes.
fn flatten(value: &Value) -> Vec<String> {
    match value {
        Value::Number(n) if n.is_i64() || n.is_u64() => vec![n.to_string()],
        Value::String(text) => {
            let text = plexdb_store::py_strip(text);
            if text.is_empty() {
                Vec::new()
            } else {
                vec![text.to_string()]
            }
        }
        Value::Array(items) => items.iter().flat_map(flatten).collect(),
        Value::Object(map) => map.values().flat_map(flatten).collect(),
        _ => Vec::new(),
    }
}

/// Fribb's entries -> an [`AnimeIndex`]. An entry with no integer `anilist_id` is skipped. A
/// `themoviedb_id` object keyed `movie`/`tv` says the kind; a bare scalar or list takes it
/// from the entry's `type` (`MOVIE`, or a show).
pub fn index_mapping(entries: &[Value]) -> AnimeIndex {
    let mut index = AnimeIndex::default();
    for entry in entries {
        let Some(anilist_id) = entry
            .get("anilist_id")
            .filter(|v| v.is_i64() || v.is_u64())
            .and_then(Value::as_i64)
        else {
            continue;
        };
        index.size += 1;
        match entry.get("themoviedb_id") {
            Some(Value::Object(map)) => {
                for (media_type, ids) in map {
                    let kind = match media_type.as_str() {
                        "movie" => "movie",
                        "tv" => "show",
                        _ => continue,
                    };
                    for tmdb_id in flatten(ids) {
                        index
                            .tmdb
                            .entry((kind.into(), tmdb_id))
                            .or_default()
                            .push(anilist_id);
                    }
                }
            }
            other => {
                let kind = if entry.get("type").and_then(Value::as_str) == Some("MOVIE") {
                    "movie"
                } else {
                    "show"
                };
                for tmdb_id in other.map(flatten).unwrap_or_default() {
                    index
                        .tmdb
                        .entry((kind.into(), tmdb_id))
                        .or_default()
                        .push(anilist_id);
                }
            }
        }
        for imdb_id in entry.get("imdb_id").map(flatten).unwrap_or_default() {
            index.imdb.entry(imdb_id).or_default().push(anilist_id);
        }
        for tvdb_id in entry.get("tvdb_id").map(flatten).unwrap_or_default() {
            index.tvdb.entry(tvdb_id).or_default().push(anilist_id);
        }
    }
    index
}

/// What one sweep touched: the summary `plexdb enrich-anilist` prints.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct AniListStats {
    pub titles_seen: usize,
    /// Movies and shows the mapping places on AniList.
    pub titles_anime: usize,
    pub titles_fetched: usize,
    pub titles_cached: usize,
    /// Fetched titles AniList gave at least one tag for.
    pub titles_matched: usize,
    /// Titles in a batch whose request failed. Nothing is written for them.
    pub titles_failed: usize,
    pub batches_sent: usize,
    pub anilist_ids_asked: usize,
    pub keywords_written: usize,
    pub spoiler_keywords_written: usize,
    /// Distinct (keyword, role) rows this run stated or restated.
    pub roles_stated: usize,
}

/// Delete everything this source owns: its keywords and spoiler keywords, its role rows and
/// its cursors. Another source's rows stay. Returns the number of rows removed.
pub fn wipe(conn: &mut Connection) -> rusqlite::Result<usize> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut removed = tx.execute(
        "DELETE FROM enrichment WHERE namespace = ? AND source = ?",
        [NAMESPACE, SOURCE],
    )?;
    tx.execute("DELETE FROM keyword_surfaces WHERE source = ?", [SOURCE])?;
    removed += tx.execute("DELETE FROM keyword_roles WHERE source = ?", [SOURCE])?;
    removed += delete_cursors(&tx, NAMESPACE, SOURCE, None)?;
    tx.commit()?;
    Ok(removed)
}

type Due = Vec<(String, Vec<i64>)>;

/// Group titles so each batch asks for at most `batch_ids` AniList ids. A title mapping to
/// more than that goes alone; the client pages through it.
fn batches(due: Due, batch_ids: usize) -> Vec<Due> {
    let mut out = Vec::new();
    let mut batch: Due = Vec::new();
    let mut size = 0;
    for (item_id, anilist_ids) in due {
        if !batch.is_empty() && size + anilist_ids.len() > batch_ids {
            out.push(std::mem::take(&mut batch));
            size = 0;
        }
        size += anilist_ids.len();
        batch.push((item_id, anilist_ids));
    }
    if !batch.is_empty() {
        out.push(batch);
    }
    out
}

/// Ask AniList for the tags of every walked anime movie/show whose cursor is missing or
/// stale, batching titles up to [`PAGE_SIZE`] AniList ids per request.
pub fn enrich_anilist(
    conn: &mut Connection,
    source: &mut dyn AniListSource,
    stale_days: i64,
) -> Result<AniListStats, SweepError> {
    let cutoff = Now::read().cutoff(stale_days);
    let mut stats = AniListStats::default();

    let cached = load_fetched(conn, NAMESPACE, SOURCE, FETCHED_KEY)?;
    let kinds: Vec<(String, String)> = conn
        .prepare("SELECT item_id, type FROM items WHERE type IN ('movie', 'show')")?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let mut ids: HashMap<String, HashMap<String, Vec<String>>> = HashMap::new();
    {
        let mut statement = conn.prepare(
            "SELECT e.item_id, e.ns, e.value FROM external_ids e \
             JOIN items i ON i.item_id = e.item_id \
             WHERE i.type IN ('movie', 'show') AND e.ns IN ('tmdb', 'imdb', 'tvdb') \
             ORDER BY e.item_id, e.ns, e.value",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })?;
        for row in rows {
            let (item_id, ns, value) = row?;
            ids.entry(item_id)
                .or_default()
                .entry(ns)
                .or_default()
                .push(value);
        }
    }

    let entries = source
        .mapping()
        .map_err(|err| SweepError::Source(err.to_string()))?;
    let index = index_mapping(&entries);
    if index.size == 0 {
        // Every title would read as "not anime" and be skipped: an upstream breakage, not a
        // library with no anime in it.
        return Err(SweepError::Source(
            "the anime mapping holds no entry with an anilist_id".into(),
        ));
    }

    let none: Vec<String> = Vec::new();
    let mut due: Due = Vec::new();
    for (item_id, kind) in &kinds {
        stats.titles_seen += 1;
        let own = ids.get(item_id);
        let get = |ns: &str| own.and_then(|m| m.get(ns)).unwrap_or(&none);
        let anilist_ids = index.lookup(kind, get("tmdb"), get("imdb"), get("tvdb"));
        if anilist_ids.is_empty() {
            continue;
        }
        stats.titles_anime += 1;
        if let Some(fetched_at) = cached.get(item_id)
            && !is_stale(fetched_at, cutoff)?
        {
            stats.titles_cached += 1;
            continue;
        }
        due.push((item_id.clone(), anilist_ids));
    }

    let mut stated: BTreeSet<(String, String)> = BTreeSet::new();
    let mut consecutive_failures = 0;
    for batch in batches(due, PAGE_SIZE) {
        let asked: Vec<i64> = batch
            .iter()
            .flat_map(|(_, title_ids)| title_ids.iter().copied())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        stats.batches_sent += 1;
        stats.anilist_ids_asked += asked.len();
        let found = match source.tags(&asked) {
            Ok(found) => found,
            Err(err) => {
                stats.titles_failed += batch.len();
                consecutive_failures += 1;
                println!("anilist: batch of {} title(s) failed: {err}", batch.len());
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES {
                    return Err(SweepError::Source(abort_message(
                        MAX_CONSECUTIVE_FAILURES,
                        "batches",
                        stats.titles_fetched,
                        stats.titles_failed,
                        &err,
                    )));
                }
                continue;
            }
        };
        consecutive_failures = 0;
        write_batch(
            conn,
            &batch,
            &found,
            &Now::read().iso(),
            &mut stats,
            &mut stated,
        )?;
    }

    stats.roles_stated = stated.len();
    Ok(stats)
}

/// Replace every title's rows in one transaction, cursors included, so an interrupted sweep
/// never leaves a cursor vouching for rows already deleted.
fn write_batch(
    conn: &mut Connection,
    batch: &Due,
    found: &BTreeMap<i64, Vec<Tag>>,
    now_iso: &str,
    stats: &mut AniListStats,
    stated: &mut BTreeSet<(String, String)>,
) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    for (item_id, anilist_ids) in batch {
        write_fetched(&tx, NAMESPACE, SOURCE, item_id, now_iso, FETCHED_KEY)?;
        // Every entry the title maps to; `write_title_keywords` merges them per stored keyword:
        // the highest rank wins, and a keyword is a spoiler if any copy says so.
        let tags: Vec<&Tag> = anilist_ids
            .iter()
            .flat_map(|a| found.get(a).into_iter().flatten())
            .collect();
        let stored = write_title_keywords(
            &tx,
            item_id,
            SOURCE,
            tags.iter().map(|tag| RawKeyword {
                surface: tag.name.clone(),
                rank: tag.rank,
                spoiler: tag.spoiler,
            }),
            now_iso,
        )?;
        let mut spoilers: HashMap<String, bool> = HashMap::new();
        for tag in &tags {
            let Some(keyword) = stored.get(plexdb_store::py_strip(&tag.name)) else {
                continue;
            };
            let held = spoilers.entry(keyword.clone()).or_insert(false);
            *held = *held || tag.spoiler;
            if let Some(role) = role_for(tag.category.as_deref()) {
                state_role(&tx, keyword, role, SOURCE, now_iso)?;
                stated.insert((keyword.clone(), role.to_string()));
            }
        }
        let spoiler_count = spoilers.values().filter(|spoiler| **spoiler).count();
        stats.spoiler_keywords_written += spoiler_count;
        stats.keywords_written += spoilers.len() - spoiler_count;
        stats.titles_fetched += 1;
        if !spoilers.is_empty() {
            stats.titles_matched += 1;
        }
    }
    tx.commit()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn a_category_states_the_role_of_its_nearest_parent() {
        assert_eq!(role_for(Some("Theme")), Some("theme"));
        assert_eq!(role_for(Some("Theme-Other-Organisations")), Some("theme"));
        assert_eq!(role_for(Some("Setting-Time")), Some("era"));
        assert_eq!(role_for(Some("Setting-Scene")), None);
        assert_eq!(
            role_for(Some("Cast-Traits-Species")),
            Some("character_trait")
        );
        assert_eq!(role_for(None), None);
    }

    #[test]
    fn the_mapping_places_a_title_by_the_first_id_namespace_that_hits() {
        let index = index_mapping(&[
            json!({"anilist_id": 5, "themoviedb_id": {"tv": [100, "101"]}, "imdb_id": "tt1",
                   "tvdb_id": 7}),
            json!({"anilist_id": 6, "themoviedb_id": 100, "type": "TV"}),
            json!({"anilist_id": 9, "themoviedb_id": 200, "type": "MOVIE", "tvdb_id": 8}),
            json!({"anilist_id": true, "themoviedb_id": 300}),
            json!({"themoviedb_id": 400}),
        ]);
        assert_eq!(index.size, 3);
        let ids = |v: &[&str]| v.iter().map(ToString::to_string).collect::<Vec<_>>();
        // A show's TMDB id hits both entries, sorted and de-duplicated.
        assert_eq!(index.lookup("show", &ids(&["100"]), &[], &[]), [5, 6]);
        // The same id as a movie hits nothing; the IMDb id then decides.
        assert_eq!(
            index.lookup("movie", &ids(&["100"]), &ids(&["tt1"]), &[]),
            [5]
        );
        assert_eq!(index.lookup("movie", &ids(&["200"]), &[], &[]), [9]);
        // A TVDB id places a show only.
        assert_eq!(index.lookup("show", &[], &[], &ids(&["8"])), [9]);
        assert!(index.lookup("movie", &[], &[], &ids(&["8"])).is_empty());
    }

    #[test]
    fn titles_are_cut_into_batches_of_at_most_the_page_size() {
        let due: Due = vec![
            ("a".into(), vec![1, 2]),
            ("b".into(), vec![3, 4]),
            ("c".into(), (10..20).collect()),
            ("d".into(), vec![5]),
        ];
        let cut_due = batches(due, 4);
        let cut: Vec<Vec<&str>> = cut_due
            .iter()
            .map(|batch| batch.iter().map(|(id, _)| id.as_str()).collect())
            .collect();
        assert_eq!(cut, [vec!["a", "b"], vec!["c"], vec!["d"]]);
    }
}
