//! `plexdb harvest-frames` — fill the Frame index from the library, through Plex
//! (`plexdb/frame_harvest.py`).
//!
//! For one title it finds the Cuts (scdet's scene changes and the file's chapter
//! marks), picks the frames to sample, embeds them through the encoder client
//! (ADR-0025) and replaces the title's rows in `frames.db` in one transaction. Plex
//! says which part each title plays and where its file is; the video itself is read
//! from that file under `PLEXDB_MEDIA_DIR`, read-only. It writes `frames.db` alone;
//! the store is read through a separate read-only connection (ADR-0024).
//!
//! **What it works on, in order.** (1) Titles someone queued, oldest first.
//! (2) Titles whose indexed `part_id` is not the part Plex lists now, least recently
//! indexed first. (3) Titles never indexed, movies before episodes, newest Plex
//! `addedAt` first. (4) Titles whose `file_hash` changed since they were indexed,
//! least recently indexed first. A failed attempt records Plex's current part, so a
//! title that failed is retried by none of (2) to (4) until its `file_hash` changes
//! or it is queued again.
//!
//! **Sampling.** The title ends at its last video frame, as the scdet pass decodes
//! it: a container can carry seconds of audio past the video, and a frame asked
//! for there does not exist. The boundaries are the Cuts, ignoring any closer than
//! [`MIN_SHOT_MS`] to the one before. Between two boundaries is a Shot, and its
//! `(in_ms, out_ms)` is stored in `shots`: this module is the only writer of Shot
//! boundaries, and readers read the boundaries it wrote. A Shot of [`SPAN_MS`] or
//! less gets one frame, at its midpoint; a longer Shot gets one frame every
//! [`SPAN_MS`], starting `SPAN_MS / 2` after its first boundary.
//!
//! **Stopping.** A run ends when the window closes (after the title in hand), when
//! nothing is left, at `--limit`, or when the encoder does not answer. An encoder
//! that stops answering leaves the title in hand untouched; the next run takes it
//! first because its rows are still absent or stale.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::time::Instant;

use plexdb_store::{Config, StoreError};
use rusqlite::{Connection, OpenFlags, Transaction, params};
use serde_json::Value;

use crate::frame_encoder::{Encoder, EncoderClient, EncoderError};
use crate::frame_media::{Cut, FfmpegMedia, MediaError, MediaReader, local_path};
use crate::frames::{
    FRAME_DIMS, FrameConfig, HarvestLock, Window, clear_decoder_fallback, delete_vectors,
    inside_hours, open_frames, parse_hours, py_repr, set_indexing, write_decoder_fallback,
};
use crate::localtime::Local;
use crate::plex::{PlexSource, TYPE_EPISODE, TYPE_MOVIE};
use crate::pyval::{get, or, py_int, py_str};
use crate::sources::Sources;
use crate::steps::Failure;

pub const NAME: &str = "harvest-frames";

/// Boundaries closer together than this are one boundary for sampling (a dissolve makes
/// scdet fire on consecutive frames). Every Cut is still stored.
pub const MIN_SHOT_MS: i64 = 500;
/// A Shot this long or shorter gets one frame; a longer one a frame this often.
pub const SPAN_MS: i64 = 2000;
/// Frames per embeddings request.
pub const BATCH: usize = 32;
/// Consecutive titles the encoder may reject (4xx other than 429) before the run stops:
/// one corrupt frame fails one title, a wrong model name fails them all.
const MAX_ENCODER_REJECTS: usize = 3;
const QUANT: &str = "int8";

pub const STOP_WINDOW: &str = "window closed";
pub const STOP_EMPTY: &str = "nothing to do";
pub const STOP_LIMIT: &str = "limit reached";
pub const STOP_ENCODER: &str = "encoder unavailable";
pub const STOP_REJECTED: &str = "encoder rejected consecutive titles";

/// The `frames-float.db` file `--keep-float` writes beside `frames.db`.
pub const FLOAT_FILE: &str = "frames-float.db";

const FLOAT_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS frame_float (
    item_id TEXT NOT NULL,
    t_ms    INTEGER NOT NULL,
    shot_ix INTEGER NOT NULL,
    vec     BLOB NOT NULL,      -- FRAME_DIMS little-endian float32s, as the encoder sent them
    PRIMARY KEY (item_id, t_ms)
);
";

// --- sampling -------------------------------------------------------------------

/// The `(start, end)` of each Shot between the boundaries `cut_times_ms`.
pub fn shots(cut_times_ms: &[i64], duration_ms: i64) -> Vec<(i64, i64)> {
    let mut sorted: Vec<i64> = cut_times_ms.to_vec();
    sorted.sort_unstable();
    sorted.dedup();
    let mut bounds = vec![0];
    for t_ms in sorted {
        let last = *bounds.last().unwrap_or(&0);
        if MIN_SHOT_MS <= t_ms - last && t_ms < duration_ms {
            bounds.push(t_ms);
        }
    }
    bounds.push(duration_ms);
    bounds.windows(2).map(|pair| (pair[0], pair[1])).collect()
}

/// `(t_ms, shot_ix)` for every frame to embed.
pub fn sample_times(shot_list: &[(i64, i64)]) -> Vec<(i64, usize)> {
    let mut out = Vec::new();
    for (ix, &(start, end)) in shot_list.iter().enumerate() {
        if end - start <= SPAN_MS {
            out.push(((start + end).div_euclid(2), ix));
            continue;
        }
        let mut t_ms = start + SPAN_MS / 2;
        while t_ms < end {
            out.push((t_ms, ix));
            t_ms += SPAN_MS;
        }
    }
    out
}

// --- discovery --------------------------------------------------------------------

/// One title's Plex part: `(kind, added_at, part_id, size, file)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Part {
    pub kind: String,
    pub added_at: i64,
    pub part_id: i64,
    pub size: i64,
    pub file: String,
}

/// `item_id -> Part` for every movie and episode Plex lists with a media part. A title
/// with several Plex copies takes the newest.
pub fn plex_parts(
    store: &Connection,
    plex: &dyn PlexSource,
) -> Result<BTreeMap<String, Part>, StoreError> {
    let mut wanted: HashMap<String, (String, String, String)> = HashMap::new();
    let mut statement = store.prepare(
        "SELECT p.rating_key, p.item_id, p.section_id, i.type FROM plex_items p \
         JOIN items i ON i.item_id = p.item_id WHERE i.type IN ('movie', 'episode')",
    )?;
    let rows = statement.query_map([], |row| {
        let text = |ix: usize| -> rusqlite::Result<String> {
            Ok(match row.get_ref(ix)? {
                rusqlite::types::ValueRef::Null => "None".to_string(),
                rusqlite::types::ValueRef::Integer(n) => n.to_string(),
                rusqlite::types::ValueRef::Real(f) => format!("{f:?}"),
                rusqlite::types::ValueRef::Text(t) => String::from_utf8_lossy(t).into_owned(),
                rusqlite::types::ValueRef::Blob(_) => String::new(),
            })
        };
        Ok((text(0)?, text(1)?, text(2)?, text(3)?))
    })?;
    for row in rows {
        let (rating_key, item_id, section, kind) = row?;
        wanted.insert(rating_key, (item_id, section, kind));
    }
    let section_types: HashMap<String, String> = plex
        .sections()?
        .into_iter()
        .map(|section| (section.key, section.kind))
        .collect();
    let section_keys: BTreeSet<&String> = wanted.values().map(|(_, section, _)| section).collect();
    let mut best: BTreeMap<String, Part> = BTreeMap::new();
    for key in section_keys {
        let plex_type = match section_types.get(key).map(String::as_str) {
            Some("movie") => TYPE_MOVIE,
            Some("show") => TYPE_EPISODE,
            _ => continue,
        };
        for record in plex.items(key, plex_type)? {
            let Some((item_id, _, item_type)) = wanted.get(&py_str(get(&record, "ratingKey")))
            else {
                continue;
            };
            let parts: Vec<&Value> = get(&record, "Media")
                .as_array()
                .into_iter()
                .flatten()
                .flat_map(|media| get(media, "Part").as_array().into_iter().flatten())
                .collect();
            let Some(first) = parts.first() else {
                continue;
            };
            let int = |value: &Value| py_int(or(value, &Value::from(0))).unwrap_or(0);
            let entry = Part {
                kind: item_type.clone(),
                added_at: int(get(&record, "addedAt")),
                part_id: py_int(get(first, "id")).ok_or_else(|| {
                    StoreError::Store(format!(
                        "Plex listed a media part with no usable id: {}",
                        get(first, "id")
                    ))
                })?,
                size: int(get(first, "size")),
                file: py_str(or(get(first, "file"), &Value::String(String::new()))),
            };
            if best
                .get(item_id)
                .is_none_or(|current| entry.added_at > current.added_at)
            {
                best.insert(item_id.clone(), entry);
            }
        }
    }
    Ok(best)
}

/// One title the harvester may index, as Plex describes it now.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub item_id: String,
    pub part_id: i64,
    pub size: i64,
    /// The part's file as Plex names it; the locator says where this process reads it.
    pub file: String,
    pub reason: &'static str,
    /// Already computed by the discovery of a changed title, so it is not read twice.
    pub file_hash: Option<String>,
}

type Locate<'a> = &'a dyn Fn(&str) -> Result<PathBuf, MediaError>;

struct IndexState {
    part_id: Option<i64>,
    file_hash: Option<String>,
    indexed_at: String,
}

enum Phase {
    Queue,
    PartChanged(std::vec::IntoIter<String>),
    Fresh(std::vec::IntoIter<String>),
    Changed(std::vec::IntoIter<String>),
    Done,
}

/// Every title to index, in order, queued first. Read lazily: a queue row added during
/// a run is seen before the backlog resumes.
struct Candidates<'a> {
    parts: &'a BTreeMap<String, Part>,
    seen: BTreeSet<String>,
    state: BTreeMap<String, IndexState>,
    phase: Phase,
}

impl<'a> Candidates<'a> {
    fn new(parts: &'a BTreeMap<String, Part>) -> Self {
        Candidates {
            parts,
            seen: BTreeSet::new(),
            state: BTreeMap::new(),
            phase: Phase::Queue,
        }
    }

    fn candidate(&self, item_id: &str, reason: &'static str, hash: Option<String>) -> Candidate {
        let part = &self.parts[item_id];
        Candidate {
            item_id: item_id.to_string(),
            part_id: part.part_id,
            size: part.size,
            file: part.file.clone(),
            reason,
            file_hash: hash,
        }
    }

    /// `item_id`s ordered by `(indexed_at, item_id)`, the order the backlog drains in.
    fn by_age(&self) -> Vec<String> {
        let mut ids: Vec<&String> = self.state.keys().collect();
        ids.sort_by(|a, b| (&self.state[*a].indexed_at, *a).cmp(&(&self.state[*b].indexed_at, *b)));
        ids.into_iter().cloned().collect()
    }

    fn next(
        &mut self,
        frames: &Connection,
        media: &dyn MediaReader,
        locate: Locate<'_>,
        log: &mut dyn FnMut(&str),
        keep_going: &dyn Fn() -> bool,
    ) -> Result<Option<Candidate>, StoreError> {
        loop {
            match &mut self.phase {
                Phase::Queue => {
                    let queued: Vec<String> = frames
                        .prepare("SELECT item_id FROM frame_queue ORDER BY queued_at, item_id")?
                        .query_map([], |row| row.get(0))?
                        .collect::<Result<_, _>>()?;
                    for item_id in &queued {
                        if !self.parts.contains_key(item_id) && !self.seen.contains(item_id) {
                            self.seen.insert(item_id.clone());
                            log(&format!(
                                "queued {item_id} is not a movie or episode Plex lists with a media part"
                            ));
                        }
                    }
                    let next = queued.into_iter().find(|item_id| {
                        self.parts.contains_key(item_id) && !self.seen.contains(item_id)
                    });
                    if let Some(item_id) = next {
                        self.seen.insert(item_id.clone());
                        return Ok(Some(self.candidate(&item_id, "queued", None)));
                    }
                    self.state = load_state(frames)?;
                    // A title whose indexed part Plex no longer lists is searchable with
                    // frames nothing can serve. It re-indexes from the part Plex lists
                    // now, ahead of the new-title backlog; a failed title retries too,
                    // as its part is not the one that failed.
                    self.phase = Phase::PartChanged(self.by_age().into_iter());
                }
                Phase::PartChanged(ids) => {
                    let Some(item_id) = ids.next() else {
                        let mut fresh: Vec<&String> = self
                            .parts
                            .keys()
                            .filter(|id| !self.state.contains_key(*id) && !self.seen.contains(*id))
                            .collect();
                        fresh.sort_by(|a, b| {
                            let (pa, pb) = (&self.parts[*a], &self.parts[*b]);
                            ((pa.kind != "movie"), -pa.added_at, *a).cmp(&(
                                (pb.kind != "movie"),
                                -pb.added_at,
                                *b,
                            ))
                        });
                        self.phase = Phase::Fresh(
                            fresh.into_iter().cloned().collect::<Vec<_>>().into_iter(),
                        );
                        continue;
                    };
                    let indexed_part = self.state[&item_id].part_id;
                    let Some(part) = self.parts.get(&item_id) else {
                        continue;
                    };
                    if self.seen.contains(&item_id)
                        || indexed_part.is_none_or(|indexed| indexed == part.part_id)
                    {
                        continue;
                    }
                    self.seen.insert(item_id.clone());
                    log(&format!(
                        "{item_id}: indexed part {} is not Plex's part {}",
                        indexed_part.unwrap_or_default(),
                        part.part_id
                    ));
                    return Ok(Some(self.candidate(&item_id, "part changed", None)));
                }
                Phase::Fresh(ids) => {
                    if let Some(item_id) = ids.next() {
                        return Ok(Some(self.candidate(&item_id, "new", None)));
                    }
                    self.phase = Phase::Changed(self.by_age().into_iter());
                }
                Phase::Changed(ids) => {
                    let Some(item_id) = ids.next() else {
                        self.phase = Phase::Done;
                        continue;
                    };
                    let Some(part) = self.parts.get(&item_id) else {
                        continue;
                    };
                    if self.seen.contains(&item_id) {
                        continue;
                    }
                    if !keep_going() {
                        self.phase = Phase::Done;
                        continue;
                    }
                    let current = match locate(&part.file)
                        .and_then(|path| media.file_hash(&path, part.size))
                    {
                        Ok(current) => current,
                        Err(err) => {
                            log(&format!("skip {item_id}: could not hash: {err}"));
                            continue;
                        }
                    };
                    if self.state[&item_id].file_hash.as_deref() != Some(current.as_str()) {
                        return Ok(Some(self.candidate(&item_id, "changed", Some(current))));
                    }
                }
                Phase::Done => return Ok(None),
            }
        }
    }
}

fn load_state(frames: &Connection) -> Result<BTreeMap<String, IndexState>, StoreError> {
    let mut statement =
        frames.prepare("SELECT item_id, part_id, file_hash, indexed_at FROM frame_index_state")?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            IndexState {
                part_id: row.get(1)?,
                file_hash: row.get(2)?,
                indexed_at: row.get(3)?,
            },
        ))
    })?;
    Ok(rows.collect::<Result<_, _>>()?)
}

// --- the run ----------------------------------------------------------------------

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct HarvestResult {
    pub stopped: &'static str,
    pub indexed: usize,
    pub failed: usize,
    pub titles: Vec<String>,
}

/// `vec` as the little-endian float32s the encoder's answer is stored as.
pub fn vec_blob(vector: &[f64]) -> Vec<u8> {
    vector
        .iter()
        .flat_map(|value| (*value as f32).to_le_bytes())
        .collect()
}

struct Written<'a> {
    cuts: &'a [Cut],
    duration_ms: i64,
    shot_list: &'a [(i64, i64)],
    samples: &'a [(i64, usize)],
    vectors: &'a [Vec<f64>],
    model: &'a str,
}

fn write_title(
    frames: &mut Connection,
    cand: &Candidate,
    file_hash: &str,
    written: &Written<'_>,
    now: &Local,
) -> Result<(), StoreError> {
    let tx: Transaction<'_> = frames.transaction()?;
    // By rowid: `DELETE FROM frame_vec WHERE item_id = ?` scans the whole vec0 table,
    // which at full library is tens of millions of vectors for every title written.
    delete_vectors(&tx, &cand.item_id)?;
    for table in ["cuts", "shots"] {
        tx.execute(
            &format!("DELETE FROM {table} WHERE item_id = ?1"),
            [&cand.item_id],
        )?;
    }
    {
        let mut insert = tx.prepare(
            "INSERT OR IGNORE INTO cuts (item_id, t_ms, score, source) VALUES (?1, ?2, ?3, ?4)",
        )?;
        for cut in written.cuts {
            insert.execute(params![cand.item_id, cut.t_ms, cut.score, cut.source])?;
        }
        let mut insert = tx.prepare(
            "INSERT INTO shots (item_id, shot_ix, in_ms, out_ms) VALUES (?1, ?2, ?3, ?4)",
        )?;
        for (ix, (start, end)) in written.shot_list.iter().enumerate() {
            insert.execute(params![cand.item_id, ix as i64, start, end])?;
        }
        let mut insert = tx.prepare(
            "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) \
             VALUES (?1, ?2, ?3, vec_quantize_int8(?4, 'unit'))",
        )?;
        for ((t_ms, ix), vector) in written.samples.iter().zip(written.vectors) {
            insert.execute(params![cand.item_id, t_ms, *ix as i64, vec_blob(vector)])?;
        }
    }
    tx.execute(
        "INSERT OR REPLACE INTO frame_index_state (item_id, part_id, model, model_version, \
         dims, quant, duration_ms, bytes, file_hash, shots, vectors, indexed_at, error) \
         VALUES (?1, ?2, ?3, NULL, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, NULL)",
        params![
            cand.item_id,
            cand.part_id,
            written.model,
            FRAME_DIMS,
            QUANT,
            written.duration_ms,
            cand.size,
            file_hash,
            written.shot_list.len() as i64,
            written.samples.len() as i64,
            now.isoformat(),
        ],
    )?;
    tx.execute(
        "DELETE FROM frame_queue WHERE item_id = ?1",
        [&cand.item_id],
    )?;
    tx.commit()?;
    Ok(())
}

/// Keep whatever rows the title had; mark the attempt failed and take it off the queue.
/// `error` is the full message, `reason` the path-free phrase a page may show.
fn record_failure(
    frames: &mut Connection,
    cand: &Candidate,
    error: &str,
    reason: &str,
    file_hash: Option<&str>,
    now: &Local,
) -> Result<(), StoreError> {
    let tx = frames.transaction()?;
    tx.execute(
        "INSERT INTO frame_index_state (item_id, part_id, bytes, file_hash, \
         indexed_at, error, reason) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
         ON CONFLICT(item_id) DO UPDATE SET part_id = excluded.part_id, \
         bytes = excluded.bytes, file_hash = coalesce(excluded.file_hash, file_hash), \
         indexed_at = excluded.indexed_at, error = excluded.error, \
         reason = excluded.reason",
        params![
            cand.item_id,
            cand.part_id,
            cand.size,
            file_hash,
            now.isoformat(),
            error,
            reason
        ],
    )?;
    tx.execute(
        "DELETE FROM frame_queue WHERE item_id = ?1",
        [&cand.item_id],
    )?;
    tx.commit()?;
    Ok(())
}

/// `frames-float.db`: the float32 vectors `--keep-float` keeps for `frames
/// quant-agreement`. A measurement, not part of the Frame index.
pub fn open_float(path: &Path) -> Result<Connection, StoreError> {
    let conn = Connection::open(path)?;
    conn.busy_timeout(std::time::Duration::from_millis(
        plexdb_store::BUSY_TIMEOUT_MS,
    ))?;
    conn.execute_batch(FLOAT_SCHEMA)?;
    Ok(conn)
}

/// Replace one title's float vectors.
pub fn write_floats(
    conn: &mut Connection,
    item_id: &str,
    samples: &[(i64, usize)],
    vectors: &[Vec<f64>],
) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    tx.execute("DELETE FROM frame_float WHERE item_id = ?1", [item_id])?;
    {
        let mut insert = tx.prepare(
            "INSERT OR REPLACE INTO frame_float (item_id, t_ms, shot_ix, vec) \
             VALUES (?1, ?2, ?3, ?4)",
        )?;
        for ((t_ms, ix), vector) in samples.iter().zip(vectors) {
            insert.execute(params![item_id, t_ms, *ix as i64, vec_blob(vector)])?;
        }
    }
    tx.commit()
}

enum Outcome {
    Ok,
    Failed,
    Rejected,
}

/// Why indexing one title stopped without writing it.
enum Stop {
    Media(MediaError),
    Encoder(EncoderError),
    Store(StoreError),
}

impl From<MediaError> for Stop {
    fn from(err: MediaError) -> Self {
        Stop::Media(err)
    }
}
impl From<StoreError> for Stop {
    fn from(err: StoreError) -> Self {
        Stop::Store(err)
    }
}

/// Why a title ended the run instead of being recorded.
enum RunStop {
    /// The encoder stopped answering; the title in hand is left untouched.
    EncoderUnavailable(String),
    Store(StoreError),
}

impl From<StoreError> for RunStop {
    fn from(err: StoreError) -> Self {
        RunStop::Store(err)
    }
}

struct Ports<'a> {
    media: &'a dyn MediaReader,
    locate: Locate<'a>,
    encoder: &'a dyn Encoder,
    now: &'a dyn Fn() -> Local,
}

fn index_title(
    frames: &mut Connection,
    cand: &Candidate,
    ports: &Ports<'_>,
    log: &mut dyn FnMut(&str),
    float: &mut Option<Connection>,
) -> Result<Outcome, RunStop> {
    let stamp = || (ports.now)().isoformat_seconds();
    let mut file_hash = cand.file_hash.clone();
    let (mut shot_count, mut vector_count, mut decode_warnings) = (0usize, 0usize, 0usize);
    let (mut decode_s, mut scan_s, mut extract_s, mut encode_s) = (0.0f64, 0.0f64, 0.0f64, 0.0f64);
    let mut error: Option<String> = None;
    let mut outcome = Outcome::Ok;
    let attempt = (|| -> Result<(), Stop> {
        let path = (ports.locate)(&cand.file)?;
        let hash = match &file_hash {
            Some(hash) => hash.clone(),
            None => {
                let hash = ports.media.file_hash(&path, cand.size)?;
                file_hash = Some(hash.clone());
                hash
            }
        };
        let started = Instant::now();
        log(&format!(
            "{} item_id={} stage=scan start",
            stamp(),
            cand.item_id
        ));
        let scan = ports.media.scan(&path)?;
        scan_s = started.elapsed().as_secs_f64();
        decode_warnings = scan.warnings;
        let cut_times: Vec<i64> = scan.cuts.iter().map(|cut| cut.t_ms).collect();
        let shot_list = shots(&cut_times, scan.last_frame_ms);
        let samples = sample_times(&shot_list);
        log(&format!(
            "{} item_id={} stage=extract start",
            stamp(),
            cand.item_id
        ));
        let extract_started = Instant::now();
        let times: Vec<i64> = samples.iter().map(|(t_ms, _)| *t_ms).collect();
        let jpegs = ports.media.frames(&path, &times)?;
        extract_s = extract_started.elapsed().as_secs_f64();
        decode_s = started.elapsed().as_secs_f64();
        let kept: Vec<((i64, usize), Vec<u8>)> = samples
            .iter()
            .copied()
            .zip(jpegs)
            .filter_map(|(sample, jpeg)| {
                jpeg.filter(|bytes| !bytes.is_empty()).map(|j| (sample, j))
            })
            .collect();
        if kept.is_empty() {
            return Err(MediaError::new(
                format!("{}: no frame could be read", path.display()),
                "no frame could be read",
            )
            .into());
        }
        let samples: Vec<(i64, usize)> = kept.iter().map(|(sample, _)| *sample).collect();
        let mut vectors: Vec<Vec<f64>> = Vec::new();
        let mut model = ports.encoder.model().to_string();
        for batch in kept.chunks(BATCH) {
            let images: Vec<Vec<u8>> = batch.iter().map(|(_, jpeg)| jpeg.clone()).collect();
            let answer = ports.encoder.timed_images(&images).map_err(Stop::Encoder)?;
            encode_s += answer.total_s;
            if !answer.model.is_empty() {
                model = answer.model;
            }
            vectors.extend(answer.vectors);
        }
        if vectors.iter().any(|vector| vector.len() != FRAME_DIMS) {
            return Err(Stop::Encoder(EncoderError::Rejected(format!(
                "the encoder returned {}-wide vectors; frame_vec holds {FRAME_DIMS}",
                vectors[0].len()
            ))));
        }
        shot_count = shot_list.len();
        vector_count = samples.len();
        if let Some(conn) = float.as_mut() {
            // A measurement file failing to take a title must not cost the index the
            // title; `frames quant-agreement` names the titles it holds no floats for.
            if let Err(err) = write_floats(conn, &cand.item_id, &samples, &vectors) {
                log(&format!(
                    "item_id={} float vectors not kept: {err}",
                    cand.item_id
                ));
            }
        }
        write_title(
            frames,
            cand,
            &hash,
            &Written {
                cuts: &scan.cuts,
                duration_ms: scan.last_frame_ms,
                shot_list: &shot_list,
                samples: &samples,
                vectors: &vectors,
                model: &model,
            },
            &(ports.now)(),
        )?;
        Ok(())
    })();
    match attempt {
        Ok(()) => {}
        Err(Stop::Store(err)) => return Err(RunStop::Store(err)),
        Err(Stop::Encoder(EncoderError::Unavailable(message))) => {
            return Err(RunStop::EncoderUnavailable(message));
        }
        Err(Stop::Encoder(err)) => {
            error = Some(err.to_string());
            outcome = Outcome::Rejected;
            record_failure(
                frames,
                cand,
                &err.to_string(),
                "encoder refused the frames",
                file_hash.as_deref(),
                &(ports.now)(),
            )?;
        }
        Err(Stop::Media(err)) => {
            error = Some(err.message.clone());
            outcome = Outcome::Failed;
            record_failure(
                frames,
                cand,
                &err.message,
                &err.reason,
                file_hash.as_deref(),
                &(ports.now)(),
            )?;
        }
    }
    let line = format!(
        "{} item_id={} part={} bytes={} decode_s={decode_s:.1} scan_s={scan_s:.1} \
         extract_s={extract_s:.1} encode_s={encode_s:.1} shots={shot_count} \
         vectors={vector_count} decode_warnings={decode_warnings} reason={}",
        stamp(),
        cand.item_id,
        cand.part_id,
        cand.size,
        cand.reason
    );
    match &error {
        None => log(&line),
        Some(error) => log(&format!("{line} error={}", py_repr(error))),
    }
    Ok(outcome)
}

/// What a run is asked to do.
#[derive(Debug, Clone, Default)]
pub struct Options {
    pub ignore_hours: bool,
    pub limit: Option<usize>,
    pub keep_float: bool,
}

/// Everything a run reads from outside `frames.db`.
pub struct Inputs<'a> {
    pub plex: &'a dyn PlexSource,
    pub media: &'a dyn MediaReader,
    pub encoder: &'a dyn Encoder,
    pub locate: Locate<'a>,
    pub store_path: &'a Path,
    pub hours: Option<Window>,
    pub now: &'a dyn Fn() -> Local,
}

/// One harvest run over the Frame index at `frames_path`. Fails when another harvester,
/// in this process or another, holds the slot.
pub fn harvest(
    frames_path: &Path,
    inputs: &Inputs<'_>,
    options: &Options,
    log: &mut dyn FnMut(&str),
) -> Result<HarvestResult, StoreError> {
    let _lock = HarvestLock::acquire(frames_path)?;
    let mut float = if options.keep_float {
        Some(open_float(&frames_path.with_file_name(FLOAT_FILE))?)
    } else {
        None
    };
    let parts = {
        let store =
            Connection::open_with_flags(inputs.store_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
                .map_err(|err| {
                    StoreError::Store(format!(
                        "cannot open {}: {err}",
                        inputs.store_path.display()
                    ))
                })?;
        store.busy_timeout(std::time::Duration::from_millis(
            plexdb_store::BUSY_TIMEOUT_MS,
        ))?;
        plex_parts(&store, inputs.plex)?
    };
    let mut frames = open_frames(frames_path)?;
    let now = inputs.now;
    set_indexing(&frames, None, &now())?;
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run_titles(&mut frames, &parts, inputs, options, log, &mut float)
    }));
    // The title in hand is released whether the run ended, failed or panicked.
    let released = set_indexing(&frames, None, &now());
    let result = match result {
        Ok(result) => result?,
        Err(panic) => std::panic::resume_unwind(panic),
    };
    released?;
    Ok(result)
}

fn run_titles(
    frames: &mut Connection,
    parts: &BTreeMap<String, Part>,
    inputs: &Inputs<'_>,
    options: &Options,
    log: &mut dyn FnMut(&str),
    float: &mut Option<Connection>,
) -> Result<HarvestResult, StoreError> {
    let now = inputs.now;
    let ports = Ports {
        media: inputs.media,
        locate: inputs.locate,
        encoder: inputs.encoder,
        now,
    };
    let keep_going = || options.ignore_hours || inside_hours(inputs.hours, &now());
    let mut candidates = Candidates::new(parts);
    let mut result = HarvestResult::default();
    let mut rejects = 0usize;
    let mut tagged = |line: &str| log(&format!("frames: {line}"));
    loop {
        if !options.ignore_hours && !inside_hours(inputs.hours, &now()) {
            result.stopped = STOP_WINDOW;
            break;
        }
        if options
            .limit
            .is_some_and(|limit| result.indexed + result.failed >= limit)
        {
            result.stopped = STOP_LIMIT;
            break;
        }
        let Some(cand) = candidates.next(
            frames,
            inputs.media,
            inputs.locate,
            &mut tagged,
            &keep_going,
        )?
        else {
            result.stopped = STOP_EMPTY;
            break;
        };
        set_indexing(frames, Some(&cand.item_id), &now())?;
        let outcome = index_title(frames, &cand, &ports, &mut tagged, float);
        set_indexing(frames, None, &now())?;
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(RunStop::EncoderUnavailable(message)) => {
                tagged(&format!("stopping, {message}"));
                result.stopped = STOP_ENCODER;
                break;
            }
            Err(RunStop::Store(err)) => return Err(err),
        };
        rejects = if matches!(outcome, Outcome::Rejected) {
            rejects + 1
        } else {
            0
        };
        result.titles.push(cand.item_id);
        if matches!(outcome, Outcome::Ok) {
            result.indexed += 1;
        } else {
            result.failed += 1;
        }
        if rejects >= MAX_ENCODER_REJECTS {
            result.stopped = STOP_REJECTED;
            break;
        }
    }
    Ok(result)
}

// --- the command ----------------------------------------------------------------

fn parse_args(args: &[String]) -> Result<Options, Failure> {
    let usage = || {
        Failure::Usage(format!(
            "{NAME} takes [--ignore-hours] [--limit N] [--keep-float], got {}",
            args.join(" ")
        ))
    };
    let mut options = Options::default();
    let mut words = args.iter();
    while let Some(word) = words.next() {
        match word.as_str() {
            "--ignore-hours" => options.ignore_hours = true,
            "--keep-float" => options.keep_float = true,
            "--limit" => {
                let value = words.next().ok_or_else(usage)?;
                options.limit = Some(value.parse().map_err(|_| usage())?);
            }
            joined if joined.starts_with("--limit=") => {
                options.limit = Some(joined["--limit=".len()..].parse().map_err(|_| usage())?);
            }
            _ => return Err(usage()),
        }
    }
    Ok(options)
}

/// Print a progress line. A closed stdout pipe loses the line; it never panics, which
/// would abandon the run's `frame_harvest` row.
fn say(line: &str) {
    use std::io::Write;
    let _ = writeln!(std::io::stdout(), "{line}");
}

pub fn run(args: &[String]) -> Result<(), Failure> {
    let options = parse_args(args)?;
    (|| -> Result<(), StoreError> {
        let config = FrameConfig::from_env()?;
        let path = config.require_frames_path()?;
        if !crate::frames_cmd::which(&config.ffmpeg) || !crate::frames_cmd::which(&config.ffprobe) {
            return Err(StoreError::Store(format!(
                "{} and {} must be installed (or named by PLEXDB_FFMPEG and PLEXDB_FFPROBE) \
                 to harvest frames",
                config.ffmpeg, config.ffprobe
            )));
        }
        clear_decoder_fallback(&path);
        let fallback_path = path.clone();
        let media = FfmpegMedia::new(
            &config.ffmpeg,
            &config.ffprobe,
            &config.decode,
            Some(Box::new(move |reason: &str| {
                say(&format!(
                    "frame decode: cuda failed, decoding on the CPU: {reason}"
                ));
                if let Err(err) = write_decoder_fallback(&fallback_path, reason) {
                    use std::io::Write;
                    let _ = writeln!(
                        std::io::stderr(),
                        "could not record the decoder fallback: {err}"
                    );
                }
            })),
        );
        let encoder = EncoderClient::from_config(&config).map_err(|err| match err {
            EncoderError::Config(message) => StoreError::Config(message),
            other => StoreError::Store(other.to_string()),
        })?;
        let hours = parse_hours(&config.frame_hours)?;
        let media_dir = config.media_dir.clone().ok_or_else(|| {
            StoreError::Config(
                "PLEXDB_MEDIA_DIR is not set, so no video file can be read; mount the library \
                 read-only and name the directory Plex calls PLEX_SOURCE_ROOTS"
                    .into(),
            )
        })?;
        if config.source_roots.is_empty() {
            return Err(StoreError::Config(
                "PLEX_SOURCE_ROOTS is not set, so Plex's file paths cannot be mapped".into(),
            ));
        }
        let roots = config.source_roots.clone();
        let locate = move |plex_file: &str| local_path(&media_dir, plex_file, &roots);
        let store = Config::from_env()?;
        let plex = Sources::from_env().plex("harvest frames")?;
        let result = harvest(
            &path,
            &Inputs {
                plex: &plex,
                media: &media,
                encoder: &encoder,
                locate: &locate,
                store_path: &store.store_path,
                hours,
                now: &Local::now,
            },
            &options,
            &mut say,
        )?;
        say(&format!(
            "{NAME} stopped: {}; {} indexed, {} failed",
            result.stopped,
            crate::frames_cmd::group_thousands(result.indexed as i64),
            crate::frames_cmd::group_thousands(result.failed as i64)
        ));
        Ok(())
    })()
    .map_err(crate::steps::failure)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boundaries_closer_than_the_minimum_merge() {
        assert_eq!(
            shots(&[3000, 3100, 5000], 10_000),
            [(0, 3000), (3000, 5000), (5000, 10_000)]
        );
        assert_eq!(shots(&[], 400), [(0, 400)]);
        assert_eq!(shots(&[400, 12_000], 10_000), [(0, 10_000)]);
    }

    #[test]
    fn a_short_shot_gets_its_midpoint_and_a_long_one_a_frame_every_span() {
        assert_eq!(sample_times(&[(0, 400)]), [(200, 0)]);
        assert_eq!(
            sample_times(&[(0, 3000), (3000, 5000), (5000, 10_000)]),
            [(1000, 0), (4000, 1), (6000, 2), (8000, 2)]
        );
    }

    #[test]
    fn a_vector_is_stored_as_little_endian_float32() {
        let mut vector = vec![0.0; FRAME_DIMS];
        vector[0] = 1.0;
        let blob = vec_blob(&vector);
        assert_eq!(blob.len(), FRAME_DIMS * 4);
        assert_eq!(&blob[..4], 1.0f32.to_le_bytes());
    }

    #[test]
    fn arguments_parse_as_the_python_command_did() {
        let words = |args: &[&str]| args.iter().map(|a| a.to_string()).collect::<Vec<_>>();
        let options = parse_args(&words(&["--ignore-hours", "--limit", "3", "--keep-float"]))
            .expect("parses");
        assert!(options.ignore_hours && options.keep_float);
        assert_eq!(options.limit, Some(3));
        assert_eq!(parse_args(&words(&["--limit=7"])).unwrap().limit, Some(7));
        assert!(parse_args(&words(&["--limit", "x"])).is_err());
        assert!(parse_args(&words(&["--nope"])).is_err());
    }
}
