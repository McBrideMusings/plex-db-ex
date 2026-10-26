//! Harvesting Letterboxd lists into `collection`, `collection_entry` and
//! `collection_membership` — the second crowd-list source after MDBList
//! (`plexdb/letterboxd_lists.py`).
//!
//! Letterboxd has no list feed, so this fetches only the lists a person added by URL and
//! the Letterboxd lists already stored. Everything after fetching is the MDBList harvest's
//! own procedure (`collections`): the same gating, removal and whole-list snapshot.
//!
//! An entry's identity is its TMDB id, scoped by media type, read once per run from its
//! film page. A film Letterboxd has not matched to TMDB is stored under
//! `('letterboxd', <slug>, 'movie')`: it can never resolve to a library title, but the
//! list still names it. A failure writes nothing: it records a `list_attempt` row with the
//! reason and leaves the stored list as it was.

use std::collections::{BTreeSet, HashMap, HashSet};

use rusqlite::Connection;

use crate::collections::{
    ADDED, GateInput, HarvestError, HarvestStats, LETTERBOXD_SOURCE, ListEntry, Snapshot,
    apply_removals, attempts as load_attempts, decision_map, decisions_for, gate, place_entries,
    record_failure, resolve_index, stocked, write_snapshot,
};
use crate::letterboxd::{FilmIds, LbError, LbSource};
use crate::list_decisions::{canonical_list_url, letterboxd_list_parts};
use crate::staleness::Now;

/// The most entries one list may hold and still be harvested: each costs a film page
/// request, so a list past this is refused whole.
pub const MAX_LIST_ENTRIES: usize = 2000;
/// Lists in a row that fail before the run stops.
pub const MAX_CONSECUTIVE_FAILURES: usize = 3;
/// Films between progress lines while a long list resolves.
const PROGRESS_EVERY: usize = 100;

fn collection_id(user: &str, slug: &str) -> String {
    format!("{LETTERBOXD_SOURCE}:{user}/{slug}")
}

/// Refresh every Letterboxd list a person added, and every one already stored.
pub fn refresh_letterboxd_lists(
    conn: &mut Connection,
    source: &mut dyn LbSource,
    stale_days: i64,
) -> Result<HarvestStats, HarvestError> {
    let cutoff = Now::read().cutoff(stale_days);
    let mut stats = HarvestStats::default();

    let decisions = decisions_for(conn, LETTERBOXD_SOURCE)?;
    let excluded = apply_removals(conn, &decisions, &mut stats, LETTERBOXD_SOURCE)?;
    let by_decision = decision_map(&decisions);
    let attempts = load_attempts(conn)?;
    let index = resolve_index(conn)?;
    let stocked_ids = stocked(conn)?;

    struct Stored {
        observed_at: String,
        provenance: String,
    }
    let mut stored_rows: HashMap<String, Stored> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT collection_id, url, observed_at, provenance FROM collection \
             WHERE source = ? AND url IS NOT NULL",
        )?;
        let rows = stmt.query_map([LETTERBOXD_SOURCE], |r| {
            Ok((
                r.get::<_, String>(1)?,
                Stored {
                    observed_at: r.get(2)?,
                    provenance: r.get(3)?,
                },
            ))
        })?;
        for row in rows {
            let (url, stored) = row?;
            if let Some(canonical) = canonical_list_url(&url) {
                stored_rows.insert(canonical, stored);
            }
        }
    }
    let added: HashSet<&str> = decisions
        .iter()
        .filter(|d| d.added)
        .map(|d| d.url.as_str())
        .collect();
    let keys: BTreeSet<&str> = added
        .iter()
        .copied()
        .chain(stored_rows.keys().map(String::as_str))
        .filter(|key| !excluded.contains(*key))
        .collect();
    stats.lists_excluded = i64::try_from(excluded.len()).unwrap_or(0);

    let mut films: HashMap<String, FilmIds> = HashMap::new();
    let mut failures_in_a_row = 0;
    for key in keys {
        if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES {
            println!(
                "letterboxd: {failures_in_a_row} lists in a row failed; leaving the rest for \
                 the next run"
            );
            break;
        }
        stats.lists_seen += 1;
        let Some((user, slug)) = letterboxd_list_parts(key) else {
            return Err(HarvestError::Other(format!(
                "{key} is not a Letterboxd list URL"
            )));
        };
        let id = collection_id(&user, &slug);
        let held_row = stored_rows.get(key);
        let (fetch, held) = gate(
            conn,
            &GateInput {
                source: LETTERBOXD_SOURCE,
                key,
                label: key,
                decision: by_decision.get(key).copied(),
                held: attempts.get(key),
                seen_at: held_row.map(|h| h.observed_at.as_str()),
                stocked: stocked_ids.contains(&id),
                cutoff,
            },
            &mut stats,
        )?;
        if !fetch {
            continue;
        }

        let resolved =
            (|| -> Result<(crate::letterboxd::ListContents, Vec<ListEntry>), LbError> {
                let contents = source.list_contents(&user, &slug)?;
                if contents.entries.len() > MAX_LIST_ENTRIES {
                    return Err(LbError(format!(
                        "the list holds {} entries, over the {MAX_LIST_ENTRIES} this harvest resolves",
                        contents.entries.len()
                    )));
                }
                if let Some(size) = contents.size
                    && i64::try_from(contents.entries.len()).unwrap_or(i64::MAX) < size
                {
                    return Err(LbError(format!(
                        "read {} of the {size} entries the list says it holds",
                        contents.entries.len()
                    )));
                }
                let mut entries = Vec::new();
                for (offset, film) in contents.entries.iter().enumerate() {
                    let done = offset + 1;
                    if !films.contains_key(&film.slug) {
                        films.insert(film.slug.clone(), source.film_ids(&film.slug)?);
                    }
                    if done % PROGRESS_EVERY == 0 {
                        println!(
                            "letterboxd: {key} -> {done} of {} films resolved",
                            contents.entries.len()
                        );
                    }
                    let ids = &films[&film.slug];
                    let external = match &ids.tmdb_id {
                        Some(tmdb) => ("tmdb".to_string(), tmdb.clone()),
                        None => (LETTERBOXD_SOURCE.to_string(), film.slug.clone()),
                    };
                    entries.push(ListEntry {
                        external_ids: vec![external],
                        media_type: ids.media_type.clone(),
                        title: Some(film.title.clone()),
                        year: film.year,
                    });
                }
                Ok((contents, entries))
            })();
        let (contents, entries) = match resolved {
            Ok(found) => found,
            Err(err) => {
                println!("letterboxd: {key} -> failed: {err}");
                record_failure(conn, key, held.as_ref(), &Now::read().iso(), &err.0)?;
                stats.lists_failed += 1;
                failures_in_a_row += 1;
                continue;
            }
        };
        failures_in_a_row = 0;

        let (stored, memberships) = place_entries(&index, &entries, &mut stats);
        let provenance = if added.contains(key) {
            ADDED
        } else {
            held_row.map_or(ADDED, |h| h.provenance.as_str())
        };
        write_snapshot(
            conn,
            &Snapshot {
                collection_id: &id,
                source: LETTERBOXD_SOURCE,
                name: Some(&contents.name),
                url: Some(key),
                size: Some(
                    contents
                        .size
                        .unwrap_or(i64::try_from(contents.entries.len()).unwrap_or(i64::MAX)),
                ),
                likes: None,
                provenance,
                list_key: key,
                fetched_iso: &Now::read().iso(),
                stored: &stored,
                memberships: &memberships,
                seed_count: None,
            },
        )?;
        stats.lists_fetched += 1;
        stats.entries_parsed += i64::try_from(contents.entries.len()).unwrap_or(0);
        stats.entries_stored += i64::try_from(stored.len()).unwrap_or(0);
        stats.memberships_written += i64::try_from(memberships.len()).unwrap_or(0);
        println!(
            "letterboxd: {key} -> HTTP 200, {} page(s), {} entries parsed, {} resolved to the \
             library, written",
            contents.pages,
            contents.entries.len(),
            memberships.len()
        );
    }
    Ok(stats)
}
