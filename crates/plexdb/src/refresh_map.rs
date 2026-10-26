//! `refresh-map`: redraw Plex DB-EX's stored default Title map of each kind
//! (`plexdb/titlemap.py`, `plexdb/explore.py` `title_map` and `region_labels`).
//!
//! With no noise excluded the map is a pure function of the keyword rows, so it is drawn
//! once and kept, with its region labels, in `title_map`, `title_map_label` and
//! `title_map_state`. A kind whose stored fingerprint equals the current one is left alone.
//! Every other kind is drawn first, with no write lock held beyond the connection's own, and
//! then all redrawn kinds replace their rows in one transaction, so a reader sees the old maps
//! or the new ones. This is the only code that writes those tables (ADR-0001).

use std::collections::{HashMap, HashSet};

use rusqlite::{Connection, params};

use crate::decisions::thousands;
use crate::gated::{config, open};
use crate::keyword_views::{KINDS, fingerprint, keyword_rows, merge_map};
use crate::layout::embed_2d;
use crate::staleness::Now;
use crate::steps::Failure;

pub const NAME: &str = "refresh-map";

/// A tag on fewer titles than this cannot place a title near any other, so it is left out of
/// the map's vectors.
const MAP_MIN_DF: usize = 2;

/// Region labels come from a k-d split of the map: depth `d` has up to `2**d` regions holding
/// about the same number of titles each. Labels start at depth 2 (four regions) and stop at
/// depth 9 (512), or where a region has fewer than `LABEL_MIN_TITLES` titles.
const LABEL_FIRST_DEPTH: usize = 2;
const LABEL_LAST_DEPTH: usize = 9;
const LABEL_MIN_TITLES: usize = 8;

/// The one seed every map is drawn with, so a store gives one layout.
pub const SEED: u64 = 0;

/// Names how a map is drawn. It is part of a stored map's fingerprint, so changing the
/// layout, any constant above or the seed, and changing this string with it, makes every
/// stored map stale and the next refresh redraws it. `plexdb/explore.py` holds the same string.
pub const MAP_RECIPE: &str =
    "holomap-cosine-rp256-s0-full-df2-nn15-md0.1-sp1.0-uniform-labels2-9-min8";

pub fn map_fingerprint(conn: &Connection, kind: &str) -> rusqlite::Result<String> {
    fingerprint(conn, kind, MAP_RECIPE)
}

/// A title's position, in the unit square.
#[derive(Debug, Clone, PartialEq)]
pub struct Point {
    pub item_id: String,
    pub x: f64,
    pub y: f64,
}

/// A named region of the map: `node` is its heap number, `x`/`y` the median of its titles and
/// `width`/`height` the span of the middle 80% of them, in map units.
#[derive(Debug, Clone, PartialEq)]
pub struct Label {
    pub node: i64,
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    pub titles: i64,
    pub tag: String,
}

#[derive(Debug)]
pub struct Drawn {
    pub points: Vec<Point>,
    /// Titles with keywords that are not on the map.
    pub unplaced: usize,
    pub labels: Vec<Label>,
}

#[derive(Debug, PartialEq)]
pub struct Refreshed {
    pub kind: &'static str,
    /// False when the stored map already matched the keyword rows.
    pub redrawn: bool,
    pub placed: usize,
    pub seconds: f64,
}

/// Python's `round(x, places)`: the nearest decimal to the float's exact value, ties to even.
/// Rust's `{:.N}` formatting rounds the exact value the same way.
pub fn round(x: f64, places: usize) -> f64 {
    format!("{x:.places$}").parse().unwrap_or(x)
}

/// Every title of the kind placed in 2-D so titles with similar keywords sit close, and the
/// labels of its regions. A title's vector is its tags weighted by `1 + ln(n / df)`.
pub fn draw(rows: &[(String, String)], seed: u64) -> Result<Drawn, String> {
    draw_excluding(rows, &HashSet::new(), seed)
}

/// [`draw`] with the tags in `exclude` left out of the vectors and the labels. The weights still
/// count every row: a title carrying only excluded tags stays in `n` and is `unplaced`.
pub fn draw_excluding(
    rows: &[(String, String)],
    exclude: &HashSet<String>,
    seed: u64,
) -> Result<Drawn, String> {
    let mut df: HashMap<&str, usize> = HashMap::new();
    let mut carrying = 0usize;
    let mut last: Option<&str> = None;
    for (item_id, value) in rows {
        *df.entry(value).or_default() += 1;
        if last != Some(item_id.as_str()) {
            carrying += 1;
            last = Some(item_id);
        }
    }
    let n = carrying;
    let mut values: Vec<&str> = df
        .iter()
        .filter(|(value, count)| **count >= MAP_MIN_DF && !exclude.contains(**value))
        .map(|(value, _)| *value)
        .collect();
    values.sort_unstable();
    let vocab: HashMap<&str, usize> = values.iter().enumerate().map(|(i, v)| (*v, i)).collect();

    let mut ids: Vec<&str> = Vec::new();
    let mut placed: HashMap<&str, usize> = HashMap::new();
    let mut cells = Vec::new();
    for (item_id, value) in rows {
        let Some(&col) = vocab.get(value.as_str()) else {
            continue;
        };
        let row = *placed.entry(item_id).or_insert_with(|| {
            ids.push(item_id);
            ids.len() - 1
        });
        cells.push((row, col, 1.0 + (n as f64 / df[value.as_str()] as f64).ln()));
    }
    let coords = embed_2d(ids.len(), vocab.len(), &cells, seed)?;
    let points: Vec<Point> = ids
        .iter()
        .zip(&coords)
        .map(|(item_id, c)| Point {
            item_id: (*item_id).to_string(),
            x: round(c[0], 5),
            y: round(c[1], 5),
        })
        .collect();
    let labels = region_labels(rows, &points, exclude);
    Ok(Drawn {
        unplaced: n - points.len(),
        points,
        labels,
    })
}

/// Tag labels for the regions of a drawn map, coarse to fine.
///
/// Regions are the nodes of a k-d split. A region is named by the tag with the highest
/// count-in-region times IDF, the weight the map itself uses, so a tag common everywhere does
/// not name every region. A tag that already names an ancestor is skipped, so each zoom level
/// brings new words. A tag on fewer than `MAP_MIN_DF` titles never labels anything.
fn region_labels(
    rows: &[(String, String)],
    points: &[Point],
    exclude: &HashSet<String>,
) -> Vec<Label> {
    let mut df: HashMap<&str, usize> = HashMap::new();
    let mut titles = 0usize;
    let mut last: Option<&str> = None;
    for (item_id, value) in rows {
        *df.entry(value).or_default() += 1;
        if last != Some(item_id.as_str()) {
            titles += 1;
            last = Some(item_id);
        }
    }
    let n = titles as f64;
    let mut tags: HashMap<&str, Vec<&str>> = HashMap::new();
    for (item_id, value) in rows {
        if df[value.as_str()] >= MAP_MIN_DF && !exclude.contains(value) {
            tags.entry(item_id).or_default().push(value);
        }
    }

    fn span(mut values: Vec<f64>) -> (f64, f64) {
        values.sort_by(|a, b| a.total_cmp(b));
        let last = (values.len() - 1) as f64;
        (
            values[values.len() / 2],
            values[(0.9 * last) as usize] - values[(0.1 * last) as usize],
        )
    }

    let mut labels = Vec::new();
    // (heap number, indexes into `points`, tags already naming an ancestor)
    let mut stack: Vec<(usize, Vec<usize>, Vec<&str>)> =
        vec![(0, (0..points.len()).collect(), Vec::new())];
    while let Some((node, members, mut used)) = stack.pop() {
        let depth = (usize::BITS - (node + 1).leading_zeros() - 1) as usize;
        if depth > LABEL_LAST_DEPTH || members.len() < LABEL_MIN_TITLES {
            continue;
        }
        if depth >= LABEL_FIRST_DEPTH {
            let mut counts: HashMap<&str, usize> = HashMap::new();
            for &m in &members {
                for tag in tags.get(points[m].item_id.as_str()).into_iter().flatten() {
                    *counts.entry(tag).or_default() += 1;
                }
            }
            let floor = 2f64.max(0.05 * members.len() as f64);
            let named = counts
                .iter()
                .filter(|(tag, count)| **count as f64 >= floor && !used.contains(tag))
                .map(|(tag, count)| (*count as f64 * (1.0 + (n / df[*tag] as f64).ln()), *tag))
                .min_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(b.1)));
            if let Some((_, tag)) = named {
                let (x, width) = span(members.iter().map(|&m| points[m].x).collect());
                let (y, height) = span(members.iter().map(|&m| points[m].y).collect());
                labels.push(Label {
                    node: node as i64,
                    x: round(x, 4),
                    y: round(y, 4),
                    width: round(width, 4),
                    height: round(height, 4),
                    titles: members.len() as i64,
                    tag: tag.to_string(),
                });
                used.push(tag);
            }
        }
        let range = |axis: fn(&Point) -> f64| {
            let (low, high) = members
                .iter()
                .fold((f64::INFINITY, f64::NEG_INFINITY), |a, &m| {
                    (a.0.min(axis(&points[m])), a.1.max(axis(&points[m])))
                });
            high - low
        };
        let x_axis: fn(&Point) -> f64 = |p| p.x;
        let y_axis: fn(&Point) -> f64 = |p| p.y;
        let axis = if range(x_axis) >= range(y_axis) {
            x_axis
        } else {
            y_axis
        };
        let mut ordered = members;
        ordered.sort_by(|&a, &b| {
            axis(&points[a])
                .total_cmp(&axis(&points[b]))
                .then_with(|| points[a].item_id.cmp(&points[b].item_id))
        });
        let second = ordered.split_off(ordered.len() / 2);
        stack.push((2 * node + 1, ordered, used.clone()));
        stack.push((2 * node + 2, second, used));
    }
    labels.sort_by_key(|label| label.node);
    labels
}

/// Bring the stored default map of every kind up to date.
pub fn refresh(conn: &mut Connection, now: &Now) -> Result<Vec<Refreshed>, String> {
    let sql = |err: rusqlite::Error| err.to_string();
    let folded = merge_map(conn).map_err(sql)?;
    let mut results = Vec::new();
    let mut drawn = Vec::new();
    for kind in KINDS {
        let current = map_fingerprint(conn, kind).map_err(sql)?;
        let stored: Option<String> = conn
            .query_row(
                "SELECT fingerprint FROM title_map_state WHERE kind = ?",
                [kind],
                |row| row.get(0),
            )
            .map(Some)
            .or_else(|err| match err {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })
            .map_err(sql)?;
        if stored.as_deref() == Some(current.as_str()) {
            let placed: usize = conn
                .query_row(
                    "SELECT COUNT(*) FROM title_map WHERE kind = ?",
                    [kind],
                    |row| row.get(0),
                )
                .map_err(sql)?;
            results.push(Refreshed {
                kind,
                redrawn: false,
                placed,
                seconds: 0.0,
            });
            continue;
        }
        let started = std::time::Instant::now();
        let rows = keyword_rows(conn, kind, &folded).map_err(sql)?;
        let map = draw(&rows, SEED)?;
        results.push(Refreshed {
            kind,
            redrawn: true,
            placed: map.points.len(),
            seconds: started.elapsed().as_secs_f64(),
        });
        drawn.push((kind, current, map));
    }
    if drawn.is_empty() {
        return Ok(results);
    }
    let computed_at = now.iso();
    let tx = conn.transaction().map_err(sql)?;
    for (kind, current, map) in &drawn {
        tx.execute("DELETE FROM title_map WHERE kind = ?", [kind])
            .map_err(sql)?;
        {
            let mut insert = tx
                .prepare("INSERT INTO title_map (kind, item_id, x, y) VALUES (?, ?, ?, ?)")
                .map_err(sql)?;
            for p in &map.points {
                insert
                    .execute(params![kind, p.item_id, p.x, p.y])
                    .map_err(sql)?;
            }
        }
        tx.execute("DELETE FROM title_map_label WHERE kind = ?", [kind])
            .map_err(sql)?;
        {
            let mut insert = tx
                .prepare(
                    "INSERT INTO title_map_label \
                     (kind, node, x, y, width, height, titles, value) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                )
                .map_err(sql)?;
            for l in &map.labels {
                insert
                    .execute(params![
                        kind, l.node, l.x, l.y, l.width, l.height, l.titles, l.tag
                    ])
                    .map_err(sql)?;
            }
        }
        tx.execute(
            "INSERT INTO title_map_state (kind, fingerprint, unplaced, computed_at) \
             VALUES (?, ?, ?, ?) ON CONFLICT (kind) DO UPDATE SET \
             fingerprint = excluded.fingerprint, unplaced = excluded.unplaced, \
             computed_at = excluded.computed_at",
            params![kind, current, map.unplaced as i64, computed_at],
        )
        .map_err(sql)?;
    }
    tx.commit().map_err(sql)?;
    Ok(results)
}

pub fn describe(results: &[Refreshed]) -> Vec<String> {
    results
        .iter()
        .map(|r| {
            if r.redrawn {
                format!(
                    "title map: {}: drew {} title(s) in {:.1}s",
                    r.kind,
                    thousands(r.placed),
                    r.seconds
                )
            } else {
                format!(
                    "title map: {}: current, {} title(s) kept",
                    r.kind,
                    thousands(r.placed)
                )
            }
        })
        .collect()
}

/// `plexdb refresh-map`.
pub fn run(args: &[String]) -> Result<(), Failure> {
    if !args.is_empty() {
        return Err(Failure::Usage(format!(
            "{NAME} takes no arguments, got {}",
            args.join(" ")
        )));
    }
    let mut store = open(&config()?)?;
    let results = refresh(&mut store.conn, &Now::read()).map_err(Failure::Failed)?;
    for line in describe(&results) {
        println!("{line}");
    }
    Ok(())
}
