//! `repair-identities`: splitting identities that fused two unrelated titles into one
//! row (issue #23).
//!
//! Before schema version 5, `external_ids` was keyed on `(ns, value)` alone. TMDB and
//! TVDB number movies, shows and episodes in separate lists that all start at 1, so a
//! bare `tmdb://1678` claimed one row for a film and an unrelated show. Version 5
//! puts the media kind in the key, so no new fusion can happen; it cannot undo the
//! recorded ones, which takes an explicit pass that deletes the fused rows and lets
//! the walk derive them again from Plex.
//!
//! **A fused identity is one whose Plex records are not all the same media kind.**
//! Answering that needs Plex: `plex_items` records where Plex keeps a title but not
//! what it is, and `items.type` describes the identity, the very thing a fusion
//! corrupted. Two ids in one namespace is the wrong test, and using it churns the
//! store forever (*The Animatrix* carries nine TMDB ids on one rating key).
//!
//! **A play is never deleted and re-inserted, only moved.** From schema version 5 a
//! `plays` row carries its own `rating_key`, which places it again after a split. A
//! row written before that has no rating key, so once its identity splits there is no
//! way to say which half it belongs to: those are deleted and the ingest cursor is
//! rewound to the oldest of them, to read them back from history.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use plexdb_store::StoreError;
use rusqlite::{Connection, OptionalExtension, params_from_iter};

use crate::plex::{PlexSource, section_passes};
use crate::pyval::py_str;
use crate::walk::{WalkStats, walk_all};

pub const NAME: &str = "repair-identities";

/// How many ids to name in one `IN (...)` clause: SQLite caps the bound parameters
/// per statement, and a repair on a large library can find thousands.
const CHUNK: usize = 500;

/// What one repair pass found and touched.
#[derive(Debug, Default)]
pub struct RepairStats {
    /// Identities whose Plex records span more than one media kind.
    pub fused_found: usize,
    /// Plays carried across the split by their own `rating_key`.
    pub plays_repointed: usize,
    /// Plays that went with their identity and must be re-read from history.
    pub plays_dropped: i64,
    /// Plays whose `rating_key` no longer resolves after the re-walk.
    pub plays_orphaned: i64,
    /// The `viewed_at` the ingest cursor was rewound to, or `None` when nothing had to
    /// be dropped and the cursor was left alone.
    pub cursor_rewound_to: Option<i64>,
    pub walk: Option<WalkStats>,
}

/// Every rating key Plex currently holds, and the media kind it holds it as.
pub fn kinds_by_rating_key(source: &dyn PlexSource) -> Result<HashMap<String, String>, StoreError> {
    let mut kinds = HashMap::new();
    for section in source.sections()? {
        for (kind, plex_type) in section_passes(&section.kind) {
            for record in source.items(&section.key, *plex_type)? {
                let rating_key = crate::pyval::get(&record, "ratingKey");
                if crate::pyval::truthy(rating_key) {
                    kinds.insert(py_str(rating_key), (*kind).to_string());
                }
            }
        }
    }
    Ok(kinds)
}

/// Every identity whose Plex records are not all the same media kind, sorted. A
/// rating key Plex no longer holds has no kind to compare and is skipped.
pub fn fused_item_ids(
    conn: &Connection,
    kinds: &HashMap<String, String>,
) -> Result<Vec<String>, StoreError> {
    let mut by_item: BTreeMap<String, BTreeSet<&String>> = BTreeMap::new();
    let mut stmt = conn.prepare("SELECT rating_key, item_id FROM plex_items")?;
    for row in stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })? {
        let (rating_key, item_id) = row?;
        if let Some(kind) = kinds.get(&rating_key) {
            by_item.entry(item_id).or_default().insert(kind);
        }
    }
    Ok(by_item
        .into_iter()
        .filter(|(_, seen)| seen.len() > 1)
        .map(|(item_id, _)| item_id)
        .collect())
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(",")
}

/// `SELECT <aggregate> FROM plays WHERE <filter> AND item_id IN (chunk)`, summed or
/// minimised over chunks.
fn plays_matching(
    conn: &Connection,
    item_ids: &[String],
    filter: &str,
) -> Result<(i64, Option<i64>), StoreError> {
    let (mut count, mut oldest): (i64, Option<i64>) = (0, None);
    for chunk in item_ids.chunks(CHUNK) {
        let (n, min): (i64, Option<i64>) = conn.query_row(
            &format!(
                "SELECT COUNT(*), MIN(viewed_at) FROM plays WHERE {filter} AND item_id IN ({})",
                placeholders(chunk.len())
            ),
            params_from_iter(chunk),
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        count += n;
        oldest = match (oldest, min) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        };
    }
    Ok((count, oldest))
}

/// Move every play onto whatever identity its own rating key now holds. One
/// statement over the whole table; unconditional and idempotent, which is what makes
/// the repair crash-safe: it asks only "does this play's rating key resolve somewhere
/// other than where the play is?". A rating key with no `plex_items` row is a title
/// that has left the library, and its play stays where it is.
pub fn repoint_plays(conn: &Connection) -> Result<usize, StoreError> {
    Ok(conn.execute(
        "UPDATE plays SET item_id = (\
             SELECT p.item_id FROM plex_items p WHERE p.rating_key = plays.rating_key\
         ) \
         WHERE rating_key IS NOT NULL \
           AND EXISTS (SELECT 1 FROM plex_items p WHERE p.rating_key = plays.rating_key) \
           AND item_id <> (\
             SELECT p.item_id FROM plex_items p WHERE p.rating_key = plays.rating_key\
         )",
        [],
    )?)
}

/// Plays still on `item_ids` whose rating key resolves nowhere.
fn orphaned_plays(conn: &Connection, item_ids: &[String]) -> Result<i64, StoreError> {
    let mut total = 0;
    for chunk in item_ids.chunks(CHUNK) {
        let n: i64 = conn.query_row(
            &format!(
                "SELECT COUNT(*) FROM plays \
                 WHERE rating_key IS NOT NULL AND item_id IN ({}) \
                   AND NOT EXISTS (SELECT 1 FROM plex_items p WHERE p.rating_key = plays.rating_key)",
                placeholders(chunk.len())
            ),
            params_from_iter(chunk),
            |row| row.get(0),
        )?;
        total += n;
    }
    Ok(total)
}

/// Split every fused identity, carrying its plays across where it can. The fused
/// `items` row is left standing while its `plex_items` and `external_ids` rows are
/// deleted — enough for the walk to derive each rating key afresh, and not enough to
/// cascade its plays away. Re-running after a failure repairs it.
pub fn repair(
    conn: &mut Connection,
    source: &dyn PlexSource,
    source_roots: &[String],
) -> Result<RepairStats, StoreError> {
    let mut stats = RepairStats::default();
    let item_ids = fused_item_ids(conn, &kinds_by_rating_key(source)?)?;
    stats.fused_found = item_ids.len();
    if item_ids.is_empty() {
        // Still worth a pass: an earlier run may have died between deleting an
        // identity's plex_items rows and moving its plays off it.
        let tx = conn.transaction()?;
        stats.plays_repointed = repoint_plays(&tx)?;
        tx.commit()?;
        return Ok(stats);
    }

    let (dropped, oldest) = plays_matching(conn, &item_ids, "rating_key IS NULL")?;
    stats.plays_dropped = dropped;
    // Only the plays that cannot be carried across justify a rewind.
    let oldest = if dropped > 0 { oldest } else { None };

    let tx = conn.transaction()?;
    for chunk in item_ids.chunks(CHUNK) {
        let marks = placeholders(chunk.len());
        // Deliberately NOT `DELETE FROM items`: that cascades to `plays`.
        tx.execute(
            &format!("DELETE FROM plex_items WHERE item_id IN ({marks})"),
            params_from_iter(chunk),
        )?;
        tx.execute(
            &format!("DELETE FROM external_ids WHERE item_id IN ({marks})"),
            params_from_iter(chunk),
        )?;
        tx.execute(
            &format!("DELETE FROM plays WHERE rating_key IS NULL AND item_id IN ({marks})"),
            params_from_iter(chunk),
        )?;
    }
    if let Some(oldest) = oldest {
        // Only ever moves the cursor backwards: advancing it would skip events the
        // store still has no row for.
        let current: Option<i64> = tx
            .query_row(
                "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1",
                [],
                |row| row.get(0),
            )
            .optional()?;
        match current {
            None => {
                tx.execute(
                    "INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, ?)",
                    [oldest],
                )?;
                stats.cursor_rewound_to = Some(oldest);
            }
            Some(at) if oldest < at => {
                tx.execute(
                    "UPDATE plays_ingest_cursor SET since_viewed_at = ? WHERE id = 1",
                    [oldest],
                )?;
                stats.cursor_rewound_to = Some(oldest);
            }
            Some(at) => stats.cursor_rewound_to = Some(at),
        }
    }
    tx.commit()?;

    stats.walk = Some(walk_all(conn, source, source_roots, None)?);

    // After the walk, and only then: the identity a rating key resolves to did not
    // exist until it ran.
    let tx = conn.transaction()?;
    stats.plays_repointed = repoint_plays(&tx)?;
    stats.plays_orphaned = orphaned_plays(&tx, &item_ids)?;
    for chunk in item_ids.chunks(CHUNK) {
        // The fused rows held the plays while the walk ran. Delete only the ones
        // nothing points at any more; a row still holding plays is one whose rating
        // key left Plex, and taking it would take that viewing history with it.
        tx.execute(
            &format!(
                "DELETE FROM items WHERE item_id IN ({}) \
                 AND NOT EXISTS (SELECT 1 FROM plays WHERE plays.item_id = items.item_id) \
                 AND NOT EXISTS (SELECT 1 FROM plex_items WHERE plex_items.item_id = items.item_id)",
                placeholders(chunk.len())
            ),
            params_from_iter(chunk),
        )?;
    }
    tx.commit()?;
    Ok(stats)
}

/// The `repair-identities` step: what a person sees. `dry_run` lists the fused
/// identities and changes nothing, but still reads Plex.
pub fn run(
    conn: &mut Connection,
    source: &dyn PlexSource,
    source_roots: &[String],
    dry_run: bool,
) -> Result<Vec<String>, StoreError> {
    if dry_run {
        let fused = fused_item_ids(conn, &kinds_by_rating_key(source)?)?;
        let mut lines = vec![format!(
            "{} identit(ies) cover Plex records of more than one media kind — each one is \
             two unrelated titles fused into a single row",
            fused.len()
        )];
        lines.extend(fused.iter().take(20).map(|item_id| format!("  {item_id}")));
        if fused.len() > 20 {
            lines.push(format!("  ... and {} more", fused.len() - 20));
        }
        return Ok(lines);
    }
    let stats = repair(conn, source, source_roots)?;
    if stats.fused_found == 0 {
        return Ok(vec!["no fused identities found — nothing to repair".into()]);
    }
    let written = stats.walk.as_ref().map_or(0, |walk| walk.titles_written);
    let mut lines = vec![format!(
        "deleted {} fused identit(ies) and re-walked Plex: {written} title(s) rewritten",
        stats.fused_found
    )];
    if stats.plays_repointed > 0 {
        lines.push(format!(
            "{} play(s) carried across by their own rating key — no history re-read needed \
             for those",
            stats.plays_repointed
        ));
    }
    if stats.plays_orphaned > 0 {
        lines.push(format!(
            "{} play(s) could not be placed: their rating key is no longer in the library, \
             so the title left Plex between the snapshot and the walk",
            stats.plays_orphaned
        ));
    }
    if stats.plays_dropped > 0 {
        lines.push(format!(
            "{} play(s) predate schema v5 and carry no rating key, so nothing records which \
             title they came from; the ingest cursor is rewound to {} — run `plexdb \
             ingest-plays` to read them back, then `plexdb enrich-tautulli-plays` if \
             Tautulli is configured",
            stats.plays_dropped,
            stats
                .cursor_rewound_to
                .map_or("None".into(), |at| at.to_string())
        ));
    }
    Ok(lines)
}
