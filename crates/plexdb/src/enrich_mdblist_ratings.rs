//! Every rating MDBList relays for a movie or show, into `ratings`, cached per title
//! (`plexdb/enrich_mdblist_ratings.py`).
//!
//! MDBList's batch title endpoint answers up to 200 IMDb ids per request with each title's
//! ratings from IMDb, TMDB, Trakt, Letterboxd, Rotten Tomatoes, Metacritic, RogerEbert and
//! MyAnimeList. Each becomes one row in the `ratings` namespace under `source = 'mdblist'`:
//! key = the site's name as MDBList gives it, value = the site's own number on its own scale,
//! exactly as the JSON carried it. A `<site>_votes` row beside it holds the vote count where
//! one is given. A site whose value is null writes no row at all, its votes included, and
//! nothing is converted or combined (ADR-0012). `rank` stays NULL.
//!
//! Titles are looked up by IMDb id only. Each title's rows under this source are a snapshot. One
//! `enrichment_cursor` row per title (namespace `ratings`, source `mdblist`, key `fetched`) is
//! written for every title in a batch that answered, even one MDBList did not return
//! (ADR-0013).
//!
//! A failed request writes nothing for any title in its batch and records only an `attempted`
//! cursor for each, which is scheduling, not a fact. Three failed requests in a row abort the
//! run. A 429 is different: the day's quota is spent and every later request would get the same
//! answer, so the run stops at once, marks nothing, and leaves every remaining title in its
//! place for the next run.
//!
//! A run sends at most `limit` requests, because the daily quota counts requests and
//! `harvest-mdblist` draws on the same one. Due titles never attempted come first, in `item_id`
//! order, then attempted ones, oldest attempt first; batches are cut from that order per media
//! type and sent in the order of their first title.

use std::collections::HashMap;

use rusqlite::Connection;
use serde_json::Value;

use crate::cursors::{FETCHED_KEY, delete_cursors, load_cursors, mark_attempted, write_fetched};
use crate::mdblist::{BATCH_SIZE, Kind, MdbRatingsSource, MdbTitleRatings};
use crate::staleness::{Now, is_stale};
use crate::sweep::{SweepError, abort_message};

/// The namespace this writer fills, in `enrichment` and `enrichment_cursor`.
pub const NAMESPACE: &str = "ratings";
/// This writer's source name.
pub const SOURCE: &str = "mdblist";
const VOTES_SUFFIX: &str = "_votes";

/// Requests one run sends at most: at 200 titles a batch this covers 20,000 titles and leaves
/// 900 of the free tier's 1,000 daily requests for `harvest-mdblist`.
pub const DEFAULT_MAX_REQUESTS: i64 = 100;

/// Failed requests in a row that abort the run.
pub const MAX_CONSECUTIVE_FAILURES: usize = 3;

/// What one run touched: the summary `plexdb enrich-mdblist-ratings` prints.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct RatingsStats {
    pub titles_seen: usize,
    pub titles_cached: usize,
    /// Movies and shows with no `imdb` row in `external_ids`: skipped, not an error.
    pub titles_skipped_no_imdb_id: usize,
    /// Due titles in batches past the per-run request cap, left for the next run.
    pub titles_capped: usize,
    pub requests_sent: usize,
    pub requests_failed: usize,
    /// Titles that got a `fetched` cursor this run.
    pub titles_fetched: usize,
    /// Fetched titles MDBList's answer left out.
    pub titles_not_found: usize,
    /// Fetched titles with at least one rating row.
    pub titles_rated: usize,
    /// Titles in a failed batch. Nothing is written for them.
    pub titles_failed: usize,
    pub ratings_written: usize,
    pub votes_written: usize,
    /// MDBList's message when it refused a request for a spent quota, which ends the run with
    /// nothing marked.
    pub quota_spent: Option<String>,
}

#[derive(Debug, Clone)]
struct Due {
    item_id: String,
    media_type: String,
    imdb_id: String,
}

/// Delete this source's ratings and cursors. Every other source and namespace stays. Returns
/// the number of rows removed.
pub fn wipe(conn: &mut Connection) -> rusqlite::Result<usize> {
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let mut removed = tx.execute(
        "DELETE FROM enrichment WHERE namespace = ? AND source = ?",
        [NAMESPACE, SOURCE],
    )?;
    removed += delete_cursors(&tx, NAMESPACE, SOURCE, None)?;
    tx.commit()?;
    Ok(removed)
}

/// Fetch the ratings of every walked movie and show with an `imdb` external id whose cursor is
/// missing or stale, in at most `limit` requests.
pub fn enrich_mdblist_ratings(
    conn: &mut Connection,
    source: &mut dyn MdbRatingsSource,
    stale_days: i64,
    limit: usize,
) -> Result<RatingsStats, SweepError> {
    let cutoff = Now::read().cutoff(stale_days);
    let mut stats = RatingsStats::default();

    let cursors = load_cursors(conn, NAMESPACE, SOURCE)?;
    // A join, not a correlated subquery, for the reason `enrich_letterboxd` gives.
    let candidates: Vec<(String, String, Option<String>)> = conn
        .prepare(
            "SELECT i.item_id AS item_id, i.type AS type, MIN(e.value) AS imdb_id \
             FROM items i \
             LEFT JOIN external_ids e ON e.item_id = i.item_id AND e.ns = 'imdb' \
             WHERE i.type IN ('movie', 'show') \
             GROUP BY i.item_id \
             ORDER BY i.item_id",
        )?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
        .collect::<rusqlite::Result<_>>()?;

    let mut due: Vec<Due> = Vec::new();
    for (item_id, media_type, imdb_id) in candidates {
        stats.titles_seen += 1;
        let Some(imdb_id) = imdb_id.filter(|id| !id.is_empty()) else {
            stats.titles_skipped_no_imdb_id += 1;
            continue;
        };
        if let Some(fetched_at) = cursors.fetched.get(&item_id)
            && !is_stale(fetched_at, cutoff)?
        {
            stats.titles_cached += 1;
            continue;
        }
        due.push(Due {
            item_id,
            media_type,
            imdb_id,
        });
    }
    // Stable, so titles never attempted keep their `item_id` order.
    due.sort_by_cached_key(|title| cursors.order_key(&title.item_id));

    let all = cut_batches(&due);
    let sent = all.len().min(limit);
    stats.titles_capped = all[sent..].iter().map(Vec::len).sum();

    let mut consecutive_failures = 0;
    for (index, batch) in all[..sent].iter().enumerate() {
        stats.requests_sent += 1;
        let imdb_ids: Vec<String> = batch.iter().map(|t| t.imdb_id.clone()).collect();
        let found = match source.ratings(&batch[0].media_type, &imdb_ids) {
            Ok(found) => found,
            Err(err) if err.kind == Kind::Quota => {
                stats.quota_spent = Some(err.to_string());
                stats.titles_capped += all[index..sent].iter().map(Vec::len).sum::<usize>();
                break;
            }
            Err(err) => {
                stats.requests_failed += 1;
                stats.titles_failed += batch.len();
                consecutive_failures += 1;
                let ids: Vec<&str> = batch.iter().map(|t| t.item_id.as_str()).collect();
                mark_attempted(conn, NAMESPACE, SOURCE, &ids, &Now::read().iso_micros())?;
                println!(
                    "mdblist: {} batch of {} failed: {err}",
                    batch[0].media_type,
                    batch.len()
                );
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES {
                    return Err(SweepError::Source(abort_message(
                        MAX_CONSECUTIVE_FAILURES,
                        "requests",
                        stats.titles_fetched,
                        stats.titles_failed,
                        &err,
                    )));
                }
                continue;
            }
        };
        consecutive_failures = 0;
        write_batch(conn, batch, &found, &Now::read().iso(), &mut stats)?;
    }
    Ok(stats)
}

/// Cut `due` into per-media-type batches of at most [`BATCH_SIZE`], keeping `due`'s order
/// inside each, and order the batches by their first title.
fn cut_batches(due: &[Due]) -> Vec<Vec<Due>> {
    let mut types: Vec<&str> = Vec::new();
    let mut by_type: HashMap<&str, Vec<&Due>> = HashMap::new();
    for title in due {
        if !by_type.contains_key(title.media_type.as_str()) {
            types.push(&title.media_type);
        }
        by_type.entry(&title.media_type).or_default().push(title);
    }
    let position: HashMap<&str, usize> = due
        .iter()
        .enumerate()
        .map(|(i, t)| (t.item_id.as_str(), i))
        .collect();
    let mut batches: Vec<Vec<Due>> = Vec::new();
    for media_type in types {
        for chunk in by_type[media_type].chunks(BATCH_SIZE) {
            batches.push(chunk.iter().map(|t| (*t).clone()).collect());
        }
    }
    batches.sort_by_key(|batch| position[batch[0].item_id.as_str()]);
    batches
}

/// Python's `json.dumps` of a rating's value.
fn dump(value: &Value) -> String {
    value.to_string()
}

/// Replace every batch title's rows and cursor in one transaction, so an interrupted run never
/// leaves a cursor vouching for rows already deleted.
fn write_batch(
    conn: &mut Connection,
    batch: &[Due],
    found: &[MdbTitleRatings],
    now_iso: &str,
    stats: &mut RatingsStats,
) -> rusqlite::Result<()> {
    let mut by_imdb: HashMap<&str, &MdbTitleRatings> = HashMap::new();
    for title in found {
        by_imdb.insert(&title.imdb_id, title);
    }
    let tx = conn.transaction()?;
    for title in batch {
        tx.execute(
            "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ?",
            [title.item_id.as_str(), NAMESPACE, SOURCE],
        )?;
        write_fetched(&tx, NAMESPACE, SOURCE, &title.item_id, now_iso, FETCHED_KEY)?;
        stats.titles_fetched += 1;
        let Some(answer) = by_imdb.get(title.imdb_id.as_str()) else {
            stats.titles_not_found += 1;
            continue;
        };
        // A key repeated by two sites of one name keeps the later value, in the first one's
        // place, as a dict does.
        let mut rows: Vec<(String, String)> = Vec::new();
        let mut put = |key: String, value: String| match rows.iter_mut().find(|(k, _)| *k == key) {
            Some(row) => row.1 = value,
            None => rows.push((key, value)),
        };
        for rating in &answer.ratings {
            let Some(value) = &rating.value else {
                continue;
            };
            put(rating.site.clone(), dump(value));
            if let Some(votes) = rating.votes {
                put(format!("{}{VOTES_SUFFIX}", rating.site), votes.to_string());
            }
        }
        let votes = rows
            .iter()
            .filter(|(key, _)| key.ends_with(VOTES_SUFFIX))
            .count();
        stats.votes_written += votes;
        stats.ratings_written += rows.len() - votes;
        for (key, value) in &rows {
            tx.execute(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) \
                 VALUES (?, ?, ?, ?, ?, ?)",
                [
                    title.item_id.as_str(),
                    NAMESPACE,
                    SOURCE,
                    key,
                    value,
                    now_iso,
                ],
            )?;
        }
        if !rows.is_empty() {
            stats.titles_rated += 1;
        }
    }
    tx.commit()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn due(item_id: &str, media_type: &str) -> Due {
        Due {
            item_id: item_id.into(),
            media_type: media_type.into(),
            imdb_id: format!("tt{item_id}"),
        }
    }

    #[test]
    fn batches_are_per_media_type_and_sent_in_the_order_of_their_first_title() {
        let mut titles: Vec<Due> = (0..250)
            .map(|n| due(&format!("m{n:03}"), "movie"))
            .collect();
        titles.insert(1, due("s0", "show"));
        let cut = cut_batches(&titles);
        let shape: Vec<(String, usize)> = cut
            .iter()
            .map(|batch| (batch[0].media_type.clone(), batch.len()))
            .collect();
        assert_eq!(
            shape,
            [
                ("movie".to_string(), 200),
                ("show".to_string(), 1),
                ("movie".to_string(), 50)
            ]
        );
    }
}
