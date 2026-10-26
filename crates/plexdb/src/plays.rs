//! Ingesting Plex watch history into `plays`, and enriching it from Tautulli
//! (ADR-0004).
//!
//! The store owns watch history itself: Plex is the source that always works, and
//! Tautulli an optional adapter that fills in what Plex's history cannot. Tautulli
//! never creates a `plays` row, only fills columns on one that exists.
//!
//! **Resolution goes through the walk's rating-key map**: Plex's history names a
//! title only by `ratingKey`, and `plex_items` turns that into an `item_id`. An event
//! whose key has no row there is counted, never dropped silently.
//!
//! **Idempotent**: `plays.history_key` is the primary key, so a re-ingest that
//! reaches an already-recorded event is a no-op.
//!
//! **Incremental, without losing an unresolved event.** `plays_ingest_cursor` tracks
//! the oldest event still outstanding: if a run saw any unresolved event, the next
//! cutoff is pinned at the oldest of them, so it is asked for again next time. Only a
//! run that sees no unresolved event advances the cutoff, to the newest it saw.

use std::collections::{BTreeSet, HashMap};

use plexdb_store::StoreError;
use rusqlite::{Connection, OptionalExtension};
use serde_json::Value;

use crate::plex::PlexSource;
use crate::pyval::{get, number, py_str, sql, truthy};
use crate::tautulli::TautulliSource;

/// What one ingest touched.
#[derive(Debug, Default)]
pub struct PlaysStats {
    pub events_seen: usize,
    pub plays_written: usize,
    /// Already recorded by a prior ingest (same history_key).
    pub already_recorded: usize,
    /// An event whose rating_key has no row in plex_items.
    pub unresolved_rating_key: usize,
    /// An event whose deviceID is not in Plex's device list; still written, with a
    /// null client identifier and platform.
    pub unresolved_device: usize,
}

type Device = (Option<String>, Option<String>);

/// `deviceID -> (clientIdentifier, platform)`, fetched once per ingest. An empty
/// `clientIdentifier` identifies nothing and is treated as absent.
fn device_cache(source: &dyn PlexSource) -> Result<HashMap<String, Device>, StoreError> {
    let mut cache = HashMap::new();
    for device in source.devices()? {
        let id = get(&device, "id");
        if id.is_null() {
            continue;
        }
        let pick = |key: &str| {
            let value = get(&device, key);
            truthy(value).then(|| py_str(value))
        };
        cache.insert(py_str(id), (pick("clientIdentifier"), pick("platform")));
    }
    Ok(cache)
}

fn cursor(conn: &Connection) -> Result<Option<i64>, StoreError> {
    Ok(conn
        .query_row(
            "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1",
            [],
            |row| row.get::<_, Option<i64>>(0),
        )
        .optional()?
        .flatten())
}

fn viewed(value: &Value) -> Result<i64, StoreError> {
    value
        .as_i64()
        .or_else(|| value.as_f64().map(|f| f as i64))
        .ok_or_else(|| {
            StoreError::Store(format!(
                "a history event has a viewedAt that is not a number: {value}"
            ))
        })
}

/// Ingest every new history event from `source` into `plays`, in one transaction.
pub fn ingest_plays(
    conn: &mut Connection,
    source: &dyn PlexSource,
) -> Result<PlaysStats, StoreError> {
    let mut stats = PlaysStats::default();
    let since = cursor(conn)?;
    let devices = device_cache(source)?;
    let mut resolved: HashMap<String, String> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT rating_key, item_id FROM plex_items")?;
        for row in stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (rating_key, item_id) = row?;
            resolved.insert(rating_key, item_id);
        }
    }
    let events = source.history(since)?;

    // The oldest `viewed_at` this run could not resolve; while set, the cursor must
    // not move past it. And the newest it saw at all.
    let mut oldest_unresolved: Option<i64> = None;
    let mut newest_seen: Option<i64> = None;

    let tx = conn.transaction()?;
    for event in &events {
        stats.events_seen += 1;
        let (history_key, raw_rating_key) = (get(event, "historyKey"), get(event, "ratingKey"));
        let (viewed_at, account_id) = (get(event, "viewedAt"), get(event, "accountID"));
        if !truthy(history_key)
            || !truthy(raw_rating_key)
            || viewed_at.is_null()
            || account_id.is_null()
        {
            // Not a real, addressable viewing event — nothing to key a row on.
            continue;
        }
        let at = viewed(viewed_at)?;
        if newest_seen.is_none_or(|newest| at > newest) {
            newest_seen = Some(at);
        }
        let rating_key = py_str(raw_rating_key);
        let Some(item_id) = resolved.get(&rating_key) else {
            stats.unresolved_rating_key += 1;
            if oldest_unresolved.is_none_or(|oldest| at < oldest) {
                oldest_unresolved = Some(at);
            }
            continue;
        };
        let (mut client_identifier, mut platform) = (None, None);
        let raw_device_id = get(event, "deviceID");
        if !raw_device_id.is_null() {
            match devices.get(&py_str(raw_device_id)) {
                None => stats.unresolved_device += 1,
                Some((client, os)) => (client_identifier, platform) = (client.clone(), os.clone()),
            }
        }
        // `rating_key` is stored beside the `item_id` it resolved to: the id is what
        // every query joins on, and the key is the evidence it came from, without
        // which a play cannot be re-pointed when an identity is later corrected.
        let written = tx.execute(
            "INSERT INTO plays \
                (history_key, item_id, rating_key, plex_account_id, client_identifier, \
                 platform, viewed_at) \
             VALUES (?, ?, ?, ?, ?, ?, ?) \
             ON CONFLICT(history_key) DO NOTHING",
            rusqlite::params_from_iter([
                sql(history_key),
                rusqlite::types::Value::Text(item_id.clone()),
                rusqlite::types::Value::Text(rating_key),
                sql(account_id),
                client_identifier
                    .map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                platform.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                sql(viewed_at),
            ]),
        )?;
        if written > 0 {
            stats.plays_written += 1;
        } else {
            stats.already_recorded += 1;
        }
    }
    if let Some(value) = oldest_unresolved.or(newest_seen) {
        tx.execute(
            "INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, ?) \
             ON CONFLICT(id) DO UPDATE SET since_viewed_at = excluded.since_viewed_at",
            [value],
        )?;
    }
    // Nothing seen at all: the cursor stays where it was.
    tx.commit()?;
    Ok(stats)
}

/// ±900 seconds. Measured against the live server over 197 completed rows: this
/// window matched 145 of the 151 that had any candidate play at all; ±300s would
/// have matched only 110.
pub const MATCH_WINDOW_SECONDS: f64 = 900.0;

/// `tautulli_user_id -> plex_account_id`, joined on account name, which resolves the
/// one account whose id differs between the two systems: the server owner (ADR-0010).
#[derive(Debug, Default)]
pub struct AccountIdMap {
    pub by_tautulli_user_id: HashMap<i64, i64>,
    /// Account names Tautulli reports that no Plex account name matches.
    pub tautulli_only_names: Vec<String>,
    /// Account names Plex reports that no Tautulli user name matches.
    pub plex_only_names: Vec<String>,
}

/// Join Plex's `/accounts` and Tautulli's `get_users` on account name. Account id 0
/// is excluded on both sides: a placeholder on each system, never a person.
pub fn build_account_id_map(plex_accounts: &[Value], tautulli_users: &[Value]) -> AccountIdMap {
    let named = |records: &[Value], id: &str, name: &str| {
        let mut by_name: HashMap<String, i64> = HashMap::new();
        for record in records {
            let (account_id, account_name) = (get(record, id), get(record, name));
            if account_id.is_null() || number(account_id) == Some(0.0) || !truthy(account_name) {
                continue;
            }
            if let (Some(account_id), Some(account_name)) =
                (account_id.as_i64(), account_name.as_str())
            {
                by_name.insert(account_name.to_string(), account_id);
            }
        }
        by_name
    };
    let plex_by_name = named(plex_accounts, "id", "name");
    let tautulli_by_name = named(tautulli_users, "user_id", "username");
    let by_tautulli_user_id = tautulli_by_name
        .iter()
        .filter_map(|(name, user_id)| plex_by_name.get(name).map(|plex_id| (*user_id, *plex_id)))
        .collect();
    let plex_names: BTreeSet<&String> = plex_by_name.keys().collect();
    let tautulli_names: BTreeSet<&String> = tautulli_by_name.keys().collect();
    AccountIdMap {
        by_tautulli_user_id,
        tautulli_only_names: tautulli_names
            .difference(&plex_names)
            .map(|n| (*n).clone())
            .collect(),
        plex_only_names: plex_names
            .difference(&tautulli_names)
            .map(|n| (*n).clone())
            .collect(),
    }
}

/// What one Tautulli match sweep touched.
#[derive(Debug, Default)]
pub struct TautulliMatchStats {
    pub rows_seen: usize,
    /// `id` is null: a session Tautulli has not yet written to its history. Skipped,
    /// not counted as unmatched.
    pub rows_in_progress: usize,
    pub rows_matched: usize,
    /// Already carries this exact tautulli_id from a prior run.
    pub rows_already_matched: usize,
    /// A completed row this sweep could not attach to any play.
    pub rows_unmatched: usize,
    /// `plays` rows carrying no Tautulli data at all, counted fresh after the sweep.
    pub plays_without_tautulli_data: i64,
}

struct Candidate {
    history_key: String,
    viewed_at: f64,
    tautulli_id: Option<i64>,
}

type Bucket = (String, i64, Option<String>);

/// Every existing play, bucketed by the hard-key tuple a Tautulli row must share to
/// be a candidate: `(item_id, plex_account_id, client_identifier)`.
fn candidate_plays(conn: &Connection) -> Result<HashMap<Bucket, Vec<Candidate>>, StoreError> {
    let mut buckets: HashMap<Bucket, Vec<Candidate>> = HashMap::new();
    let mut stmt = conn.prepare(
        "SELECT history_key, item_id, plex_account_id, client_identifier, viewed_at, \
         tautulli_id FROM plays ORDER BY viewed_at",
    )?;
    for row in stmt.query_map([], |row| {
        Ok((
            (
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, Option<String>>(3)?,
            ),
            Candidate {
                history_key: row.get(0)?,
                viewed_at: row.get::<_, f64>(4)?,
                tautulli_id: row.get(5)?,
            },
        ))
    })? {
        let (key, candidate) = row?;
        buckets.entry(key).or_default().push(candidate);
    }
    Ok(buckets)
}

/// Enrich existing `plays` rows with Tautulli's `ip`, `percent_complete`,
/// `paused_counter` and `seconds_watched`. The match is composite: hard-match on
/// `(item_id via rating_key, plex_account_id, client_identifier)`, then take the
/// play whose `viewed_at` is nearest the row's `stopped` within
/// [`MATCH_WINDOW_SECONDS`]. Tautulli's own row `id` is stored on the play it lands
/// on, which makes a re-run idempotent and stops one row enriching two plays.
pub fn match_tautulli_history(
    conn: &mut Connection,
    source: &dyn TautulliSource,
    account_ids: &AccountIdMap,
) -> Result<TautulliMatchStats, StoreError> {
    let mut stats = TautulliMatchStats::default();
    let mut resolved: HashMap<String, String> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT rating_key, item_id FROM plex_items")?;
        for row in stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (rating_key, item_id) = row?;
            resolved.insert(rating_key, item_id);
        }
    }
    let mut candidates = candidate_plays(conn)?;
    let rows = source.history()?;

    let tx = conn.transaction()?;
    for row in &rows {
        stats.rows_seen += 1;
        let row_id = get(row, "id");
        if row_id.is_null() {
            stats.rows_in_progress += 1;
            continue;
        }
        let (raw_rating_key, user_id, stopped) = (
            get(row, "rating_key"),
            get(row, "user_id"),
            get(row, "stopped"),
        );
        if raw_rating_key.is_null()
            || raw_rating_key.as_str() == Some("")
            || user_id.is_null()
            || stopped.is_null()
        {
            stats.rows_unmatched += 1;
            continue;
        }
        let Some(item_id) = resolved.get(&py_str(raw_rating_key)) else {
            stats.rows_unmatched += 1;
            continue;
        };
        // An empty machine id is absent, as an empty Plex clientIdentifier is.
        let machine_id = Some(get(row, "machine_id"))
            .filter(|value| truthy(value))
            .map(py_str);
        let Some(user) = user_id.as_i64() else {
            stats.rows_unmatched += 1;
            continue;
        };
        let mapped = account_ids
            .by_tautulli_user_id
            .get(&user)
            .copied()
            .unwrap_or(user);
        let (Some(row_number), Some(stopped_at)) = (row_id.as_i64(), number(stopped)) else {
            stats.rows_unmatched += 1;
            continue;
        };

        let key = (item_id.clone(), mapped, machine_id);
        let mut best: Option<(f64, usize)> = None;
        let bucket = candidates.get(&key);
        for (index, play) in bucket.into_iter().flatten().enumerate() {
            if play
                .tautulli_id
                .is_some_and(|claimant| claimant != row_number)
            {
                // Already matched to a different Tautulli row — not a candidate.
                continue;
            }
            let delta = (play.viewed_at - stopped_at).abs();
            if delta > MATCH_WINDOW_SECONDS {
                continue;
            }
            if best.is_none_or(|(nearest, _)| delta < nearest) {
                best = Some((delta, index));
            }
        }
        let Some((_, index)) = best else {
            stats.rows_unmatched += 1;
            continue;
        };
        let play = &mut candidates.get_mut(&key).expect("the bucket just read")[index];
        if play.tautulli_id == Some(row_number) {
            // Idempotent re-run: this row already landed on this play.
            stats.rows_already_matched += 1;
            continue;
        }
        // `seconds_watched` is `duration`, copied verbatim: duration is already net
        // of paused time, so subtracting `paused_counter` would do it twice.
        tx.execute(
            "UPDATE plays SET ip = ?, percent_complete = ?, paused_counter = ?, \
             seconds_watched = ?, tautulli_id = ? WHERE history_key = ?",
            rusqlite::params_from_iter([
                sql(get(row, "ip_address")),
                sql(get(row, "percent_complete")),
                sql(get(row, "paused_counter")),
                sql(get(row, "duration")),
                sql(row_id),
                rusqlite::types::Value::Text(play.history_key.clone()),
            ]),
        )?;
        play.tautulli_id = Some(row_number);
        stats.rows_matched += 1;
    }
    stats.plays_without_tautulli_data = tx.query_row(
        "SELECT count(*) FROM plays WHERE tautulli_id IS NULL",
        [],
        |row| row.get(0),
    )?;
    tx.commit()?;
    Ok(stats)
}

pub const NAME_INGEST: &str = "ingest-plays";
pub const NAME_TAUTULLI: &str = "enrich-tautulli-plays";

/// The lines `plexdb ingest-plays` prints.
pub fn describe_ingest(stats: &PlaysStats) -> Vec<String> {
    let mut lines = vec![format!(
        "ingested {} play(s) from {} event(s) seen, {} already recorded",
        stats.plays_written, stats.events_seen, stats.already_recorded
    )];
    if stats.unresolved_rating_key > 0 {
        lines.push(format!(
            "{} event(s) had a rating key not in the walk's map, skipped — run `plexdb walk` \
             to catch up",
            stats.unresolved_rating_key
        ));
    }
    if stats.unresolved_device > 0 {
        lines.push(format!(
            "{} event(s) had a device id not in Plex's device list; recorded with no client \
             identifier or platform",
            stats.unresolved_device
        ));
    }
    lines
}

/// The lines `plexdb enrich-tautulli-plays` prints after its account-name notes.
pub fn describe_match(stats: &TautulliMatchStats) -> Vec<String> {
    vec![
        format!(
            "matched {} Tautulli row(s) to plays ({} row(s) seen, {} in-progress skipped, {} \
             already matched)",
            stats.rows_matched, stats.rows_seen, stats.rows_in_progress, stats.rows_already_matched
        ),
        format!(
            "{} Tautulli row(s) found no matching play; {} play(s) still carry no Tautulli data",
            stats.rows_unmatched, stats.plays_without_tautulli_data
        ),
    ]
}
