//! Why a Shot matched: the Search tab's "Why?" inspector, computed from `frames.db`
//!
//! Everything here is arithmetic on vectors the Frame index already holds plus the phrase
//! vector the search left in the Phrase cache, so no encoder or model weights are involved
//! (ADR-0017). A frame's similarity to the phrase is `dot(phrase, frame) / |frame|`, the same
//! cosine the reader ranks by; two frames' image similarity is the cosine of their int8
//! vectors.
//!
//! The library's vectors are read once and kept until a title is indexed or replaced: 70,083
//! frames of 768 int8s are about 54 MB.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex, PoisonError};

use plexdb_schema::frames::FRAME_DIMS;
use rusqlite::{Connection, OpenFlags};

use crate::frames::load_vec_extension;

/// Peaks marked on a title's plot, and the least gap between two of them.
const PEAKS: usize = 4;
const PEAK_GAP_MS: i64 = 120_000;
/// Nearest frames shown from the same title and from other titles, and the cap per other title.
const SAME_TITLE: usize = 6;
const OTHER_TITLES: usize = 8;
const PER_TITLE: usize = 2;

/// The match cannot be explained; the message says why.
#[derive(Debug)]
pub struct WhyUnavailable(pub String);

/// Every indexed frame: where it is, its int8 vector, and the vector's length.
pub struct Library {
    stamp: String,
    item_ids: Vec<String>,
    t_ms: Vec<i64>,
    shot_ix: Vec<i64>,
    /// `n × FRAME_DIMS` int8s, row-major.
    vecs: Vec<i8>,
    norms: Vec<f32>,
    /// `item_id -> [start, end)` rows, contiguous.
    first: BTreeMap<String, (usize, usize)>,
}

/// One frame near the best one.
pub struct Neighbour {
    pub item_id: String,
    pub t_ms: i64,
    pub image_sim: f32,
    pub phrase_sim: f32,
}

/// The numbers behind one Shot's match, before titles and parts are added.
pub struct Explained {
    pub frames: usize,
    pub titles: usize,
    pub median: f32,
    pub top1: f32,
    pub highest: f32,
    pub is_highest: bool,
    pub best: (i64, f32),
    pub series: Vec<(i64, f32)>,
    pub peaks: Vec<(i64, f32)>,
    pub same: Vec<Neighbour>,
    pub other: Vec<Neighbour>,
}

fn open_readonly(path: &Path) -> Result<Connection, WhyUnavailable> {
    load_vec_extension();
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|err| WhyUnavailable(format!("could not read the store: {err}")))?;
    conn.busy_timeout(std::time::Duration::from_secs(10))
        .map_err(|err| WhyUnavailable(format!("could not read the store: {err}")))?;
    Ok(conn)
}

fn query(err: rusqlite::Error) -> WhyUnavailable {
    WhyUnavailable(format!("could not read the store: {err}"))
}

/// The library read from `frames.db`, once per change to the file.
#[derive(Default)]
pub struct WhyCache {
    library: Mutex<Option<Arc<Library>>>,
}

impl WhyCache {
    /// The library at `frames_file`, reloaded when a title was indexed or replaced since.
    pub fn library(&self, frames_file: &Path) -> Result<Arc<Library>, WhyUnavailable> {
        // The vectors change only when a title is indexed or replaced, which moves one of
        // these; the file itself is written to all through a harvest.
        let stamp = {
            let conn = open_readonly(frames_file)?;
            conn.query_row(
                "SELECT count(*), coalesce(sum(vectors), 0), coalesce(max(indexed_at), '') \
                 FROM frame_index_state WHERE model IS NOT NULL AND error IS NULL",
                [],
                |row| {
                    let count: i64 = row.get(0)?;
                    let vectors: rusqlite::types::Value = row.get(1)?;
                    let newest: String = row.get(2)?;
                    Ok(format!("{count}:{vectors:?}:{newest}"))
                },
            )
            .map_err(query)?
        };
        let mut held = self.library.lock().unwrap_or_else(PoisonError::into_inner);
        if held.as_ref().is_none_or(|lib| lib.stamp != stamp) {
            *held = Some(Arc::new(load(frames_file, stamp)?));
        }
        Ok(held.clone().expect("just loaded"))
    }
}

fn load(path: &Path, stamp: String) -> Result<Library, WhyUnavailable> {
    let conn = open_readonly(path)?;
    let mut stmt = conn
        .prepare(
            "SELECT v.item_id, v.t_ms, v.shot_ix, v.vec FROM frame_vec v \
             JOIN frame_index_state s ON s.item_id = v.item_id \
             WHERE s.model IS NOT NULL AND s.error IS NULL",
        )
        .map_err(query)?;
    let mut rows: Vec<(String, i64, i64, Vec<u8>)> = stmt
        .query_map([], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })
        .map_err(query)?
        .collect::<Result<_, _>>()
        .map_err(query)?;
    rows.sort_by(|a, b| (&a.0, a.1).cmp(&(&b.0, b.1)));
    let n = rows.len();
    let mut vecs: Vec<i8> = Vec::with_capacity(n * FRAME_DIMS);
    let mut first: BTreeMap<String, (usize, usize)> = BTreeMap::new();
    let mut item_ids = Vec::with_capacity(n);
    let mut t_ms = Vec::with_capacity(n);
    let mut shot_ix = Vec::with_capacity(n);
    for (i, (item_id, t, ix, blob)) in rows.into_iter().enumerate() {
        let mut row: Vec<i8> = blob.iter().map(|b| *b as i8).collect();
        row.resize(FRAME_DIMS, 0);
        vecs.extend_from_slice(&row);
        let start = first.get(&item_id).map_or(i, |(start, _)| *start);
        first.insert(item_id.clone(), (start, i + 1));
        item_ids.push(item_id);
        t_ms.push(t);
        shot_ix.push(ix);
    }
    let norms = (0..n)
        .map(|i| {
            let row = &vecs[i * FRAME_DIMS..(i + 1) * FRAME_DIMS];
            (row.iter()
                .map(|v| f64::from(*v) * f64::from(*v))
                .sum::<f64>() as f32)
                .sqrt()
        })
        .collect();
    Ok(Library {
        stamp,
        item_ids,
        t_ms,
        shot_ix,
        vecs,
        norms,
        first,
    })
}

impl Library {
    fn row(&self, i: usize) -> &[i8] {
        &self.vecs[i * FRAME_DIMS..(i + 1) * FRAME_DIMS]
    }

    /// Each frame's cosine with `query`; 0 for an all-zero frame. With `unit` the query is
    /// taken as already a unit vector, as the reader takes a phrase: `dot(phrase, frame) / |frame|`.
    fn cosines(&self, query: &[f32], unit: bool) -> Vec<f32> {
        let size = if unit {
            1.0f32
        } else {
            (query
                .iter()
                .map(|q| f64::from(*q) * f64::from(*q))
                .sum::<f64>() as f32)
                .sqrt()
        };
        (0..self.norms.len())
            .map(|i| {
                let dot = self
                    .row(i)
                    .iter()
                    .zip(query)
                    .map(|(v, q)| f64::from(*v) * f64::from(*q))
                    .sum::<f64>() as f32;
                let denom = self.norms[i] * size;
                if denom > 0.0 { dot / denom } else { 0.0 }
            })
            .collect()
    }
}

/// Indices of `values` from the largest down, ties in index order (`argsort(-x, kind="stable")`).
fn descending(values: &[f32]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..values.len()).collect();
    order.sort_by(|a, b| values[*b].total_cmp(&values[*a]));
    order
}

/// `numpy.median` over float32 values.
fn median(values: &[f32]) -> f32 {
    let mut sorted = values.to_vec();
    sorted.sort_by(f32::total_cmp);
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    }
}

/// `numpy.quantile(values, q)` with the default linear interpolation.
fn quantile(values: &[f32], q: f64) -> f32 {
    let mut sorted = values.to_vec();
    sorted.sort_by(f32::total_cmp);
    let position = q * (sorted.len() - 1) as f64;
    let lo = position.floor() as usize;
    let hi = position.ceil() as usize;
    let frac = (position - lo as f64) as f32;
    sorted[lo] + (sorted[hi] - sorted[lo]) * frac
}

/// The numbers behind one Shot's match.
pub fn explain(
    lib: &Library,
    phrase_vec: &[f32],
    item_id: &str,
    shot_ix: i64,
) -> Result<Explained, WhyUnavailable> {
    let Some(&(lo, hi)) = lib.first.get(item_id) else {
        return Err(WhyUnavailable(format!(
            "{item_id} has no frames in the Frame index"
        )));
    };
    let sims = lib.cosines(phrase_vec, true);
    if sims.is_empty() {
        return Err(WhyUnavailable(
            "this Shot has no frames in the Frame index".into(),
        ));
    }
    let median = median(&sims);
    let top1 = quantile(&sims, 0.99);
    let highest = sims.iter().copied().fold(f32::MIN, f32::max);
    let title_sims = &sims[lo..hi];
    let in_shot: Vec<usize> = (lo..hi).filter(|i| lib.shot_ix[*i] == shot_ix).collect();
    if in_shot.is_empty() {
        return Err(WhyUnavailable(
            "this Shot has no frames in the Frame index".into(),
        ));
    }
    let best =
        in_shot.iter().copied().fold(
            in_shot[0],
            |best, i| if sims[i] > sims[best] { i } else { best },
        );
    let best_sim = sims[best];

    let series: Vec<(i64, f32)> = (lo..hi).map(|i| (lib.t_ms[i], sims[i])).collect();
    let mut peaks: Vec<(i64, f32)> = Vec::new();
    for i in descending(title_sims) {
        let t = lib.t_ms[lo + i];
        if peaks.iter().all(|(p, _)| (t - p).abs() >= PEAK_GAP_MS) {
            peaks.push((t, title_sims[i]));
            if peaks.len() == PEAKS {
                break;
            }
        }
    }

    let best_vec: Vec<f32> = lib.row(best).iter().map(|v| f32::from(*v)).collect();
    let likeness = lib.cosines(&best_vec, false);
    let mut same = Vec::new();
    let mut other = Vec::new();
    let mut per_title: BTreeMap<&str, usize> = BTreeMap::new();
    for i in descending(&likeness) {
        if i == best {
            continue;
        }
        let other_id = lib.item_ids[i].as_str();
        let entry = || Neighbour {
            item_id: other_id.to_string(),
            t_ms: lib.t_ms[i],
            image_sim: likeness[i],
            phrase_sim: sims[i],
        };
        if other_id == item_id {
            if lib.shot_ix[i] != shot_ix && same.len() < SAME_TITLE {
                same.push(entry());
            }
        } else if other.len() < OTHER_TITLES
            && per_title.get(other_id).copied().unwrap_or(0) < PER_TITLE
        {
            *per_title.entry(other_id).or_insert(0) += 1;
            other.push(entry());
        }
        if same.len() == SAME_TITLE && other.len() == OTHER_TITLES {
            break;
        }
    }
    Ok(Explained {
        frames: sims.len(),
        titles: lib.first.len(),
        median,
        top1,
        highest,
        is_highest: best_sim >= highest,
        best: (lib.t_ms[best], best_sim),
        series,
        peaks,
        same,
        other,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn median_and_quantile_follow_numpy() {
        assert_eq!(median(&[3.0, 1.0, 2.0]), 2.0);
        assert_eq!(median(&[4.0, 1.0, 3.0, 2.0]), 2.5);
        assert_eq!(quantile(&[0.0, 1.0, 2.0, 3.0], 0.99), 2.97);
        assert_eq!(quantile(&[5.0], 0.99), 5.0);
    }

    #[test]
    fn descending_keeps_ties_in_index_order() {
        assert_eq!(descending(&[0.5, 0.9, 0.5, 0.1]), vec![1, 0, 2, 3]);
    }
}
