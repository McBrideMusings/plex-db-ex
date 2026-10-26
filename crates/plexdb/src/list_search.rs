//! Finding crowd lists outward from the titles the household has watched
//! (`plexdb/list_search.py`).
//!
//! MDBList's top-lists feed follows popularity. This harvest follows taste: each watched
//! movie or show is a **seed**, searched by title through MDBList's list search; every list
//! the search returns is a **candidate**; a candidate is fetched, and kept with provenance
//! `search` only when it holds at least `min_titles` titles of this library.
//! `collection.seed_count` is how many of those titles are seeds (watched).
//!
//! MDBList matches a list's *name* against the query and nothing else, so a name match is
//! only a lead; the fetched list's own entries decide whether it is kept. A candidate that
//! holds too few library titles is written nowhere and marked rejected, so it is not
//! fetched again until `stale_days` have passed.
//!
//! A seed asked at `t` is not asked again until `stale_days` have passed
//! (`enrichment_cursor` under namespace `list_search`, source `mdblist`, key `fetched`).
//! What a seed found is bookkeeping, not a fact (ADR-0013): a hit is an
//! `enrichment_cursor` row, `key = hit:<list url>`, rewritten whole each time the seed is
//! asked; a rejection is `key = rejected:<list url>` on one seed that surfaced it.

use std::collections::{BTreeSet, HashMap, HashSet};

use rusqlite::{Connection, OptionalExtension, params};

use crate::clock::{self, is_before};
use crate::collections::{
    ADDED, Attempt, Decision, FEED, GateInput, HarvestError, HarvestStats, ListEntry,
    MDBLIST_SOURCE, SEARCH, Snapshot, apply_removals, attempts as load_attempts, decision_map,
    decisions_for, drop_lists, gate, place_entries, record_failure, resolve_index, stocked,
    write_snapshot,
};
use crate::list_decisions::{canonical_list_url, list_url_parts};
use crate::mdblist::MdbSource;

pub const NAMESPACE: &str = "list_search";
pub const CURSOR_SOURCE: &str = "mdblist";
pub const HIT_PREFIX: &str = "hit:";
pub const REJECTED_PREFIX: &str = "rejected:";
const FETCHED_KEY: &str = "fetched";
const ATTEMPTED_KEY: &str = "attempted";

/// Seeds one run asks, candidates one run fetches, and library titles a candidate must hold.
pub const DEFAULT_MAX_SEEDS: i64 = 25;
pub const DEFAULT_MAX_LISTS: i64 = 10;
pub const DEFAULT_MIN_TITLES: i64 = 5;

/// A watched movie, or a show with a watched episode, with how many plays it has.
const SEEDS: &str = "
SELECT w.item_id, i.title, COUNT(*) AS plays
FROM (
    SELECT p.item_id AS item_id FROM plays p JOIN items m ON m.item_id = p.item_id
     WHERE m.type = 'movie'
    UNION ALL
    SELECT e.show_item_id FROM plays p JOIN items e ON e.item_id = p.item_id
     WHERE e.type = 'episode' AND e.show_item_id IS NOT NULL
) w JOIN items i ON i.item_id = w.item_id
GROUP BY w.item_id
ORDER BY plays DESC, w.item_id
";

/// What one outward search touched, on top of the list harvest's own counts.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct SearchStats {
    pub harvest: HarvestStats,
    pub seeds_asked: i64,
    pub seeds_cached: i64,
    pub seeds_failed: i64,
    pub lists_found: i64,
    pub lists_qualified: i64,
    pub lists_rejected: i64,
}

#[derive(Debug, Clone, Copy)]
pub struct Options {
    pub stale_days: i64,
    pub limit: i64,
    pub min_titles: i64,
    pub max_lists: i64,
}

struct Seed {
    item_id: String,
    title: String,
}

fn url_of(user_name: &str, slug: &str) -> Option<String> {
    if user_name.is_empty() || slug.is_empty() {
        return None;
    }
    canonical_list_url(&format!("https://mdblist.com/lists/{user_name}/{slug}"))
}

fn is_fresh(fetched_at: Option<&String>, cutoff: i128) -> Result<bool, HarvestError> {
    match fetched_at {
        None => Ok(false),
        Some(at) => Ok(!clock::is_stale(at, cutoff)?),
    }
}

/// Search each due seed's title and record the lists returned. Returns the URLs found.
fn ask_seeds(
    conn: &mut Connection,
    source: &mut dyn MdbSource,
    stats: &mut SearchStats,
    seeds: &[Seed],
    cutoff: i128,
    limit: i64,
) -> Result<BTreeSet<String>, HarvestError> {
    let mut fetched: HashMap<String, String> = HashMap::new();
    let mut attempted: HashMap<String, String> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT item_id, key, fetched_at FROM enrichment_cursor \
             WHERE namespace = ? AND source = ? AND key IN (?, ?)",
        )?;
        let rows = stmt.query_map(
            params![NAMESPACE, CURSOR_SOURCE, FETCHED_KEY, ATTEMPTED_KEY],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            },
        )?;
        for row in rows {
            let (item_id, key, at) = row?;
            if key == FETCHED_KEY {
                fetched.insert(item_id, at);
            } else {
                attempted.insert(item_id, at);
            }
        }
    }
    let mut due = Vec::new();
    for seed in seeds {
        if !is_fresh(fetched.get(&seed.item_id), cutoff)? {
            due.push(seed);
        }
    }
    stats.seeds_cached = i64::try_from(seeds.len() - due.len()).unwrap_or(0);
    // Never-attempted first, then oldest attempt first; the sort is stable.
    due.sort_by_key(|seed| {
        (
            attempted.contains_key(&seed.item_id),
            attempted.get(&seed.item_id).cloned().unwrap_or_default(),
        )
    });
    let mut found = BTreeSet::new();
    let take = usize::try_from(limit).unwrap_or(0);
    for seed in due.into_iter().take(take) {
        let results = match source.search_lists(&seed.title) {
            Ok(results) => results,
            Err(err) => {
                println!(
                    "mdblist: search \"{}\" (seed {}) -> HTTP {}, failed: {err}",
                    seed.title,
                    seed.item_id,
                    err.status_text()
                );
                if err.is_systemic() {
                    return Err(err.into());
                }
                mark_attempted(conn, &seed.item_id)?;
                stats.seeds_failed += 1;
                continue;
            }
        };
        let urls: BTreeSet<String> = results
            .iter()
            .filter_map(|r| url_of(&r.user_name, &r.slug))
            .collect();
        let now = clock::now_iso();
        let tx = conn.transaction()?;
        tx.execute(
            "DELETE FROM enrichment_cursor WHERE item_id = ? AND namespace = ? \
             AND source = ? AND key LIKE ?",
            params![
                seed.item_id,
                NAMESPACE,
                CURSOR_SOURCE,
                format!("{HIT_PREFIX}%")
            ],
        )?;
        for url in &urls {
            tx.execute(
                "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
                 VALUES (?, ?, ?, ?, ?)",
                params![
                    seed.item_id,
                    NAMESPACE,
                    CURSOR_SOURCE,
                    format!("{HIT_PREFIX}{url}"),
                    now
                ],
            )?;
        }
        write_fetched(&tx, &seed.item_id, &now)?;
        tx.commit()?;
        stats.seeds_asked += 1;
        println!(
            "mdblist: search \"{}\" (seed {}) -> {} list(s)",
            seed.title,
            seed.item_id,
            urls.len()
        );
        found.extend(urls);
    }
    Ok(found)
}

const UPSERT: &str = "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) \
    VALUES (?, ?, ?, ?, ?) \
    ON CONFLICT(item_id, namespace, source, key) DO UPDATE SET fetched_at = excluded.fetched_at";

/// Record a title as fetched and clear its `attempted` cursor.
fn write_fetched(conn: &Connection, item_id: &str, now_iso: &str) -> rusqlite::Result<()> {
    conn.execute(
        UPSERT,
        params![item_id, NAMESPACE, CURSOR_SOURCE, FETCHED_KEY, now_iso],
    )?;
    conn.execute(
        "DELETE FROM enrichment_cursor \
         WHERE item_id = ? AND namespace = ? AND source = ? AND key = ?",
        params![item_id, NAMESPACE, CURSOR_SOURCE, ATTEMPTED_KEY],
    )?;
    Ok(())
}

/// Move a title that produced nothing cacheable to the back of the next run's order.
fn mark_attempted(conn: &Connection, item_id: &str) -> rusqlite::Result<()> {
    let now = clock::iso_micros(clock::now_micros());
    conn.execute(
        UPSERT,
        params![item_id, NAMESPACE, CURSOR_SOURCE, ATTEMPTED_KEY, now],
    )?;
    Ok(())
}

/// Remember that a fetched candidate held too few library titles, on one seed that
/// surfaced it, so it is not fetched again inside the staleness window.
fn reject(conn: &Connection, url: &str, now_iso: &str) -> rusqlite::Result<()> {
    let seed: Option<String> = conn
        .query_row(
            "SELECT item_id FROM enrichment_cursor WHERE namespace = ? AND source = ? AND key = ? \
             ORDER BY item_id LIMIT 1",
            params![NAMESPACE, CURSOR_SOURCE, format!("{HIT_PREFIX}{url}")],
            |r| r.get(0),
        )
        .optional()?;
    let Some(item_id) = seed else {
        return Ok(());
    };
    conn.execute(
        UPSERT,
        params![
            item_id,
            NAMESPACE,
            CURSOR_SOURCE,
            format!("{REJECTED_PREFIX}{url}"),
            now_iso
        ],
    )?;
    Ok(())
}

/// Ask up to `limit` due seeds which lists carry their title, then fetch up to
/// `max_lists` of the candidates and keep those holding at least `min_titles` library
/// titles.
pub fn refresh_list_search(
    conn: &mut Connection,
    source: &mut dyn MdbSource,
    options: Options,
) -> Result<SearchStats, HarvestError> {
    let cutoff = clock::cutoff(options.stale_days);
    let mut stats = SearchStats::default();

    let decisions: Vec<Decision> = decisions_for(conn, MDBLIST_SOURCE)?;
    let excluded = apply_removals(conn, &decisions, &mut stats.harvest, MDBLIST_SOURCE)?;
    stats.harvest.lists_excluded = i64::try_from(excluded.len()).unwrap_or(0);
    let by_decision = decision_map(&decisions);

    let seeds: Vec<Seed> = {
        let mut stmt = conn.prepare(SEEDS)?;
        stmt.query_map([], |r| {
            Ok(Seed {
                item_id: r.get(0)?,
                title: r.get::<_, Option<String>>(1)?.unwrap_or_default(),
            })
        })?
        .collect::<rusqlite::Result<_>>()?
    };
    let watched: HashSet<&str> = seeds.iter().map(|s| s.item_id.as_str()).collect();
    let found = ask_seeds(conn, source, &mut stats, &seeds, cutoff, options.limit)?;
    stats.lists_found = i64::try_from(found.len()).unwrap_or(0);

    let counts: Vec<(String, i64)> = {
        let mut stmt = conn.prepare(
            "SELECT key, COUNT(*) AS seeds FROM enrichment_cursor \
             WHERE namespace = ? AND source = ? AND key LIKE ? GROUP BY key",
        )?;
        stmt.query_map(
            params![NAMESPACE, CURSOR_SOURCE, format!("{HIT_PREFIX}%")],
            |r| {
                Ok((
                    r.get::<_, String>(0)?[HIT_PREFIX.len()..].to_string(),
                    r.get(1)?,
                ))
            },
        )?
        .collect::<rusqlite::Result<_>>()?
    };
    let rejected: HashMap<String, String> = {
        let mut stmt = conn.prepare(
            "SELECT key, MAX(fetched_at) AS fetched_at FROM enrichment_cursor \
             WHERE namespace = ? AND source = ? AND key LIKE ? GROUP BY key",
        )?;
        stmt.query_map(
            params![NAMESPACE, CURSOR_SOURCE, format!("{REJECTED_PREFIX}%")],
            |r| {
                Ok((
                    r.get::<_, String>(0)?[REJECTED_PREFIX.len()..].to_string(),
                    r.get::<_, String>(1)?,
                ))
            },
        )?
        .collect::<rusqlite::Result<_>>()?
    };
    let mut candidates: Vec<(String, i64)> = counts
        .into_iter()
        .filter(|(url, _)| !excluded.contains(url))
        .collect();
    candidates.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));

    let attempts: HashMap<String, Attempt> = load_attempts(conn)?;
    let index = resolve_index(conn)?;
    let stocked_ids = stocked(conn)?;
    struct Stored {
        collection_id: String,
        observed_at: String,
        provenance: String,
    }
    let stored: HashMap<String, Stored> = {
        let mut stmt = conn.prepare(
            "SELECT collection_id, url, observed_at, provenance FROM collection \
             WHERE source = ? AND url IS NOT NULL",
        )?;
        let rows = stmt.query_map([MDBLIST_SOURCE], |r| {
            Ok((
                r.get::<_, String>(1)?,
                Stored {
                    collection_id: r.get(0)?,
                    observed_at: r.get(2)?,
                    provenance: r.get(3)?,
                },
            ))
        })?;
        let mut out = HashMap::new();
        for row in rows {
            let (url, stored) = row?;
            if let Some(canonical) = canonical_list_url(&url) {
                out.insert(canonical, stored);
            }
        }
        out
    };

    let mut over_cap = 0;
    let mut asked = 0;
    for (url, surfaced) in &candidates {
        stats.harvest.lists_seen += 1;
        let decision = by_decision.get(url.as_str()).copied();
        let redecided = decision.is_some_and(|d| {
            (d.decision == "refetch" || d.decision == "retry")
                && rejected
                    .get(url)
                    .is_some_and(|at| is_before(at, &d.decided_at))
        });
        if !redecided && is_fresh(rejected.get(url), cutoff)? {
            stats.harvest.lists_cached += 1;
            println!(
                "mdblist: {url} -> rejected earlier (fewer than {} library titles)",
                options.min_titles
            );
            continue;
        }
        let held_row = stored.get(url);
        let (fetch, held) = gate(
            conn,
            &GateInput {
                source: MDBLIST_SOURCE,
                key: url,
                label: url,
                decision,
                held: attempts.get(url),
                seen_at: held_row.map(|h| h.observed_at.as_str()),
                stocked: held_row.is_some_and(|h| stocked_ids.contains(&h.collection_id)),
                cutoff,
            },
            &mut stats.harvest,
        )?;
        if !fetch {
            continue;
        }
        if asked >= options.max_lists {
            over_cap += 1;
            continue;
        }
        asked += 1;
        let Some((user, slug)) = list_url_parts(url) else {
            return Err(HarvestError::Other(format!(
                "{url} is not an MDBList list URL"
            )));
        };
        let fetched = source.find_list(&user, &slug).and_then(|meta| {
            source
                .list_entries(meta.list_id)
                .map(|contents| (meta, contents))
        });
        let (meta, contents) = match fetched {
            Ok(found) => found,
            Err(err) => {
                println!(
                    "mdblist: {url} -> HTTP {}, failed: {err}",
                    err.status_text()
                );
                if err.is_systemic() {
                    return Err(err.into());
                }
                record_failure(conn, url, held.as_ref(), &clock::now_iso(), &err.message)?;
                stats.harvest.lists_failed += 1;
                continue;
            }
        };
        let entries: Vec<ListEntry> = contents
            .entries
            .iter()
            .map(|e| ListEntry {
                external_ids: e.external_ids.clone(),
                media_type: e.media_type.clone(),
                title: e.title.clone(),
                year: e.year,
            })
            .collect();
        let (entry_rows, memberships) = place_entries(&index, &entries, &mut stats.harvest);
        stats.harvest.entries_parsed += i64::try_from(entries.len()).unwrap_or(0);
        let now = clock::now_iso();
        let collection_id = format!("{MDBLIST_SOURCE}:{}", meta.list_id);
        if i64::try_from(memberships.len()).unwrap_or(0) < options.min_titles {
            reject(conn, url, &now)?;
            if let Some(held_row) = held_row.filter(|h| h.provenance == SEARCH) {
                drop_lists(conn, std::slice::from_ref(&held_row.collection_id))?;
            }
            stats.lists_rejected += 1;
            println!(
                "mdblist: {url} -> HTTP {}, {} entries parsed, {} resolved to the library, \
                 rejected (needs {})",
                contents.status,
                entries.len(),
                memberships.len(),
                options.min_titles
            );
            continue;
        }
        let seed_count = i64::try_from(
            memberships
                .iter()
                .filter(|(item_id, _)| watched.contains(item_id.as_str()))
                .count(),
        )
        .unwrap_or(0);
        let keeps = held_row.map(|h| h.provenance.as_str());
        let provenance = if by_decision.get(url.as_str()).is_some_and(|d| d.added) {
            ADDED
        } else if keeps == Some(FEED) || keeps == Some(ADDED) {
            keeps.unwrap_or(SEARCH)
        } else {
            SEARCH
        };
        write_snapshot(
            conn,
            &Snapshot {
                collection_id: &collection_id,
                source: MDBLIST_SOURCE,
                name: Some(&meta.name),
                url: Some(url),
                size: meta.size,
                likes: meta.likes,
                provenance,
                list_key: url,
                fetched_iso: &now,
                stored: &entry_rows,
                memberships: &memberships,
                seed_count: (provenance == SEARCH).then_some(seed_count),
            },
        )?;
        stats.harvest.lists_fetched += 1;
        stats.lists_qualified += 1;
        stats.harvest.entries_stored += i64::try_from(entry_rows.len()).unwrap_or(0);
        stats.harvest.memberships_written += i64::try_from(memberships.len()).unwrap_or(0);
        println!(
            "mdblist: {url} -> HTTP {}, {} entries parsed, {} resolved to the library, \
             qualified and written (search, {seed_count} seed(s), surfaced by {surfaced})",
            contents.status,
            entries.len(),
            memberships.len()
        );
    }
    if over_cap > 0 {
        println!(
            "mdblist: {over_cap} candidate(s) left for the next run (cap {})",
            options.max_lists
        );
    }
    Ok(stats)
}

/// Delete every list found by search and every seed's cursor and hits, so the next run
/// asks every seed again. Lists from the feed or added by a person are untouched. Returns
/// `(lists_removed, cursors_removed)`.
pub fn wipe_searched(conn: &mut Connection) -> rusqlite::Result<(usize, usize)> {
    let ids: Vec<String> = {
        let mut stmt = conn
            .prepare("SELECT collection_id FROM collection WHERE source = ? AND provenance = ?")?;
        stmt.query_map(params![MDBLIST_SOURCE, SEARCH], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?
    };
    drop_lists(conn, &ids)?;
    let cursors = conn.execute(
        "DELETE FROM enrichment_cursor WHERE namespace = ? AND source = ?",
        params![NAMESPACE, CURSOR_SOURCE],
    )?;
    Ok((ids.len(), cursors))
}

pub fn report_line(stats: &SearchStats) -> String {
    format!(
        "mdblist search: {} seed(s) asked, {} still fresh, {} failed; {} list(s) found, {} \
         fetched and kept, {} fetched and rejected, {} already cached, {} failed, {} stopped \
         after repeated failures, {} excluded, {} membership(s) written",
        stats.seeds_asked,
        stats.seeds_cached,
        stats.seeds_failed,
        stats.lists_found,
        stats.lists_qualified,
        stats.lists_rejected,
        stats.harvest.lists_cached,
        stats.harvest.lists_failed,
        stats.harvest.lists_stopped,
        stats.harvest.lists_excluded,
        stats.harvest.memberships_written,
    )
}
