//! What the stored Title map and Tag network are drawn from: every keyword a title of one
//! kind carries, with merged keywords folded into the one they merge with
//! (`plexdb/explore.py` `keyword_rows`, `plexdb/merge_review.py` `merge_map`, ADR-0018), and
//! the fingerprint that says whether a stored view still matches those rows.
//!
//! The fingerprint is a published rule: Plex DB-EX compares a stored view's fingerprint with
//! the one it computes from the store, so `plexdb/explore.py` computes the same bytes. It is
//! `<recipe>|<keyword digest>|<merge digest>`. Each digest is a SHA-256 over a sequence of
//! rows; a row is its fields in order, each as one byte `0` for NULL, or `1`, the length as a
//! big-endian `u32`, then the UTF-8 bytes. The keyword digest takes every keyword row of a
//! title of the kind (`item_id`, `source`, `value`, `fetched_at`) ordered by `item_id`,
//! `source`, `value`; the merge digest takes every `(keyword, target)` of the merge map
//! ordered by keyword.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use rusqlite::Connection;
use rusqlite::types::ValueRef;
use sha2::{Digest, Sha256};

pub const KINDS: [&str; 2] = ["movie", "show"];

/// A pair with `jev_score` at or above this merges, unless a person decided it.
const MERGE_AT: f64 = 0.9;
const FILM_SUFFIX_MODEL: &str = crate::keyword_verdicts::FILM_SUFFIX_MODEL;

/// Each keyword a merged pair folds away, mapped to the keyword it folds into.
///
/// Merged pairs chain: `a`~`b` and `b`~`c` make one group. A group folds into its member the
/// most titles carry (ties by name), except that the "X film" side of a film-suffix rule pair
/// never names a group, which folds toward the bare "X" however many titles say "X film". A
/// keyword in no merged pair is absent.
pub fn merge_map(conn: &Connection) -> rusqlite::Result<HashMap<String, String>> {
    let suffixed: BTreeSet<String> = conn
        .prepare(
            "SELECT CASE WHEN keyword_a LIKE '% film' THEN keyword_a ELSE keyword_b END \
             FROM keyword_pairs WHERE jev_model = ?",
        )?
        .query_map([FILM_SUFFIX_MODEL], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let mut parent: BTreeMap<String, String> = BTreeMap::new();
    fn root(parent: &mut BTreeMap<String, String>, keyword: &str) -> String {
        let mut current = keyword.to_string();
        while let Some(next) = parent.get(&current).cloned() {
            if next == current {
                break;
            }
            let grand = parent.get(&next).cloned().unwrap_or_else(|| next.clone());
            parent.insert(current.clone(), grand);
            current = next;
        }
        current
    }
    let pairs: Vec<(String, String)> = conn
        .prepare(
            "SELECT keyword_a, keyword_b FROM keyword_pairs WHERE decision = 'accepted' \
             OR (decision IS NULL AND jev_score >= ?)",
        )?
        .query_map([MERGE_AT], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    for (a, b) in &pairs {
        let (ra, rb) = (root(&mut parent, a), root(&mut parent, b));
        if ra != rb {
            parent.insert(ra, rb);
        }
    }
    if parent.is_empty() {
        return Ok(HashMap::new());
    }
    let mut groups: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let keys: Vec<String> = parent.keys().cloned().collect();
    for keyword in keys {
        let top = root(&mut parent, &keyword);
        groups.entry(top).or_default().insert(keyword);
    }
    for (top, group) in &mut groups {
        group.insert(top.clone());
    }
    let members: BTreeSet<&String> = groups.values().flatten().collect();
    let mut carried: HashMap<String, i64> = HashMap::new();
    {
        let mut count = conn.prepare(
            "SELECT COUNT(DISTINCT item_id) FROM enrichment \
             WHERE namespace = 'keywords' AND key = 'keyword' AND value = ?",
        )?;
        for member in members {
            carried.insert(member.clone(), count.query_row([member], |row| row.get(0))?);
        }
    }
    let mut folded = HashMap::new();
    for group in groups.values() {
        let into = group
            .iter()
            .min_by_key(|k| {
                (
                    suffixed.contains(*k),
                    -carried.get(*k).copied().unwrap_or(0),
                    (*k).clone(),
                )
            })
            .expect("a group has a member");
        for keyword in group {
            if keyword != into {
                folded.insert(keyword.clone(), into.clone());
            }
        }
    }
    Ok(folded)
}

/// Every `(item_id, keyword)` of a title of `kind`, each pair once, with every merged keyword
/// folded into the one it merges with, ordered by `item_id` then keyword.
pub fn keyword_rows(
    conn: &Connection,
    kind: &str,
    folded: &HashMap<String, String>,
) -> rusqlite::Result<Vec<(String, String)>> {
    let mut statement = conn.prepare(
        "SELECT DISTINCT e.item_id, e.value FROM enrichment e JOIN items i USING (item_id) \
         WHERE e.namespace = 'keywords' AND e.key = 'keyword' AND i.type = ?",
    )?;
    let rows = statement.query_map([kind], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut distinct = BTreeSet::new();
    for row in rows {
        let (item_id, value) = row?;
        let value = folded.get(&value).cloned().unwrap_or(value);
        distinct.insert((item_id, value));
    }
    Ok(distinct.into_iter().collect())
}

fn field(sha: &mut Sha256, value: ValueRef<'_>) {
    let text = match value {
        ValueRef::Null => {
            sha.update([0u8]);
            return;
        }
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => {
            String::from_utf8_lossy(bytes).into_owned()
        }
        ValueRef::Integer(n) => n.to_string(),
        ValueRef::Real(x) => x.to_string(),
    };
    text_field(sha, Some(&text));
}

fn text_field(sha: &mut Sha256, value: Option<&str>) {
    match value {
        None => sha.update([0u8]),
        Some(text) => {
            sha.update([1u8]);
            sha.update((text.len() as u32).to_be_bytes());
            sha.update(text.as_bytes());
        }
    }
}

/// `<recipe>|<keyword digest>|<merge digest>`: a digest of what a stored default view of
/// `kind` is drawn from. Any keyword row added, removed, re-stamped or re-valued, any title
/// changing type, any new merge verdict and any change to `recipe` changes it.
pub fn fingerprint(conn: &Connection, kind: &str, recipe: &str) -> rusqlite::Result<String> {
    let mut sha = Sha256::new();
    let mut statement = conn.prepare(
        "SELECT e.item_id, e.source, e.value, e.fetched_at \
         FROM enrichment e JOIN items i USING (item_id) \
         WHERE e.namespace = 'keywords' AND e.key = 'keyword' AND i.type = ? \
         ORDER BY e.item_id, e.source, e.value",
    )?;
    let mut rows = statement.query([kind])?;
    while let Some(row) = rows.next()? {
        for column in 0..4 {
            field(&mut sha, row.get_ref(column)?);
        }
    }
    let keywords = hex(&sha.finalize());
    let mut sha = Sha256::new();
    let merges: BTreeMap<String, String> = merge_map(conn)?.into_iter().collect();
    for (keyword, target) in &merges {
        text_field(&mut sha, Some(keyword));
        text_field(&mut sha, Some(target));
    }
    Ok(format!("{recipe}|{keywords}|{}", hex(&sha.finalize())))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
