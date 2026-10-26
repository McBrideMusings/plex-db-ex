//! The int8-against-fp16 measurement for the Frame index (plex-db-ex-8nb.1.7)
//! (`plexdb/frame_quant.py`).
//!
//! `frame_vec` keeps each frame vector only as int8. To say what that costs, a harvest
//! run with `--keep-float` also writes the encoder's float32 vectors to `frames-float.db`
//! beside `frames.db`, and [`agreement`] ranks the same Shots for a phrase from both and
//! counts how many of the top k match. The file is a measurement, not part of the Frame
//! index: no reader opens it, nothing migrates it, and it can be deleted at any time.
//!
//! Both rankings use the search's phrase-only rule — a Shot scores its best frame's
//! cosine with the phrase — so the only difference between them is how the frame vector
//! was stored. The fp16 side casts each float32 vector to float16.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use plexdb_store::{BUSY_TIMEOUT_MS, StoreError};
use rusqlite::Connection;

use crate::pyjson::Json;

pub const FLOAT_FILE: &str = "frames-float.db";

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS frame_float (
    item_id TEXT NOT NULL,
    t_ms    INTEGER NOT NULL,
    shot_ix INTEGER NOT NULL,
    vec     BLOB NOT NULL,      -- FRAME_DIMS little-endian float32s, as the encoder sent them
    PRIMARY KEY (item_id, t_ms)
);
";

pub fn float_path(frames_db: &Path) -> PathBuf {
    frames_db.with_file_name(FLOAT_FILE)
}

pub fn open_float(path: &Path) -> Result<Connection, StoreError> {
    let conn = Connection::open(path)
        .map_err(|err| StoreError::Store(format!("cannot open {}: {err}", path.display())))?;
    conn.busy_timeout(std::time::Duration::from_millis(BUSY_TIMEOUT_MS))?;
    conn.execute_batch(SCHEMA)?;
    Ok(conn)
}

type ShotKey = (String, i64);

/// The frames both files hold, row-aligned: `keys[i]` is frame i's Shot.
pub struct Paired {
    pub keys: Vec<ShotKey>,
    int8: Vec<Vec<f32>>,
    fp16: Vec<Vec<f32>>,
    pub titles: usize,
    pub titles_without_floats: Vec<String>,
}

/// numpy's `float32 -> float16` cast (round to nearest, ties to even), kept as the
/// float32 value the half stands for.
pub fn through_fp16(x: f32) -> f32 {
    f16_to_f32(f32_to_f16(x))
}

fn f32_to_f16(x: f32) -> u16 {
    let bits = x.to_bits();
    let sign = ((bits >> 16) & 0x8000) as u16;
    let exp = ((bits >> 23) & 0xff) as i32;
    let man = bits & 0x7f_ffff;
    if exp == 255 {
        return sign | 0x7c00 | if man != 0 { 0x200 } else { 0 };
    }
    let e = exp - 127 + 15;
    if e >= 31 {
        return sign | 0x7c00;
    }
    let round = |value: u32, rem: u32, halfway: u32| {
        value + u32::from(rem > halfway || (rem == halfway && value & 1 == 1))
    };
    if e <= 0 {
        if e < -10 {
            return sign;
        }
        let m = man | 0x80_0000;
        let shift = (14 - e) as u32;
        let half = m >> shift;
        let rem = m & ((1 << shift) - 1);
        return sign | round(half, rem, 1 << (shift - 1)) as u16;
    }
    let half = ((e as u32) << 10) | (man >> 13);
    sign | round(half, man & 0x1fff, 0x1000) as u16
}

fn f16_to_f32(bits: u16) -> f32 {
    let sign = if bits & 0x8000 != 0 { -1.0f32 } else { 1.0 };
    let exp = i32::from((bits >> 10) & 0x1f);
    let man = f32::from(bits & 0x3ff);
    match exp {
        0 => sign * man * 2f32.powi(-24),
        31 if man == 0.0 => sign * f32::INFINITY,
        31 => f32::NAN,
        _ => sign * (1.0 + man / 1024.0) * 2f32.powi(exp - 15),
    }
}

/// Read both files once; [`agreement`] then scores any number of phrases.
pub fn load(frames: &Connection, floats: &Connection) -> rusqlite::Result<Paired> {
    let mut held: HashMap<(String, i64), (i64, Vec<u8>)> = HashMap::new();
    let mut stmt = floats.prepare("SELECT item_id, t_ms, shot_ix, vec FROM frame_float")?;
    for row in stmt.query_map([], |row| {
        Ok((
            (row.get::<_, String>(0)?, row.get::<_, i64>(1)?),
            (row.get::<_, i64>(2)?, row.get::<_, Vec<u8>>(3)?),
        ))
    })? {
        let (key, value) = row?;
        held.insert(key, value);
    }
    let mut paired = Paired {
        keys: Vec::new(),
        int8: Vec::new(),
        fp16: Vec::new(),
        titles: 0,
        titles_without_floats: Vec::new(),
    };
    let mut titles: BTreeSet<String> = BTreeSet::new();
    let mut paired_titles: BTreeSet<String> = BTreeSet::new();
    let mut stmt = frames.prepare("SELECT item_id, t_ms, shot_ix, vec FROM frame_vec")?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let item_id: String = row.get(0)?;
        let t_ms: i64 = row.get(1)?;
        let shot_ix: i64 = row.get(2)?;
        let vec: Vec<u8> = row.get(3)?;
        titles.insert(item_id.clone());
        let Some((held_ix, floats)) = held.remove(&(item_id.clone(), t_ms)) else {
            continue;
        };
        if held_ix != shot_ix {
            continue;
        }
        paired.fp16.push(
            floats
                .chunks_exact(4)
                .map(|b| through_fp16(f32::from_le_bytes([b[0], b[1], b[2], b[3]])))
                .collect(),
        );
        paired
            .int8
            .push(vec.iter().map(|&b| f32::from(b as i8)).collect());
        paired.keys.push((item_id.clone(), shot_ix));
        paired_titles.insert(item_id);
    }
    paired.titles = titles.len();
    paired.titles_without_floats = titles.difference(&paired_titles).cloned().collect();
    Ok(paired)
}

/// numpy's pairwise summation of a contiguous float32 run, the order `np.add.reduce`
/// adds in.
fn pairwise_sum(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        a.iter().fold(0.0, |acc, x| acc + x)
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

fn norm(row: &[f32]) -> f32 {
    let squares: Vec<f32> = row.iter().map(|x| x * x).collect();
    pairwise_sum(&squares).sqrt()
}

/// Each row's cosine with `phrase`: `row . phrase / |row|`, the dot product summed in
/// float64 (numpy hands it to a BLAS routine whose order no caller can reproduce).
fn sims(vectors: &[Vec<f32>], phrase: &[f32]) -> Vec<f32> {
    vectors
        .iter()
        .map(|row| {
            let dot: f64 = row
                .iter()
                .zip(phrase)
                .map(|(a, b)| f64::from(*a) * f64::from(*b))
                .sum();
            dot as f32 / norm(row)
        })
        .collect()
}

fn ranked(keys: &[ShotKey], sims: &[f32]) -> Vec<ShotKey> {
    let mut best: BTreeMap<&ShotKey, f32> = BTreeMap::new();
    for (key, sim) in keys.iter().zip(sims) {
        let slot = best.entry(key).or_insert(-2.0);
        if *sim > *slot {
            *slot = *sim;
        }
    }
    let mut order: Vec<(&ShotKey, f32)> = best.into_iter().collect();
    order.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(b.0)));
    order.into_iter().map(|(key, _)| key.clone()).collect()
}

/// Python's `round(x, 5)`.
fn round5(x: f64) -> f64 {
    format!("{x:.5}").parse().unwrap_or(x)
}

/// Rank Shots for one phrase from int8 and from fp16 and report the overlap of each
/// top k.
pub fn agreement(paired: &Paired, phrase_vec: &[f64], ks: &[usize]) -> Json {
    if paired.keys.is_empty() {
        return Json::object([
            ("frames", Json::Int(0)),
            ("shots", Json::Int(0)),
            ("top", Json::Object(Vec::new())),
            ("same_first", Json::Null),
            ("max_abs_sim_diff", Json::Null),
        ]);
    }
    let mut phrase: Vec<f32> = phrase_vec.iter().map(|x| *x as f32).collect();
    let length = phrase
        .iter()
        .map(|x| f64::from(*x) * f64::from(*x))
        .sum::<f64>() as f32;
    let length = length.sqrt();
    for x in &mut phrase {
        *x /= length;
    }
    let int8_sims = sims(&paired.int8, &phrase);
    let fp16_sims = sims(&paired.fp16, &phrase);
    let int8_rank = ranked(&paired.keys, &int8_sims);
    let fp16_rank = ranked(&paired.keys, &fp16_sims);
    let top: Vec<(String, Json)> = ks
        .iter()
        .map(|&k| {
            let a: BTreeSet<&ShotKey> = int8_rank.iter().take(k).collect();
            let b: BTreeSet<&ShotKey> = fp16_rank.iter().take(k).collect();
            (k.to_string(), Json::Int(a.intersection(&b).count() as i64))
        })
        .collect();
    let max_diff = int8_sims
        .iter()
        .zip(&fp16_sims)
        .map(|(a, b)| (a - b).abs())
        .fold(0.0f32, f32::max);
    Json::object([
        ("frames", Json::Int(paired.keys.len() as i64)),
        ("shots", Json::Int(int8_rank.len() as i64)),
        ("top", Json::Object(top)),
        ("same_first", Json::Bool(int8_rank[0] == fp16_rank[0])),
        ("max_abs_sim_diff", Json::Float(round5(f64::from(max_diff)))),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fp16_cast_rounds_to_nearest_even_like_numpy() {
        // Values checked against `np.float32(x).astype(np.float16)`.
        assert_eq!(through_fp16(1.0), 1.0);
        assert_eq!(through_fp16(0.1), 0.099975586);
        assert_eq!(through_fp16(-0.3333333), -0.33325195);
        assert_eq!(through_fp16(65520.0), f32::INFINITY);
        assert_eq!(through_fp16(1e-8), 0.0);
        assert_eq!(through_fp16(6.0e-8), 5.9604645e-8);
        // 1 + 2^-11 is halfway between 1 and 1 + 2^-10: ties to the even mantissa.
        assert_eq!(through_fp16(1.0 + 2f32.powi(-11)), 1.0);
        assert_eq!(
            through_fp16(1.0 + 3.0 * 2f32.powi(-11)),
            1.0 + 2f32.powi(-9)
        );
    }
}
