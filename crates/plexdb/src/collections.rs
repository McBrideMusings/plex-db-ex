//! Harvesting crowd lists into `collection`, `collection_entry` and
//! `collection_membership` (`plexdb/collections.py`, ADR-0027, ADR-0028).
//!
//! A list's membership is a snapshot, not a fact: on re-pull its whole entry and
//! membership set is replaced in one transaction. Membership holds only titles the
//! store already walked; an entry naming any other is counted and left out of
//! membership, though it stays in `collection_entry` under the strongest external id the
//! source gave. No score is computed here (ADR-0012).
//!
//! What every list source shares lives here: the gate that decides whether a list is
//! fetched this run, the removal of an excluded or removed list, the failure record, and
//! the whole-list write. A source supplies only how it fetches.

use std::collections::{HashMap, HashSet};

use rusqlite::{Connection, Row, params};

use crate::clock::is_before;
use crate::list_decisions::{LETTERBOXD, MDBLIST, canonical_list_url, list_source, list_url_parts};
use crate::mdblist::{MdbError, MdbList, MdbSource};
use crate::staleness::{BadTime, Cutoff, Now, is_stale};

pub const MDBLIST_SOURCE: &str = MDBLIST;
pub const LETTERBOXD_SOURCE: &str = LETTERBOXD;

/// `collection.provenance` of a list that arrived from a source's own top lists.
pub const FEED: &str = "feed";
/// … of a list a person added by URL.
pub const ADDED: &str = "added";
/// … of a list found by searching outward from watched titles.
pub const SEARCH: &str = "search";

/// Failures in a row after which a list is not fetched again until a `retry` decision.
pub const MAX_FAILURES: i64 = 3;
/// The longest failure reason kept in `list_attempt`.
pub const REASON_MAX: usize = 300;

/// The store's id namespaces in the order an id is derived (`identity.PRIORITY`).
const PRIORITY: [&str; 4] = ["imdb", "tmdb", "tvdb", "plex"];

#[derive(Debug, thiserror::Error)]
pub enum HarvestError {
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Mdb(#[from] MdbError),
    #[error("{0}")]
    Other(String),
}

impl From<BadTime> for HarvestError {
    fn from(err: BadTime) -> Self {
        HarvestError::Other(err.to_string())
    }
}

/// What one harvest touched (`HarvestStats`).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct HarvestStats {
    pub lists_seen: i64,
    pub lists_fetched: i64,
    pub lists_cached: i64,
    pub lists_failed: i64,
    pub lists_stopped: i64,
    pub lists_excluded: i64,
    pub lists_removed: i64,
    pub entries_parsed: i64,
    pub entries_stored: i64,
    pub memberships_written: i64,
    pub entries_not_in_library: i64,
    pub entries_duplicate: i64,
}

/// A `list_decision` row.
#[derive(Debug, Clone)]
pub struct Decision {
    pub url: String,
    pub decision: String,
    pub decided_at: String,
    pub added: bool,
}

/// A `list_attempt` row.
#[derive(Debug, Clone)]
pub struct Attempt {
    pub list_key: String,
    pub attempts: i64,
    pub attempted_at: String,
    pub reason: String,
}

fn decision_row(row: &Row<'_>) -> rusqlite::Result<Decision> {
    Ok(Decision {
        url: row.get("url")?,
        decision: row.get("decision")?,
        decided_at: row.get("decided_at")?,
        added: row.get::<_, i64>("added")? != 0,
    })
}

fn attempt_row(row: &Row<'_>) -> rusqlite::Result<Attempt> {
    Ok(Attempt {
        list_key: row.get("list_key")?,
        attempts: row.get("attempts")?,
        attempted_at: row.get("attempted_at")?,
        reason: row.get("reason")?,
    })
}

/// Delete every collection, membership and entry belonging to one source, leaving every
/// other source untouched. Returns `(memberships, entries, collections)` removed.
pub fn wipe_source(conn: &mut Connection, source: &str) -> rusqlite::Result<(usize, usize, usize)> {
    let tx = conn.transaction()?;
    let owned = "(SELECT collection_id FROM collection WHERE source = ?)";
    let memberships = tx.execute(
        &format!("DELETE FROM collection_membership WHERE collection_id IN {owned}"),
        [source],
    )?;
    let entries = tx.execute(
        &format!("DELETE FROM collection_entry WHERE collection_id IN {owned}"),
        [source],
    )?;
    let collections = tx.execute("DELETE FROM collection WHERE source = ?", [source])?;
    tx.commit()?;
    Ok((memberships, entries, collections))
}

pub type Index = HashMap<(String, String, String), String>;

/// `(ns, value, kind)` -> `item_id`, read once for the whole harvest.
pub fn resolve_index(conn: &Connection) -> rusqlite::Result<Index> {
    let mut stmt = conn.prepare("SELECT item_id, ns, value, kind FROM external_ids")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            (row.get(1)?, row.get(2)?, row.get(3)?),
            row.get::<_, String>(0)?,
        ))
    })?;
    rows.collect()
}

/// The `item_id` for one list entry, or `None` if this store never walked it. Tries
/// namespaces in `PRIORITY` order, the order `derive_item_id` uses.
fn resolve(index: &Index, external_ids: &[(String, String)], media_type: &str) -> Option<String> {
    for ns in PRIORITY {
        // `dict(external_ids)`: the last value for a namespace wins.
        let Some((_, value)) = external_ids.iter().rev().find(|(name, _)| name == ns) else {
            continue;
        };
        if let Some(item_id) = index.get(&(ns.to_string(), value.clone(), media_type.to_string())) {
            return Some(item_id.clone());
        }
    }
    None
}

/// Delete these lists' entries, memberships and collection rows.
pub fn drop_lists(conn: &mut Connection, collection_ids: &[String]) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    for collection_id in collection_ids {
        for table in ["collection_membership", "collection_entry", "collection"] {
            tx.execute(
                &format!("DELETE FROM {table} WHERE collection_id = ?"),
                [collection_id],
            )?;
        }
    }
    tx.commit()
}

/// The decision rows naming a list of this source, in table order.
pub fn decisions_for(conn: &Connection, source: &str) -> rusqlite::Result<Vec<Decision>> {
    let mut stmt = conn.prepare("SELECT * FROM list_decision")?;
    let rows = stmt.query_map([], decision_row)?;
    let mut out = Vec::new();
    for row in rows {
        let row = row?;
        if list_source(&row.url) == Some(source) {
            out.push(row);
        }
    }
    Ok(out)
}

pub fn decision_map(decisions: &[Decision]) -> HashMap<&str, &Decision> {
    decisions.iter().map(|d| (d.url.as_str(), d)).collect()
}

pub fn attempts(conn: &Connection) -> rusqlite::Result<HashMap<String, Attempt>> {
    let mut stmt = conn.prepare("SELECT * FROM list_attempt")?;
    let rows = stmt.query_map([], attempt_row)?;
    let mut out = HashMap::new();
    for row in rows {
        let row = row?;
        out.insert(row.list_key.clone(), row);
    }
    Ok(out)
}

/// `collection_id`s that hold at least one entry.
pub fn stocked(conn: &Connection) -> rusqlite::Result<HashSet<String>> {
    let mut stmt = conn.prepare("SELECT DISTINCT collection_id FROM collection_entry")?;
    let rows = stmt.query_map([], |row| row.get(0))?;
    rows.collect()
}

/// Act on the `excluded` and `removed` decisions: wipe an excluded list whatever its
/// provenance, drop a removed list only if a person added it, and forget either's failed
/// fetches. Returns the URLs that must not be fetched.
pub fn apply_removals(
    conn: &mut Connection,
    decisions: &[Decision],
    stats: &mut HarvestStats,
    source: &str,
) -> rusqlite::Result<HashSet<String>> {
    let mut kept = HashSet::new();
    let stored: Vec<(String, Option<String>, String)> = {
        let mut stmt =
            conn.prepare("SELECT collection_id, url, provenance FROM collection WHERE source = ?")?;
        stmt.query_map([source], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<rusqlite::Result<_>>()?
    };
    for decision in decisions {
        let verdict = decision.decision.as_str();
        if verdict != "excluded" && verdict != "removed" {
            continue;
        }
        if verdict == "excluded" {
            kept.insert(decision.url.clone());
        }
        let doomed: Vec<String> = stored
            .iter()
            .filter(|(_, url, provenance)| {
                canonical_list_url(url.as_deref().unwrap_or("")).as_deref() == Some(&decision.url)
                    && (verdict == "excluded" || provenance == ADDED)
            })
            .map(|(id, _, _)| id.clone())
            .collect();
        if !doomed.is_empty() {
            drop_lists(conn, &doomed)?;
            stats.lists_removed += i64::try_from(doomed.len()).unwrap_or(0);
            println!(
                "{source}: {} -> {verdict}, dropped {} stored list(s)",
                decision.url,
                doomed.len()
            );
        }
        conn.execute(
            "DELETE FROM list_attempt WHERE list_key = ?",
            [&decision.url],
        )?;
    }
    Ok(kept)
}

/// Record a failed fetch; returns the failures in a row now.
pub fn record_failure(
    conn: &Connection,
    key: &str,
    held: Option<&Attempt>,
    now_iso: &str,
    reason: &str,
) -> rusqlite::Result<i64> {
    let attempts = held.map_or(1, |h| h.attempts + 1);
    let reason: String = reason.chars().take(REASON_MAX).collect();
    conn.execute(
        "INSERT INTO list_attempt (list_key, attempts, attempted_at, reason) \
         VALUES (?, ?, ?, ?) ON CONFLICT (list_key) DO UPDATE SET \
         attempts = excluded.attempts, attempted_at = excluded.attempted_at, \
         reason = excluded.reason",
        params![key, attempts, now_iso, reason],
    )?;
    Ok(attempts)
}

pub struct GateInput<'a> {
    pub source: &'a str,
    pub key: &'a str,
    pub label: &'a str,
    pub decision: Option<&'a Decision>,
    pub held: Option<&'a Attempt>,
    pub seen_at: Option<&'a str>,
    pub stocked: bool,
    pub cutoff: Cutoff,
}

/// Whether a list is fetched this run, and the failure record that still stands for it.
///
/// A `retry` decided after the last failure clears the count. A list that has failed
/// `MAX_FAILURES` times in a row is left alone (`stopped`). A list fetched inside the
/// staleness window that holds entries is `cached`, unless a `refetch` was decided after
/// that fetch. Each skip prints its log line and bumps `stats`.
pub fn gate(
    conn: &Connection,
    input: &GateInput<'_>,
    stats: &mut HarvestStats,
) -> Result<(bool, Option<Attempt>), HarvestError> {
    let mut held = input.held.cloned();
    if let (Some(attempt), Some(decision)) = (&held, input.decision)
        && decision.decision == "retry"
        && is_before(&attempt.attempted_at, &decision.decided_at)
    {
        conn.execute("DELETE FROM list_attempt WHERE list_key = ?", [input.key])?;
        held = None;
    }
    if let Some(attempt) = &held
        && attempt.attempts >= MAX_FAILURES
    {
        stats.lists_stopped += 1;
        println!(
            "{}: {} -> stopped after {} failed fetches (last {}: {}); decide retry to try again",
            input.source, input.label, attempt.attempts, attempt.attempted_at, attempt.reason
        );
        return Ok((false, held));
    }
    let refetch = input.decision.is_some_and(|d| d.decision == "refetch")
        && input
            .seen_at
            .is_none_or(|seen| is_before(seen, input.decision.map_or("", |d| &d.decided_at)));
    if !refetch
        && let Some(seen) = input.seen_at
        && !is_stale(seen, input.cutoff)?
        && input.stocked
    {
        stats.lists_cached += 1;
        println!(
            "{}: {} -> cached (fetched {seen})",
            input.source, input.label
        );
        return Ok((false, held));
    }
    Ok((true, held))
}

/// One title on a fetched list, whatever the source.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListEntry {
    pub external_ids: Vec<(String, String)>,
    pub media_type: String,
    pub title: Option<String>,
    pub year: Option<i64>,
}

/// `(rank, ns, value, kind, title, year)` of a `collection_entry` row.
pub type EntryRow = (i64, String, String, String, Option<String>, Option<i64>);

/// `(entry rows, memberships)` for a list's entries in list order. Array position is the
/// rank, 1-indexed. A title outside the library is dropped from the memberships and
/// counted; a title already placed keeps its first position.
pub fn place_entries(
    index: &Index,
    entries: &[ListEntry],
    stats: &mut HarvestStats,
) -> (Vec<EntryRow>, Vec<(String, i64)>) {
    let mut memberships = Vec::new();
    let mut stored = Vec::new();
    let mut placed: HashSet<String> = HashSet::new();
    let mut listed: HashSet<(String, String, String)> = HashSet::new();
    for (offset, entry) in entries.iter().enumerate() {
        let position = i64::try_from(offset).unwrap_or(0) + 1;
        let (ns, value) = &entry.external_ids[0];
        let key = (ns.clone(), value.clone(), entry.media_type.clone());
        if listed.insert(key) {
            stored.push((
                position,
                ns.clone(),
                value.clone(),
                entry.media_type.clone(),
                entry.title.clone(),
                entry.year,
            ));
        }
        let Some(item_id) = resolve(index, &entry.external_ids, &entry.media_type) else {
            stats.entries_not_in_library += 1;
            continue;
        };
        if !placed.insert(item_id.clone()) {
            stats.entries_duplicate += 1;
            continue;
        }
        memberships.push((item_id, position));
    }
    (stored, memberships)
}

pub struct Snapshot<'a> {
    pub collection_id: &'a str,
    pub source: &'a str,
    pub name: Option<&'a str>,
    pub url: Option<&'a str>,
    pub size: Option<i64>,
    pub likes: Option<i64>,
    pub provenance: &'a str,
    pub list_key: &'a str,
    pub fetched_iso: &'a str,
    pub stored: &'a [EntryRow],
    pub memberships: &'a [(String, i64)],
    pub seed_count: Option<i64>,
}

/// Replace one list's collection row, entries and memberships in one transaction, and
/// forget its failed fetches. A list is written whole or not at all.
pub fn write_snapshot(conn: &mut Connection, snapshot: &Snapshot<'_>) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    let id = snapshot.collection_id;
    tx.execute("DELETE FROM collection_entry WHERE collection_id = ?", [id])?;
    tx.execute(
        "DELETE FROM collection_membership WHERE collection_id = ?",
        [id],
    )?;
    tx.execute(
        "INSERT INTO collection \
         (collection_id, source, name, url, size, likes, observed_at, provenance, seed_count) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT(collection_id) DO UPDATE SET \
         source = excluded.source, name = excluded.name, url = excluded.url, \
         size = excluded.size, likes = excluded.likes, \
         observed_at = excluded.observed_at, provenance = excluded.provenance, \
         seed_count = excluded.seed_count",
        params![
            id,
            snapshot.source,
            snapshot.name,
            snapshot.url,
            snapshot.size,
            snapshot.likes,
            snapshot.fetched_iso,
            snapshot.provenance,
            snapshot.seed_count
        ],
    )?;
    {
        let mut insert = tx.prepare(
            "INSERT INTO collection_entry \
             (collection_id, rank, ns, value, kind, title, year) VALUES (?, ?, ?, ?, ?, ?, ?)",
        )?;
        for (rank, ns, value, kind, title, year) in snapshot.stored {
            insert.execute(params![id, rank, ns, value, kind, title, year])?;
        }
        let mut member = tx.prepare(
            "INSERT INTO collection_membership \
             (collection_id, item_id, rank, mentions, observed_at) VALUES (?, ?, ?, NULL, ?)",
        )?;
        for (item_id, rank) in snapshot.memberships {
            member.execute(params![id, item_id, rank, snapshot.fetched_iso])?;
        }
    }
    tx.execute(
        "DELETE FROM list_attempt WHERE list_key = ?",
        [snapshot.list_key],
    )?;
    tx.commit()
}

/// How a unit arrived and what the feed said about it.
struct Unit {
    key: String,
    url: Option<String>,
    meta: Option<MdbList>,
    provenance: &'static str,
}

fn list_url(meta: &MdbList) -> Option<String> {
    (!meta.user_name.is_empty() && !meta.slug.is_empty())
        .then(|| format!("https://mdblist.com/lists/{}/{}", meta.user_name, meta.slug))
}

/// The feed's lists, then every list a person added that the feed did not name. An
/// excluded list is counted and left out; an added list also in the feed is fetched once,
/// as added.
fn units(
    source: &mut dyn MdbSource,
    decisions: &[Decision],
    excluded: &HashSet<String>,
    stats: &mut HarvestStats,
) -> Result<Vec<Unit>, MdbError> {
    let added: HashSet<&str> = decisions
        .iter()
        .filter(|d| d.added)
        .map(|d| d.url.as_str())
        .collect();
    let mut units = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for meta in source.top_lists()? {
        let url = list_url(&meta);
        let key = url
            .as_deref()
            .and_then(canonical_list_url)
            .unwrap_or_else(|| format!("{MDBLIST_SOURCE}:{}", meta.list_id));
        seen.insert(key.clone());
        if excluded.contains(&key) {
            stats.lists_excluded += 1;
            continue;
        }
        let provenance = if added.contains(key.as_str()) {
            ADDED
        } else {
            FEED
        };
        units.push(Unit {
            key,
            url,
            meta: Some(meta),
            provenance,
        });
    }
    let mut extra: Vec<&str> = added
        .iter()
        .copied()
        .filter(|url| !seen.contains(*url) && !excluded.contains(*url))
        .collect();
    extra.sort_unstable();
    for url in extra {
        units.push(Unit {
            key: url.to_string(),
            url: Some(url.to_string()),
            meta: None,
            provenance: ADDED,
        });
    }
    Ok(units)
}

/// Refresh every MDBList top list, and every list a person added by URL, into
/// `collection`, `collection_entry` and `collection_membership`.
///
/// A list inside the staleness window that already holds entries is not re-fetched.
/// `list_decision` steers the run: an excluded list is wiped and never fetched; a removed
/// list a person added is dropped; an added URL is fetched with provenance `added`; a
/// `refetch` decided after a list was last fetched ignores staleness for that fetch. A
/// list whose fetch fails records the attempt in `list_attempt` and the run goes on; after
/// `MAX_FAILURES` failures in a row the list is left alone until a `retry`. A failure that
/// says nothing about one list (the quota, a refused key, no answer) stops the run.
pub fn refresh_mdblist(
    conn: &mut Connection,
    source: &mut dyn MdbSource,
    stale_days: i64,
) -> Result<HarvestStats, HarvestError> {
    let cutoff = Now::read().cutoff(stale_days);
    let mut stats = HarvestStats::default();

    let decisions = decisions_for(conn, MDBLIST_SOURCE)?;
    let excluded = apply_removals(conn, &decisions, &mut stats, MDBLIST_SOURCE)?;
    let by_decision = decision_map(&decisions);
    let attempts = attempts(conn)?;

    let index = resolve_index(conn)?;
    let stocked = stocked(conn)?;
    let observed: HashMap<String, String> = {
        let mut stmt =
            conn.prepare("SELECT collection_id, observed_at FROM collection WHERE source = ?")?;
        stmt.query_map([MDBLIST_SOURCE], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?
    };
    let mut by_url: HashMap<String, String> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT collection_id, url FROM collection WHERE source = ? AND url IS NOT NULL",
        )?;
        let rows = stmt.query_map([MDBLIST_SOURCE], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (id, url) = row?;
            if let Some(canonical) = canonical_list_url(&url) {
                by_url.insert(canonical, id);
            }
        }
    }

    for unit in units(source, &decisions, &excluded, &mut stats)? {
        stats.lists_seen += 1;
        let label = unit.url.clone().unwrap_or_else(|| unit.key.clone());
        let collection_id = match &unit.meta {
            Some(meta) => Some(format!("{MDBLIST_SOURCE}:{}", meta.list_id)),
            None => by_url.get(&unit.key).cloned(),
        };
        let (fetch, held) = gate(
            conn,
            &GateInput {
                source: MDBLIST_SOURCE,
                key: &unit.key,
                label: &label,
                decision: by_decision.get(unit.key.as_str()).copied(),
                held: attempts.get(&unit.key),
                seen_at: collection_id
                    .as_ref()
                    .and_then(|id| observed.get(id))
                    .map(String::as_str),
                stocked: collection_id
                    .as_ref()
                    .is_some_and(|id| stocked.contains(id)),
                cutoff,
            },
            &mut stats,
        )?;
        if !fetch {
            continue;
        }

        let fetched = (|| -> Result<(MdbList, crate::mdblist::MdbContents), MdbError> {
            let meta = match unit.meta.clone() {
                Some(meta) => meta,
                None => {
                    let Some((user, slug)) = list_url_parts(&unit.key) else {
                        return Err(MdbError::new(
                            format!("{} is not an MDBList list URL", unit.key),
                            None,
                        ));
                    };
                    source.find_list(&user, &slug)?
                }
            };
            let contents = source.list_entries(meta.list_id)?;
            Ok((meta, contents))
        })();
        let (meta, contents) = match fetched {
            Ok(found) => found,
            Err(err) => {
                println!(
                    "mdblist: {label} -> HTTP {}, failed: {err}",
                    err.status_text()
                );
                if err.is_systemic() {
                    return Err(err.into());
                }
                record_failure(
                    conn,
                    &unit.key,
                    held.as_ref(),
                    &Now::read().iso(),
                    &err.message,
                )?;
                stats.lists_failed += 1;
                continue;
            }
        };
        let collection_id = format!("{MDBLIST_SOURCE}:{}", meta.list_id);
        let url = list_url(&meta).or_else(|| unit.url.clone());
        let fetched_iso = Now::read().iso();
        // Array position is the rank the list presents; MDBList's own `rank` is a
        // different ordering entirely and is ignored.
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
        let (stored_rows, memberships) = place_entries(&index, &entries, &mut stats);
        write_snapshot(
            conn,
            &Snapshot {
                collection_id: &collection_id,
                source: MDBLIST_SOURCE,
                name: Some(&meta.name),
                url: url.as_deref(),
                size: meta.size,
                likes: meta.likes,
                provenance: unit.provenance,
                list_key: &unit.key,
                fetched_iso: &fetched_iso,
                stored: &stored_rows,
                memberships: &memberships,
                seed_count: None,
            },
        )?;
        stats.lists_fetched += 1;
        stats.entries_parsed += i64::try_from(entries.len()).unwrap_or(0);
        stats.entries_stored += i64::try_from(stored_rows.len()).unwrap_or(0);
        stats.memberships_written += i64::try_from(memberships.len()).unwrap_or(0);
        println!(
            "mdblist: {} -> HTTP {}, {} entries parsed, {} resolved to the library, written",
            url.as_deref().unwrap_or(&collection_id),
            contents.status,
            entries.len(),
            memberships.len()
        );
    }
    Ok(stats)
}

/// The harvests' one summary line, minus its source prefix.
pub fn report_line(prefix: &str, stats: &HarvestStats) -> String {
    format!(
        "{prefix}: {} list(s) seen, {} fetched, {} already cached, {} failed, {} stopped after \
         repeated failures, {} excluded, {} dropped, {} entry(s) stored, {} membership(s) \
         written, {} entry(s) skipped (not in library), {} entry(s) skipped (already placed on \
         that list)",
        stats.lists_seen,
        stats.lists_fetched,
        stats.lists_cached,
        stats.lists_failed,
        stats.lists_stopped,
        stats.lists_excluded,
        stats.lists_removed,
        stats.entries_stored,
        stats.memberships_written,
        stats.entries_not_in_library,
        stats.entries_duplicate,
    )
}
