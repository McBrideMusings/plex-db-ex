//! `refresh-tagnetwork`: redraw Plex DB-EX's stored default Tag network of each kind
//! (`plexdb/tagnetwork.py`, `plexdb/explore.py` `tag_network`).
//!
//! The network is every tag on at least [`NETWORK_MIN_DF`] titles, positioned by which titles
//! carry it and linked to its [`NETWORK_EDGES_PER_NODE`] strongest co-tags. Nodes and edges
//! are exact counts and are the same however the layout is drawn; only the positions come
//! from [`crate::layout`]. Stored in `tag_network`, `tag_network_edge` and
//! `tag_network_state`, replaced in one transaction for every redrawn kind, and the only
//! code that writes them (ADR-0001).

use std::collections::{BTreeSet, HashMap, HashSet};

use rusqlite::{Connection, params};

use crate::decisions::thousands;
use crate::gated::{config, open};
use crate::keyword_views::{KINDS, fingerprint, keyword_rows, merge_map};
use crate::layout::embed_2d;
use crate::refresh_map::{SEED, round};
use crate::staleness::Now;
use crate::steps::Failure;

pub const NAME: &str = "refresh-tagnetwork";

/// A tag on fewer titles than this cannot draw a meaningful edge or earn a position of its
/// own.
const NETWORK_MIN_DF: usize = 3;

/// Each node keeps only its strongest links, so a tag central to the library does not draw a
/// line to every tag it has ever shared one title with.
const NETWORK_EDGES_PER_NODE: usize = 6;

/// Names how a network is drawn, as `MAP_RECIPE` does the map's. `plexdb/explore.py` holds the
/// same string.
pub const NETWORK_RECIPE: &str = "holomap-cosine-rp256-s0-full-df3-nn15-md0.1-sp1.0-uniform-epn6";

pub fn network_fingerprint(conn: &Connection, kind: &str) -> rusqlite::Result<String> {
    fingerprint(conn, kind, NETWORK_RECIPE)
}

#[derive(Debug, Clone, PartialEq)]
pub struct Node {
    pub value: String,
    pub df: usize,
    pub x: f64,
    pub y: f64,
}

#[derive(Debug)]
pub struct Network {
    /// Every tag on at least [`NETWORK_MIN_DF`] titles, by name.
    pub nodes: Vec<Node>,
    /// `(a, b, shared titles)`, `a < b`, each pair once, strongest first then by name.
    pub edges: Vec<(String, String, usize)>,
}

#[derive(Debug, PartialEq)]
pub struct Refreshed {
    pub kind: &'static str,
    pub redrawn: bool,
    pub nodes: usize,
    pub seconds: f64,
}

/// The first two stages of a network, before the slow layout: which tags qualify and how
/// many titles each is on, and the strongest links between them.
#[derive(Debug)]
pub struct Prepared {
    /// Every qualifying tag, by name; a tag's index here is its row in the layout.
    pub values: Vec<String>,
    /// Titles carrying `values[i]`.
    pub df: Vec<usize>,
    /// `(a, b, shared titles)`, `a < b`, each pair once, strongest first then by name.
    pub edges: Vec<(String, String, usize)>,
    cells: Vec<(usize, usize, f64)>,
    titles: usize,
}

/// The whole tag network of one kind.
pub fn draw(rows: &[(String, String)], seed: u64) -> Result<Network, String> {
    layout(prepare(rows, &HashSet::new()), seed)
}

/// The vocabulary and edges of the network of one kind with the tags in `exclude` left out.
pub fn prepare(rows: &[(String, String)], exclude: &HashSet<String>) -> Prepared {
    let mut df: HashMap<&str, usize> = HashMap::new();
    for (_, value) in rows {
        *df.entry(value).or_default() += 1;
    }
    let mut values: Vec<&str> = df
        .iter()
        .filter(|(value, count)| **count >= NETWORK_MIN_DF && !exclude.contains(**value))
        .map(|(value, _)| *value)
        .collect();
    values.sort_unstable();
    let vocab: HashMap<&str, usize> = values.iter().enumerate().map(|(i, v)| (*v, i)).collect();

    // Titles carrying at least one tag in the vocabulary, each with its tags as vocab indexes.
    let mut by_title: Vec<(&str, Vec<usize>)> = Vec::new();
    for (item_id, value) in rows {
        let Some(&tag) = vocab.get(value.as_str()) else {
            continue;
        };
        match by_title.last_mut() {
            Some((last, tags)) if *last == item_id => tags.push(tag),
            _ => by_title.push((item_id, vec![tag])),
        }
    }

    let mut cells = Vec::new();
    let mut together: Vec<HashMap<usize, usize>> = vec![HashMap::new(); values.len()];
    for (col, (_, tags)) in by_title.iter().enumerate() {
        for &a in tags {
            cells.push((a, col, 1.0));
            let counts = &mut together[a];
            for &b in tags {
                if a != b {
                    *counts.entry(b).or_default() += 1;
                }
            }
        }
    }

    let mut kept: BTreeSet<(usize, String, String)> = BTreeSet::new();
    for (a, counts) in together.iter().enumerate() {
        let mut ranked: Vec<(&usize, &usize)> = counts.iter().collect();
        ranked.sort_by(|x, y| y.1.cmp(x.1).then_with(|| values[*x.0].cmp(values[*y.0])));
        for (&b, &count) in ranked.into_iter().take(NETWORK_EDGES_PER_NODE) {
            let (low, high) = if values[a] < values[b] {
                (values[a], values[b])
            } else {
                (values[b], values[a])
            };
            // The set holds each pair once per shared count; the count is the pair's own, so
            // both ends of a pair add the same one.
            kept.insert((usize::MAX - count, low.to_string(), high.to_string()));
        }
    }
    let edges = kept
        .into_iter()
        .map(|(inverse, a, b)| (a, b, usize::MAX - inverse))
        .collect();

    Prepared {
        df: values.iter().map(|value| df[value]).collect(),
        values: values.iter().map(|value| value.to_string()).collect(),
        edges,
        cells,
        titles: by_title.len(),
    }
}

/// The slow stage: place every tag of `prepared` by which titles carry it.
pub fn layout(prepared: Prepared, seed: u64) -> Result<Network, String> {
    let coords = embed_2d(
        prepared.values.len(),
        prepared.titles,
        &prepared.cells,
        seed,
    )?;
    let nodes = prepared
        .values
        .into_iter()
        .zip(prepared.df)
        .zip(&coords)
        .map(|((value, df), c)| Node {
            value,
            df,
            x: round(c[0], 5),
            y: round(c[1], 5),
        })
        .collect();
    Ok(Network {
        nodes,
        edges: prepared.edges,
    })
}

/// Bring the stored default tag network of every kind up to date.
pub fn refresh(conn: &mut Connection, now: &Now) -> Result<Vec<Refreshed>, String> {
    let sql = |err: rusqlite::Error| err.to_string();
    let folded = merge_map(conn).map_err(sql)?;
    let mut results = Vec::new();
    let mut drawn = Vec::new();
    for kind in KINDS {
        let current = network_fingerprint(conn, kind).map_err(sql)?;
        let stored: Option<String> = conn
            .query_row(
                "SELECT fingerprint FROM tag_network_state WHERE kind = ?",
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
            let nodes: usize = conn
                .query_row(
                    "SELECT COUNT(*) FROM tag_network WHERE kind = ?",
                    [kind],
                    |row| row.get(0),
                )
                .map_err(sql)?;
            results.push(Refreshed {
                kind,
                redrawn: false,
                nodes,
                seconds: 0.0,
            });
            continue;
        }
        let started = std::time::Instant::now();
        let rows = keyword_rows(conn, kind, &folded).map_err(sql)?;
        let network = draw(&rows, SEED)?;
        results.push(Refreshed {
            kind,
            redrawn: true,
            nodes: network.nodes.len(),
            seconds: started.elapsed().as_secs_f64(),
        });
        drawn.push((kind, current, network));
    }
    if drawn.is_empty() {
        return Ok(results);
    }
    let computed_at = now.iso();
    let tx = conn.transaction().map_err(sql)?;
    for (kind, current, network) in &drawn {
        tx.execute("DELETE FROM tag_network WHERE kind = ?", [kind])
            .map_err(sql)?;
        tx.execute("DELETE FROM tag_network_edge WHERE kind = ?", [kind])
            .map_err(sql)?;
        {
            let mut insert = tx
                .prepare("INSERT INTO tag_network (kind, value, df, x, y) VALUES (?, ?, ?, ?, ?)")
                .map_err(sql)?;
            for n in &network.nodes {
                insert
                    .execute(params![kind, n.value, n.df as i64, n.x, n.y])
                    .map_err(sql)?;
            }
        }
        {
            let mut insert = tx
                .prepare("INSERT INTO tag_network_edge (kind, a, b, shared) VALUES (?, ?, ?, ?)")
                .map_err(sql)?;
            for (a, b, shared) in &network.edges {
                insert
                    .execute(params![kind, a, b, *shared as i64])
                    .map_err(sql)?;
            }
        }
        tx.execute(
            "INSERT INTO tag_network_state (kind, fingerprint, computed_at) \
             VALUES (?, ?, ?) ON CONFLICT (kind) DO UPDATE SET \
             fingerprint = excluded.fingerprint, computed_at = excluded.computed_at",
            params![kind, current, computed_at],
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
                    "tag network: {}: drew {} node(s) in {:.1}s",
                    r.kind,
                    thousands(r.nodes),
                    r.seconds
                )
            } else {
                format!(
                    "tag network: {}: current, {} node(s) kept",
                    r.kind,
                    thousands(r.nodes)
                )
            }
        })
        .collect()
}

/// `plexdb refresh-tagnetwork`.
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
