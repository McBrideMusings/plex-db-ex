//! Finding synonym keywords: embeddings propose pairs, Jev judges them, and a person's
//! decisions are folded in from a file (`plexdb/synonyms.py`, ADR-0018).
//!
//! `keyword_pairs` holds Jev's answer for every pair it was asked about, a person's
//! decision where one was made (a pair only a person decided has no answer, ADR-0020),
//! and nothing else the writer concludes: no cosine, no status, no "counts as merged".
//! A pair is judged once. A pair that has a row is never asked again, and the judge step
//! never touches its `decision`.
//!
//! A pair Jev refuses with a 400 or 422 gets a row with `jev_error` set and no score or
//! model, so it is not asked again and does not hold up its batch. Any other failure (429
//! exhausted, 5xx, a bad key, no connection) stops the run.
//!
//! **Which pairs are asked about.** Every run examines every stored keyword: each
//! proposes its 10 nearest neighbours in the whole stored vocabulary with cosine >=
//! [`MIN_COSINE`], and a proposed pair that has a row is left alone. Nothing records that
//! a keyword was examined, so there is no state a killed run can leave wrong, and a
//! keyword that gains a new neighbour proposes it on the next run. Examining is a matrix
//! product over cached vectors and sends no request.
//!
//! A kill partway leaves valid rows behind, because keywords are processed in batches of
//! [`KEYWORD_BATCH`] and each batch is judged completely before its rows are written in
//! one transaction. The next run proposes the same pairs again and asks Jev only about
//! those with no row.
//!
//! `limit` caps how many pairs one run asks Jev about, so a first run over a whole
//! vocabulary can be taken in pieces.
//!
//! **What is embedded.** The readable surface form (`readable_surfaces`, the shortest per
//! keyword), not the stored stem, and with no task prefix. The pair is stored under the
//! stems, and Jev is asked about the surfaces.
//!
//! **Embeddings are cached in a file, not the store.** Every run examines the whole
//! vocabulary, so without a cache each sweep would embed all of it again. The cache is
//! [`EMBEDDING_CACHE_FILE`] beside the store: one unit vector per surface text and the
//! model that made them. It is derived and disposable: deleting it, or changing the
//! model, costs one re-embed. A sweep embeds only the texts the cache lacks, so a night
//! with no new keyword makes no embedding request.

use std::collections::{BTreeSet, HashSet};
use std::path::Path;

use plexdb_keywords::readable_surfaces;
use rusqlite::{Connection, params};

use crate::decisions::py_repr;
use crate::embed::{Embedder, EmbeddingError, load_cache, save_cache};
use crate::jev::{Jev, Verdict, judge_all};
use crate::staleness::Now;

/// How many nearest neighbours each keyword proposes.
pub const NEIGHBOURS: usize = 10;
/// The lowest cosine a neighbour may have and still be proposed. It belongs to
/// `embed::MODEL`: embeddinggemma-300m cosines run higher than nomic's.
/// Measured 2026-10-20 over 24,219 keywords, top-10 neighbours, against the 1,116 merged
/// pairs in the store (accepted, or undecided with jev_score >= 0.9):
///
/// ```text
/// nomic-embed-text    0.75  recalls 1,108  43,662 proposals (1.80 per keyword)
/// embeddinggemma-300m 0.70  recalls 1,070 134,039 proposals (5.53 per keyword)
/// embeddinggemma-300m 0.75  recalls 1,047  79,084 proposals (3.27 per keyword)
/// embeddinggemma-300m 0.78  recalls 1,000  48,209 proposals (1.99 per keyword)
/// embeddinggemma-300m 0.80  recalls   936  32,197 proposals (1.33 per keyword)
/// ```
///
/// No gemma cut reaches nomic's recall (the ceiling at 0.50 is 1,085). 0.78 keeps the
/// proposals per keyword near nomic's. The labelled pairs are the ones nomic proposed, so
/// they favour nomic.
pub const MIN_COSINE: f32 = 0.78;
/// Texts per embeddings request.
pub const EMBED_BATCH: usize = 128;
/// Keywords examined per committed batch — about 330 pairs, about 10 seconds of Jev.
pub const KEYWORD_BATCH: usize = 200;
/// Jev requests in flight at once.
pub const JUDGE_WORKERS: usize = 8;

pub const EMBEDDING_CACHE_FILE: &str = "keyword-embeddings.bin";

/// Texts embedded between cache saves, so a sweep killed during the first full embed
/// keeps most of what it finished.
const SAVE_EVERY: usize = EMBED_BATCH * 20;

#[derive(Debug, Default, PartialEq, Eq)]
pub struct JudgeStats {
    pub keywords_stored: usize,
    /// Keywords this run proposed neighbours for (fewer than stored when `limit` ends it
    /// early).
    pub keywords_examined: usize,
    pub pairs_proposed: usize,
    /// Proposed pairs that already had a row, left alone.
    pub pairs_already_judged: usize,
    pub pairs_judged: usize,
    /// Pairs Jev refused with a 400 or 422, stored with `jev_error` and no score.
    pub pairs_unjudgeable: usize,
    pub batches_committed: usize,
}

#[derive(Debug, thiserror::Error)]
pub enum JudgeError {
    #[error("{0}")]
    Store(#[from] rusqlite::Error),
    #[error("{0}")]
    Embedding(#[from] EmbeddingError),
    #[error("{0}")]
    Jev(String),
    #[error("cannot write the embedding cache {path}: {source}")]
    Cache {
        path: String,
        source: std::io::Error,
    },
}

/// Judge every pair the stored keywords propose and write one row per pair.
///
/// `limit` caps how many pairs this run asks the judge about. `cache_path` is the
/// embedding cache file; `None` embeds everything each call.
pub fn find_synonym_pairs(
    conn: &mut Connection,
    embedder: &dyn Embedder,
    judge: &dyn Jev,
    cache_path: Option<&Path>,
    limit: Option<usize>,
    log: &mut dyn FnMut(&str),
) -> Result<JudgeStats, JudgeError> {
    let mut stats = JudgeStats::default();
    let surfaces = readable_surfaces(conn)?;
    let keywords: Vec<&String> = surfaces.keys().collect();
    stats.keywords_stored = keywords.len();
    if keywords.is_empty() {
        return Ok(stats);
    }

    let texts: Vec<String> = keywords.iter().map(|k| surfaces[*k].clone()).collect();
    let vectors = embed_all(embedder, &texts, cache_path, log)?;
    let mut remaining = limit;

    for (batch_index, batch) in keywords.chunks(KEYWORD_BATCH).enumerate() {
        if remaining == Some(0) {
            break;
        }
        let start = batch_index * KEYWORD_BATCH;
        let proposed = propose(start..start + batch.len(), &keywords, &vectors);
        stats.keywords_examined += batch.len();
        stats.pairs_proposed += proposed.len();
        let mut todo = Vec::new();
        for pair in &proposed {
            if !has_pair(conn, pair)? {
                todo.push(pair);
            }
        }
        stats.pairs_already_judged += proposed.len() - todo.len();
        if let Some(left) = remaining.as_mut() {
            todo.truncate(*left);
            *left -= todo.len();
        }
        if todo.is_empty() {
            continue;
        }
        let verdicts = judge_all(
            |pair: &&(String, String)| judge.judge(&surfaces[&pair.0], &surfaces[&pair.1]),
            &todo,
            JUDGE_WORKERS,
        )
        .map_err(JudgeError::Jev)?;
        let judged_at = Now::read().iso();
        let tx = conn.transaction()?;
        for ((a, b), verdict) in todo.iter().map(|pair| (&pair.0, &pair.1)).zip(&verdicts) {
            match verdict {
                Err(refusal) => {
                    stats.pairs_unjudgeable += tx.execute(
                        "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_error, judged_at) \
                         VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        params![a, b, refusal, judged_at],
                    )?;
                    log(&format!(
                        "unjudgeable pair {} / {}: {refusal}",
                        py_repr(&surfaces[a]),
                        py_repr(&surfaces[b])
                    ));
                }
                Ok(Verdict { score, model }) => {
                    stats.pairs_judged += tx.execute(
                        "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, \
                         judged_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        params![a, b, score, model, judged_at],
                    )?;
                }
            }
        }
        tx.commit()?;
        stats.batches_committed += 1;
        log(&format!(
            "{}/{} keyword(s) examined, {} pair(s) judged",
            crate::decisions::thousands(stats.keywords_examined),
            crate::decisions::thousands(keywords.len()),
            crate::decisions::thousands(stats.pairs_judged)
        ));
    }
    Ok(stats)
}

/// One unit-length vector per row of `data`, so a dot product is a cosine.
pub struct Vectors {
    data: Vec<f32>,
    dim: usize,
}

impl Vectors {
    fn row(&self, i: usize) -> &[f32] {
        &self.data[i * self.dim..(i + 1) * self.dim]
    }

    fn rows(&self) -> usize {
        self.data.len().checked_div(self.dim).unwrap_or(0)
    }
}

/// Unit-length vectors, one row per text. Texts the cache holds are not sent to
/// `embedder`.
fn embed_all(
    embedder: &dyn Embedder,
    texts: &[String],
    cache_path: Option<&Path>,
    log: &mut dyn FnMut(&str),
) -> Result<Vectors, JudgeError> {
    let mut known = cache_path.map(load_cache).unwrap_or_default();
    let mut seen = HashSet::new();
    let missing: Vec<&String> = texts
        .iter()
        .filter(|text| !known.contains_key(*text) && seen.insert(*text))
        .collect();
    log(&format!(
        "embedding {} of {} keyword(s); {} cached",
        crate::decisions::thousands(missing.len()),
        crate::decisions::thousands(texts.len()),
        crate::decisions::thousands(texts.len() - missing.len())
    ));
    let save = |path: &Path, vectors: &[(&String, &Vec<f32>)]| {
        save_cache(path, vectors).map_err(|source| JudgeError::Cache {
            path: path.display().to_string(),
            source,
        })
    };
    for (chunk_index, chunk) in missing.chunks(EMBED_BATCH).enumerate() {
        let owned: Vec<String> = chunk.iter().map(|t| (*t).clone()).collect();
        let embedded = embedder.embed(&owned)?;
        for (text, vector) in chunk.iter().zip(embedded) {
            known.insert((*text).clone(), unit(vector));
        }
        let start = chunk_index * EMBED_BATCH;
        if let Some(path) = cache_path
            && (start + EMBED_BATCH).is_multiple_of(SAVE_EVERY)
        {
            let all: Vec<(&String, &Vec<f32>)> = known.iter().collect();
            save(path, &all)?;
        }
    }
    let dim = known[&texts[0]].len();
    let mut data = Vec::with_capacity(texts.len() * dim);
    for text in texts {
        let vector = &known[text];
        if vector.len() != dim {
            return Err(JudgeError::Embedding(EmbeddingError(
                "the embedding server returned vectors of different sizes".into(),
            )));
        }
        data.extend_from_slice(vector);
    }
    if let Some(path) = cache_path
        && !missing.is_empty()
    {
        let mut seen = HashSet::new();
        let current: Vec<(&String, &Vec<f32>)> = texts
            .iter()
            .filter(|text| seen.insert(*text))
            .map(|text| (text, &known[text]))
            .collect();
        save(path, &current)?;
    }
    Ok(Vectors { data, dim })
}

fn unit(vector: Vec<f32>) -> Vec<f32> {
    let norm = vector
        .iter()
        .map(|x| f64::from(*x) * f64::from(*x))
        .sum::<f64>()
        .sqrt() as f32;
    if norm == 0.0 {
        return vector;
    }
    vector.into_iter().map(|x| x / norm).collect()
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter()
        .zip(b)
        .map(|(x, y)| f64::from(*x) * f64::from(*y))
        .sum::<f64>() as f32
}

/// One row's neighbours: the [`NEIGHBOURS`] highest cosines against every other row,
/// kept when at least [`MIN_COSINE`].
fn neighbours(row: usize, vectors: &Vectors) -> Vec<usize> {
    let n = vectors.rows();
    let mut sims: Vec<f32> = (0..n)
        .map(|j| dot(vectors.row(row), vectors.row(j)))
        .collect();
    sims[row] = f32::NEG_INFINITY;
    let k = NEIGHBOURS.min(n);
    let mut order: Vec<usize> = (0..n).collect();
    order.select_nth_unstable_by(k - 1, |a, b| sims[*b].total_cmp(&sims[*a]));
    order[..k]
        .iter()
        .copied()
        .filter(|j| sims[*j] >= MIN_COSINE)
        .collect()
}

/// The distinct pairs the keywords in `rows` propose, ordered, each with `a < b`.
fn propose(
    rows: std::ops::Range<usize>,
    keywords: &[&String],
    vectors: &Vectors,
) -> Vec<(String, String)> {
    let rows: Vec<usize> = rows.collect();
    let workers = std::thread::available_parallelism()
        .map_or(1, std::num::NonZero::get)
        .min(rows.len())
        .max(1);
    let per_worker = rows.len().div_ceil(workers);
    let found: Vec<Vec<(usize, Vec<usize>)>> = std::thread::scope(|scope| {
        let handles: Vec<_> = rows
            .chunks(per_worker)
            .map(|chunk| {
                scope.spawn(move || {
                    chunk
                        .iter()
                        .map(|row| (*row, neighbours(*row, vectors)))
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| handle.join().expect("neighbour search"))
            .collect()
    });
    let mut pairs: BTreeSet<(String, String)> = BTreeSet::new();
    for (row, others) in found.into_iter().flatten() {
        for other in others {
            let (a, b) = (keywords[row], keywords[other]);
            pairs.insert(if a < b {
                (a.clone(), b.clone())
            } else {
                (b.clone(), a.clone())
            });
        }
    }
    pairs.into_iter().collect()
}

fn has_pair(conn: &Connection, pair: &(String, String)) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ?)",
        [&pair.0, &pair.1],
        |row| row.get(0),
    )
}
