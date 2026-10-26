//! The 2-D layout the Title map and the Tag network are drawn with
//! (`plexdb/explore.py` `_embed_2d`, which used UMAP).
//!
//! Rows are sparse vectors, normalised to unit length and laid out with cosine distance by
//! `holomap` 0.3.0 (maintainer mnmal-ai), the crate the owner picked from the plex-db-ex-f58.21
//! spike. `holomap`'s exact nearest-neighbour search is quadratic in the number of columns as
//! well as rows and did not finish the movie inputs (11,636 x 11,522) in 15 minutes, so a
//! vector with more than [`PROJECTED_DIMS`] columns is first reduced to that many by a seeded
//! random projection onto +/-1 signs. The signs come from [`sign_bits`], so one seed gives
//! one layout on every run and every machine.

use holomap::{Holomap, Metric};

/// The columns a vector is projected down to when it has more.
pub const PROJECTED_DIMS: usize = 256;

const NEIGHBOURS: usize = 15;
const MIN_DIST: f32 = 0.1;
const SPREAD: f32 = 1.0;

/// One sparse cell: `(row, column, value)`.
pub type Cell = (usize, usize, f64);

/// `rows` x `cols` sparse vectors placed in the unit square so cosine-similar rows sit close
/// together: both axes scaled by one factor, so the longer one spans `[0, 1]` and the shorter
/// starts at 0. Nothing is clipped. Fewer than 5 rows or 3 columns is too little for a layout
/// to mean anything, and the rows are laid on a line instead.
pub fn embed_2d(
    rows: usize,
    cols: usize,
    cells: &[Cell],
    seed: u64,
) -> Result<Vec<[f64; 2]>, String> {
    if rows < 5 || cols < 3 {
        return Ok((0..rows)
            .map(|i| [i as f64 / (rows.saturating_sub(1)).max(1) as f64, 0.5])
            .collect());
    }
    let (dense, dims) = reduce(rows, cols, cells, seed);
    let flat = Holomap::builder(seed)
        .n_neighbors(NEIGHBOURS.min(rows - 1))
        .min_dist(MIN_DIST)
        .spread(SPREAD)
        .metric(Metric::Cosine)
        .fit_transform(&dense, dims)
        .map_err(|err| format!("holomap could not lay out {rows} rows: {err}"))?;
    if flat.len() != rows * 2 {
        return Err(format!(
            "holomap returned {} values for {rows} rows",
            flat.len()
        ));
    }
    let coords: Vec<[f64; 2]> = flat
        .chunks(2)
        .map(|c| [f64::from(c[0]), f64::from(c[1])])
        .collect();
    Ok(scale_to_unit(&coords))
}

/// Subtract each axis's minimum and divide both by the longer span.
fn scale_to_unit(coords: &[[f64; 2]]) -> Vec<[f64; 2]> {
    let mut low = [f64::INFINITY; 2];
    let mut high = [f64::NEG_INFINITY; 2];
    for c in coords {
        for k in 0..2 {
            low[k] = low[k].min(c[k]);
            high[k] = high[k].max(c[k]);
        }
    }
    let span = (high[0] - low[0]).max(high[1] - low[1]);
    let span = if span > 0.0 { span } else { 1.0 };
    coords
        .iter()
        .map(|c| [(c[0] - low[0]) / span, (c[1] - low[1]) / span])
        .collect()
}

/// The normalised rows as a dense row-major matrix, projected when they are wide, and the
/// number of columns it has.
fn reduce(rows: usize, cols: usize, cells: &[Cell], seed: u64) -> (Vec<f32>, usize) {
    let dims = cols.min(PROJECTED_DIMS);
    let mut norms = vec![0f64; rows];
    for &(r, _, v) in cells {
        norms[r] += v * v;
    }
    let mut dense = vec![0f32; rows * dims];
    let mut signs = [0f32; PROJECTED_DIMS];
    let mut projected_column = usize::MAX;
    // Cells arrive in any order; sort by column so a column's signs are made once.
    let mut by_column: Vec<&Cell> = cells.iter().collect();
    by_column.sort_by_key(|cell| (cell.1, cell.0));
    for &&(r, c, v) in &by_column {
        let norm = norms[r].sqrt();
        if norm == 0.0 {
            continue;
        }
        let value = (v / norm) as f32;
        let row = &mut dense[r * dims..(r + 1) * dims];
        if cols <= PROJECTED_DIMS {
            row[c] += value;
            continue;
        }
        if projected_column != c {
            fill_signs(seed, c, &mut signs);
            projected_column = c;
        }
        for k in 0..PROJECTED_DIMS {
            row[k] += value * signs[k];
        }
    }
    (dense, dims)
}

/// Column `column`'s [`PROJECTED_DIMS`] signs, as +1.0 and -1.0.
fn fill_signs(seed: u64, column: usize, signs: &mut [f32; PROJECTED_DIMS]) {
    for (word, bits) in sign_bits(seed, column).into_iter().enumerate() {
        for bit in 0..64 {
            signs[word * 64 + bit] = if bits >> bit & 1 == 1 { 1.0 } else { -1.0 };
        }
    }
}

/// The 256 random bits of one projection column, one per output dimension: SplitMix64
/// (Steele, Lea and Flood, 2014) started from the seed and the column, so a column's bits
/// depend on nothing else in the input.
pub fn sign_bits(seed: u64, column: usize) -> [u64; PROJECTED_DIMS / 64] {
    let mut state = seed ^ (column as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    let mut words = [0u64; PROJECTED_DIMS / 64];
    for word in &mut words {
        state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        *word = z ^ (z >> 31);
    }
    words
}
