//! `extras-near`: fold near-duplicate extras into one item (ADR-0030).
//!
//! Two encodes of one video are two files with different bytes, so the walk's SHA-256 match
//! cannot join them. Once both are in the Frame index their frame vectors say they are the
//! same video. For each extra whose index entry is new since its last comparison, this
//! compares its vectors with every other indexed extra of its library. A pair at or above
//! `PLEXDB_EXTRAS_NEAR` becomes one item: the later item's files move to the earlier, its
//! `items` and `extra_item` rows are deleted, an `extra_merge` row records the fold, and its
//! frames leave `frames.db`. A Split decision (`extras_decisions`) gives its file its own item
//! back under the same `fs:` id and writes the `split` cursor row below, so this pass never
//! folds that item again.
//!
//! **Score.** For two items, the mean over one item's frames of the best cosine against the
//! other's frames, taken both ways, and the lower of the two. A cut-down copy of a video
//! scores high one way and low the other, and is not the same video.
//!
//! **Which pairs are scored.** Only pairs whose last-frame times differ by at most
//! [`DURATION_SLACK`] and whose mean vectors have a cosine of at least [`MEAN_GATE`]; scoring
//! every pair of a library is quadratic in frames and the gate costs one dot product.
//!
//! **Which one is kept.** The organized one, else the earlier `first_seen`, else the smaller
//! `item_id`. A merge never hides an organized item behind an unorganized one.
//!
//! The vectors are read and compared without the writer lock ([`prepare`]); the lock is held
//! for the `extra_merge` writes alone ([`apply`]). `frames.db` is cleaned afterwards by
//! [`clean_frames`] under the harvester's lock, and a pass that finds the harvester running
//! leaves it for the next tick.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use plexdb_store::StoreError;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};

use crate::frames::{FRAME_DIMS, HarvestLock, delete_vectors, load_vec_extension, open_frames};

pub const NAME: &str = "extras-near";
pub const THRESHOLD_VAR: &str = "PLEXDB_EXTRAS_NEAR";
pub const DEFAULT_THRESHOLD: f64 = 0.97;

/// The `enrichment_cursor` triple recording when an extra's index entry was last compared.
pub const CURSOR_NAMESPACE: &str = "extras";
pub const CURSOR_SOURCE: &str = "near";
const COMPARED: &str = "compared";
/// A person split this item from the items it was folded into: never fold it again.
pub const SPLIT: &str = "split";

/// Last-frame times further apart than this (milliseconds, or this share of the longer one)
/// are different videos.
pub const DURATION_SLACK: (i64, f64) = (2000, 0.05);
/// Mean vectors with a lower cosine are not scored.
pub const MEAN_GATE: f64 = 0.9;

/// `PLEXDB_EXTRAS_NEAR`: a number from 0 to 1, default [`DEFAULT_THRESHOLD`].
pub fn threshold_from(value: Option<&str>) -> Result<f64, StoreError> {
    let Some(text) = value.map(str::trim).filter(|text| !text.is_empty()) else {
        return Ok(DEFAULT_THRESHOLD);
    };
    match text.parse::<f64>() {
        Ok(number) if (0.0..=1.0).contains(&number) => Ok(number),
        _ => Err(StoreError::Config(format!(
            "{THRESHOLD_VAR} must be a number from 0 to 1, got {text:?}"
        ))),
    }
}

/// One extra's frames, as unit vectors.
struct Frames {
    vectors: Vec<Vec<f32>>,
    mean: Vec<f32>,
    last_ms: i64,
}

fn unit(raw: &[i8]) -> Option<Vec<f32>> {
    let floats: Vec<f32> = raw.iter().map(|b| f32::from(*b)).collect();
    let norm = floats.iter().map(|v| v * v).sum::<f32>().sqrt();
    (norm > 0.0).then(|| floats.iter().map(|v| v / norm).collect())
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

fn load_frames(
    frames: &Connection,
    item_id: &str,
    last_ms: i64,
) -> Result<Option<Frames>, StoreError> {
    let mut stmt = frames.prepare("SELECT vec FROM frame_vec WHERE item_id = ?1")?;
    let blobs: Vec<Vec<u8>> = stmt
        .query_map([item_id], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let vectors: Vec<Vec<f32>> = blobs
        .iter()
        .filter(|blob| blob.len() == FRAME_DIMS)
        .filter_map(|blob| unit(&blob.iter().map(|b| *b as i8).collect::<Vec<_>>()))
        .collect();
    if vectors.is_empty() {
        return Ok(None);
    }
    let mut mean = vec![0f32; FRAME_DIMS];
    for vector in &vectors {
        for (sum, value) in mean.iter_mut().zip(vector) {
            *sum += value;
        }
    }
    let norm = mean.iter().map(|v| v * v).sum::<f32>().sqrt();
    if norm > 0.0 {
        mean.iter_mut().for_each(|v| *v /= norm);
    }
    Ok(Some(Frames {
        vectors,
        mean,
        last_ms,
    }))
}

fn one_way(from: &Frames, to: &Frames) -> f64 {
    let total: f32 = from
        .vectors
        .iter()
        .map(|a| {
            to.vectors
                .iter()
                .map(|b| dot(a, b))
                .fold(f32::MIN, f32::max)
        })
        .sum();
    f64::from(total) / from.vectors.len() as f64
}

/// The score of two items' frames, or `None` when the gates rule the pair out.
fn score(a: &Frames, b: &Frames) -> Option<f64> {
    if a.last_ms <= 0 || b.last_ms <= 0 {
        return None;
    }
    let longer = a.last_ms.max(b.last_ms) as f64;
    let slack = (DURATION_SLACK.0 as f64).max(longer * DURATION_SLACK.1);
    if (a.last_ms - b.last_ms).abs() as f64 > slack {
        return None;
    }
    if f64::from(dot(&a.mean, &b.mean)) < MEAN_GATE {
        return None;
    }
    Some(one_way(a, b).min(one_way(b, a)))
}

/// One extra the comparison may involve.
#[derive(Debug, Clone)]
struct Candidate {
    item_id: String,
    library: String,
    organized: bool,
    first_seen: String,
    indexed_at: String,
    last_ms: i64,
}

/// A pair at or above the threshold, `merged` to be folded into `kept`.
#[derive(Debug, Clone, PartialEq)]
pub struct Pair {
    pub merged: String,
    pub kept: String,
    pub score: f64,
}

/// What a comparison found: the pairs to fold and the entries it looked at.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Prepared {
    pub pairs: Vec<Pair>,
    /// `(item_id, indexed_at)` of every extra compared.
    pub compared: Vec<(String, String)>,
    pub candidates: usize,
}

fn kept_first(a: &Candidate, b: &Candidate) -> bool {
    (!a.organized, &a.first_seen, &a.item_id) < (!b.organized, &b.first_seen, &b.item_id)
}

fn load(
    cache: &mut HashMap<String, Option<Frames>>,
    frames: &Connection,
    candidate: &Candidate,
) -> Result<(), StoreError> {
    if !cache.contains_key(&candidate.item_id) {
        let loaded = load_frames(frames, &candidate.item_id, candidate.last_ms)?;
        cache.insert(candidate.item_id.clone(), loaded);
    }
    Ok(())
}

/// Compare every extra whose index entry changed since it was last compared. Only reads.
pub fn prepare(
    store: &Connection,
    frames: &Connection,
    threshold: f64,
) -> Result<Prepared, StoreError> {
    let indexed: HashMap<String, (String, i64)> = {
        let mut stmt = frames.prepare(
            "SELECT item_id, indexed_at, COALESCE(duration_ms, 0) FROM frame_index_state \
             WHERE error IS NULL AND model IS NOT NULL",
        )?;
        stmt.query_map([], |row| Ok((row.get(0)?, (row.get(1)?, row.get(2)?))))?
            .collect::<Result<_, _>>()?
    };
    let split: HashSet<String> = {
        let mut stmt = store.prepare(
            "SELECT item_id FROM enrichment_cursor WHERE namespace = ?1 AND source = ?2 \
             AND key = ?3",
        )?;
        stmt.query_map(params![CURSOR_NAMESPACE, CURSOR_SOURCE, SPLIT], |row| {
            row.get(0)
        })?
        .collect::<Result<_, _>>()?
    };
    let compared: HashMap<String, String> = {
        let mut stmt = store.prepare(
            "SELECT item_id, fetched_at FROM enrichment_cursor WHERE namespace = ?1 \
             AND source = ?2 AND key = ?3",
        )?;
        stmt.query_map(params![CURSOR_NAMESPACE, CURSOR_SOURCE, COMPARED], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect::<Result<_, _>>()?
    };
    let mut candidates: Vec<Candidate> = Vec::new();
    {
        let mut stmt =
            store.prepare("SELECT item_id, library, organized, first_seen FROM extra_item")?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, String>(3)?,
            ))
        })?;
        for row in rows {
            let (item_id, library, organized, first_seen) = row?;
            let Some((indexed_at, last_ms)) = indexed.get(&item_id) else {
                continue;
            };
            candidates.push(Candidate {
                item_id,
                library,
                organized: organized == 1,
                first_seen,
                indexed_at: indexed_at.clone(),
                last_ms: *last_ms,
            });
        }
    }
    let mut result = Prepared {
        candidates: candidates.len(),
        ..Prepared::default()
    };
    let mut by_library: BTreeMap<&str, Vec<&Candidate>> = BTreeMap::new();
    for candidate in &candidates {
        by_library
            .entry(candidate.library.as_str())
            .or_default()
            .push(candidate);
    }
    let mut cache: HashMap<String, Option<Frames>> = HashMap::new();
    let mut seen_pairs: HashSet<(String, String)> = HashSet::new();
    for group in by_library.values() {
        for x in group {
            if compared.get(&x.item_id) == Some(&x.indexed_at) {
                continue;
            }
            result
                .compared
                .push((x.item_id.clone(), x.indexed_at.clone()));
            if split.contains(&x.item_id) {
                continue;
            }
            load(&mut cache, frames, x)?;
            for y in group {
                if x.item_id == y.item_id || split.contains(&y.item_id) {
                    continue;
                }
                let key = if x.item_id < y.item_id {
                    (x.item_id.clone(), y.item_id.clone())
                } else {
                    (y.item_id.clone(), x.item_id.clone())
                };
                if !seen_pairs.insert(key) {
                    continue;
                }
                load(&mut cache, frames, y)?;
                let (Some(Some(fx)), Some(Some(fy))) =
                    (cache.get(&x.item_id), cache.get(&y.item_id))
                else {
                    continue;
                };
                let Some(found) = score(fx, fy) else { continue };
                if found < threshold {
                    continue;
                }
                let (kept, merged) = if kept_first(x, y) { (x, y) } else { (y, x) };
                result.pairs.push(Pair {
                    merged: merged.item_id.clone(),
                    kept: kept.item_id.clone(),
                    score: found,
                });
            }
        }
    }
    result.pairs.sort_by(|a, b| {
        b.score
            .total_cmp(&a.score)
            .then_with(|| a.merged.cmp(&b.merged))
    });
    Ok(result)
}

/// What [`apply`] wrote.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Stats {
    pub folded: usize,
    pub compared: usize,
}

fn kept_of(tx: &Connection, item_id: &str) -> Result<Option<String>, StoreError> {
    Ok(tx
        .query_row(
            "SELECT kept_item_id FROM extra_merge WHERE merged_item_id = ?1",
            [item_id],
            |row| row.get(0),
        )
        .optional()?)
}

/// Write what [`prepare`] found: per fold, an `extra_merge` row and the folded item's files
/// moved to the kept one, and a cursor row for each entry compared. A pair whose items are no
/// longer both extras, or whose later item is already folded, is dropped; a kept item that was
/// itself folded since resolves to the item it was folded into.
pub fn apply(conn: &mut Connection, prepared: &Prepared, now: &str) -> Result<Stats, StoreError> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut stats = Stats::default();
    let exists = |tx: &Connection, id: &str| -> Result<bool, StoreError> {
        Ok(tx.query_row(
            "SELECT EXISTS (SELECT 1 FROM extra_item WHERE item_id = ?1)",
            [id],
            |row| row.get(0),
        )?)
    };
    for pair in &prepared.pairs {
        if !exists(&tx, &pair.merged)? || !exists(&tx, &pair.kept)? {
            continue;
        }
        if kept_of(&tx, &pair.merged)?.is_some() {
            continue;
        }
        let mut kept = pair.kept.clone();
        while let Some(next) = kept_of(&tx, &kept)? {
            kept = next;
        }
        if kept == pair.merged {
            continue;
        }
        // `prepare` read `organized` before the lock; a person may have organized the later
        // item since, and an organized item is never hidden behind an unorganized one.
        let organized = |tx: &Connection, id: &str| -> Result<i64, StoreError> {
            Ok(tx.query_row(
                "SELECT organized FROM extra_item WHERE item_id = ?1",
                [id],
                |row| row.get(0),
            )?)
        };
        if organized(&tx, &pair.merged)? > organized(&tx, &kept)? {
            continue;
        }
        tx.execute(
            "INSERT INTO extra_merge (merged_item_id, kept_item_id, score, merged_at) \
             VALUES (?1, ?2, ?3, ?4)",
            params![pair.merged, kept, pair.score, now],
        )?;
        let library: String = tx.query_row(
            "SELECT library FROM extra_item WHERE item_id = ?1",
            [&kept],
            |row| row.get(0),
        )?;
        tx.execute(
            "UPDATE extra_file SET item_id = ?1, primary_file = 0 WHERE item_id = ?2",
            params![kept, pair.merged],
        )?;
        tx.execute("DELETE FROM extra_item WHERE item_id = ?1", [&pair.merged])?;
        tx.execute("DELETE FROM items WHERE item_id = ?1", [&pair.merged])?;
        crate::extras_walk::choose_primaries(&tx, &library)?;
        tx.execute(
            "UPDATE extra_merge SET kept_item_id = ?1 WHERE kept_item_id = ?2",
            params![kept, pair.merged],
        )?;
        stats.folded += 1;
    }
    for (item_id, indexed_at) in &prepared.compared {
        if !exists(&tx, item_id)? {
            continue;
        }
        tx.execute(
            "INSERT OR REPLACE INTO enrichment_cursor \
             (item_id, namespace, source, key, fetched_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                item_id,
                CURSOR_NAMESPACE,
                CURSOR_SOURCE,
                COMPARED,
                indexed_at
            ],
        )?;
        stats.compared += 1;
    }
    tx.commit()?;
    Ok(stats)
}

/// Remove every folded extra's rows from `frames.db`, so a search finds the video once and the
/// harvester has nothing of it to keep. Takes the harvester's slot; returns how many items it
/// cleaned, or `None` when a harvester holds the slot.
pub fn clean_frames(frames_path: &Path, store: &Connection) -> Result<Option<usize>, StoreError> {
    let merged: Vec<String> = store
        .prepare("SELECT merged_item_id FROM extra_merge")?
        .query_map([], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    if merged.is_empty() || !frames_path.exists() {
        return Ok(Some(0));
    }
    let Ok(_lock) = HarvestLock::acquire(frames_path) else {
        return Ok(None);
    };
    let mut frames = open_frames(frames_path)?;
    let mut cleaned = 0;
    for item_id in &merged {
        let held: bool = frames.query_row(
            "SELECT EXISTS (SELECT 1 FROM frame_index_state WHERE item_id = ?1) \
             OR EXISTS (SELECT 1 FROM frame_queue WHERE item_id = ?1)",
            [item_id],
            |row| row.get(0),
        )?;
        if !held {
            continue;
        }
        let tx = frames.transaction()?;
        delete_vectors(&tx, item_id)?;
        for table in ["cuts", "shots", "frame_index_state", "frame_queue"] {
            tx.execute(
                &format!("DELETE FROM {table} WHERE item_id = ?1"),
                [item_id],
            )?;
        }
        tx.commit()?;
        cleaned += 1;
    }
    Ok(Some(cleaned))
}

/// A report line.
pub fn describe(prepared: &Prepared, stats: &Stats, cleaned: Option<usize>) -> String {
    let cleaned = match cleaned {
        Some(count) => format!("{count} folded item(s) removed from the Frame index"),
        None => "the Frame index is busy, folded items stay in it until the next tick".into(),
    };
    format!(
        "{NAME}: {} indexed extra(s), {} compared, {} folded; {cleaned}",
        prepared.candidates, stats.compared, stats.folded
    )
}

/// Ensure the vec extension is registered before a read-only frames connection is opened.
pub fn open_frames_readonly(path: &Path) -> Result<Connection, StoreError> {
    load_vec_extension();
    let conn = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|err| StoreError::Store(format!("cannot open {}: {err}", path.display())))?;
    conn.busy_timeout(std::time::Duration::from_millis(
        plexdb_store::BUSY_TIMEOUT_MS,
    ))?;
    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frames_of(vectors: Vec<Vec<f32>>, last_ms: i64) -> Frames {
        let mut mean = vec![0f32; vectors[0].len()];
        for vector in &vectors {
            for (sum, value) in mean.iter_mut().zip(vector) {
                *sum += value;
            }
        }
        let norm = mean.iter().map(|v| v * v).sum::<f32>().sqrt();
        mean.iter_mut().for_each(|v| *v /= norm);
        Frames {
            vectors,
            mean,
            last_ms,
        }
    }

    #[test]
    fn the_threshold_is_a_number_from_zero_to_one() {
        assert_eq!(threshold_from(None).unwrap(), DEFAULT_THRESHOLD);
        assert_eq!(threshold_from(Some(" ")).unwrap(), DEFAULT_THRESHOLD);
        assert_eq!(threshold_from(Some("0.9")).unwrap(), 0.9);
        assert!(threshold_from(Some("1.5")).is_err());
        assert!(threshold_from(Some("near")).is_err());
    }

    #[test]
    fn a_cut_down_copy_scores_low_one_way_and_so_low_overall() {
        let full = frames_of(vec![vec![1.0, 0.0], vec![0.0, 1.0]], 10_000);
        let part = frames_of(vec![vec![1.0, 0.0]], 10_000);
        assert!((one_way(&part, &full) - 1.0).abs() < 1e-6);
        assert!((one_way(&full, &part) - 0.5).abs() < 1e-6);
        assert!(score(&full, &part).is_none(), "the mean gate rules it out");
    }

    #[test]
    fn videos_of_different_length_are_not_scored() {
        let a = frames_of(vec![vec![1.0, 0.0]], 10_000);
        let b = frames_of(vec![vec![1.0, 0.0]], 20_000);
        assert!(score(&a, &b).is_none());
        let c = frames_of(vec![vec![1.0, 0.0]], 10_400);
        assert!((score(&a, &c).unwrap() - 1.0).abs() < 1e-6);
    }
}
