//! What Plex DB-EX's decision tabs show and record: the Merges tab, the Roles tab, display
//! names, the Lists tab's decisions and the tag doctor (`plexdb/merge_review.py`,
//! `role_review.py`, `names.py`, `list_decisions.py` and `explore.py`'s `title_doctor`).
//!
//! A person's click never touches the store (ADR-0007, ADR-0017): it goes into a decisions
//! file ([`crate::decision_book`]) that a sweep folds in. Until then these functions lay the
//! file's entries over the table, so the page shows a click at once. Every function here
//! reads a store connection and a file's latest entries and returns a value; the server in
//! [`crate::explore`] owns the HTTP.

use std::collections::{BTreeSet, HashMap, HashSet};

use plexdb_keywords::{ROLES, normalize_keyword};
use plexdb_store::py_strip;
use rusqlite::{Connection, params};

use crate::decision_book::{Book, Key, Latest, RecordError, Spellings};
use crate::decisions::{Entry, py_repr, py_tuple_repr};
use crate::explore_views::{ExploreError, Overlay, named, optional, readable_forms};
use crate::keyword_views::merge_map;
use crate::merge_decisions::{MERGE_DECISIONS_FILE, SPEC as MERGE_SPEC};
use crate::name_decisions::{NAME_DECISIONS_FILE, NAME_MAX, SPEC as NAME_SPEC};
use crate::pyjson::Json;
use crate::role_decisions::{ROLE_DECISIONS_FILE, SPEC as ROLE_SPEC};

impl From<RecordError> for ExploreError {
    fn from(err: RecordError) -> Self {
        match err {
            RecordError::Invalid(message) => ExploreError::Invalid(message),
            RecordError::File(err) => ExploreError::Internal(format!("decisions file: {err}")),
        }
    }
}

/// A pair scored at least this, with no decision, counts as merged.
pub const MERGE_AT: f64 = 0.9;
/// A pair scored from here up to [`MERGE_AT`], with no decision, waits for a person.
pub const PROPOSE_AT: f64 = 0.5;
/// Keywords one Roles listing returns at most; the filter reaches the rest.
pub const ROLE_LIST_LIMIT: usize = 200;
/// How many keywords one vocabulary search answers with, and the shortest query it takes.
pub const KEYWORD_SEARCH_LIMIT: usize = 12;
pub const KEYWORD_SEARCH_MIN: usize = 2;
/// A keyword longer than this cannot be a stored value, so a write refuses it unlooked-up.
const KEYWORD_MAX: usize = 200;

// ---------------------------------------------------------------------------------------
// The four decisions files

fn product(spellings: &Spellings, key: &[String]) -> Vec<Key> {
    let mut out: Vec<Key> = vec![Vec::new()];
    for keyword in key {
        let options = spellings
            .get(keyword)
            .filter(|found| !found.is_empty())
            .cloned()
            .unwrap_or_else(|| vec![keyword.clone()]);
        out = out
            .into_iter()
            .flat_map(|prefix| {
                options.iter().map(move |option| {
                    let mut next = prefix.clone();
                    next.push(option.clone());
                    next
                })
            })
            .collect();
    }
    out
}

/// The spellings of `keyword`, or the keyword's own text when it has none.
fn surfaces(spellings: &Spellings, keyword: &str) -> Vec<String> {
    spellings
        .get(keyword)
        .filter(|found| !found.is_empty())
        .cloned()
        .unwrap_or_else(|| vec![keyword.to_string()])
}

/// `merge_decisions.json`: one entry per pair, keyed `(surface_a, surface_b)`.
pub fn merge_book(dir: &std::path::Path) -> Book {
    Book::new(
        &MERGE_SPEC,
        "merge decisions",
        |key| crate::merge_decisions::resolve(key).map(|(a, b)| vec![a, b]),
        |key, spellings| product(spellings, key),
        dir.join(MERGE_DECISIONS_FILE),
    )
}

/// `role_decisions.json`: one entry per (keyword, role), keyed `(surface, role)`.
pub fn role_book(dir: &std::path::Path) -> Book {
    Book::new(
        &ROLE_SPEC,
        "role decisions",
        |key| crate::role_decisions::resolve(key).map(|(keyword, role)| vec![keyword, role]),
        |key, spellings| {
            surfaces(spellings, &key[0])
                .into_iter()
                .map(|surface| vec![surface, key[1].clone()])
                .collect()
        },
        dir.join(ROLE_DECISIONS_FILE),
    )
}

/// `name_decisions.json`: one entry per keyword, keyed `(surface,)`.
pub fn name_book(dir: &std::path::Path) -> Book {
    Book::new(
        &NAME_SPEC,
        "name decisions",
        |key| {
            let keyword = normalize_keyword(&key[0]);
            (!keyword.is_empty()).then(|| vec![keyword])
        },
        |key, spellings| {
            surfaces(spellings, &key[0])
                .into_iter()
                .map(|surface| vec![surface])
                .collect()
        },
        dir.join(NAME_DECISIONS_FILE),
    )
}

// ---------------------------------------------------------------------------------------
// Checking a request's fields

/// `value` as a keyword a request may name (`DecisionsFile.keyword`).
fn keyword_field(name: &str, value: Option<&serde_json::Value>) -> Result<String, ExploreError> {
    match value.and_then(serde_json::Value::as_str) {
        Some(text) if !text.is_empty() && text.chars().count() <= KEYWORD_MAX => {
            Ok(text.to_string())
        }
        _ => Err(ExploreError::Invalid(format!(
            "{name} must be text of 1 to {KEYWORD_MAX} characters"
        ))),
    }
}

/// `value` as one of `choices` (`DecisionsFile.decision`).
fn decision_field(
    choices: &[&str],
    value: Option<&serde_json::Value>,
) -> Result<String, ExploreError> {
    match value.and_then(serde_json::Value::as_str) {
        Some(text) if choices.contains(&text) => Ok(text.to_string()),
        _ => Err(ExploreError::Invalid(format!(
            "decision must be one of {}",
            choices.join(", ")
        ))),
    }
}

type Body = serde_json::Map<String, serde_json::Value>;

/// `(keyword_a, keyword_b, decision)` of a merge request (`MergeDecisions.check`).
pub fn check_merge(body: &Body) -> Result<(String, String, String), ExploreError> {
    let first = keyword_field("keyword_a", body.get("keyword_a"))?;
    let second = keyword_field("keyword_b", body.get("keyword_b"))?;
    if first >= second {
        return Err(ExploreError::Invalid(
            "keyword_a must sort before keyword_b".into(),
        ));
    }
    let decision = decision_field(MERGE_SPEC.decisions, body.get("decision"))?;
    Ok((first, second, decision))
}

/// `(keyword, role, decision)` of a role request (`RoleDecisions.check`).
pub fn check_role(body: &Body) -> Result<(String, String, String), ExploreError> {
    let role = match body.get("role").and_then(serde_json::Value::as_str) {
        Some(role) if ROLES.contains(&role) => role.to_string(),
        _ => {
            return Err(ExploreError::Invalid(format!(
                "role must be one of {}",
                ROLES.join(", ")
            )));
        }
    };
    let keyword = keyword_field("keyword", body.get("keyword"))?;
    let decision = decision_field(ROLE_SPEC.decisions, body.get("decision"))?;
    Ok((keyword, role, decision))
}

/// `(keyword, decision, name)` of a name request (`NameDecisions.check`). A name that is
/// empty, blank or null clears the keyword's name.
pub fn check_name(body: &Body) -> Result<(String, String, String), ExploreError> {
    if !body.contains_key("name") {
        return Err(ExploreError::Invalid(
            "name is required; a null or blank name clears it".into(),
        ));
    }
    let keyword = keyword_field("keyword", body.get("keyword"))?;
    let name = match &body["name"] {
        serde_json::Value::Null => return Ok((keyword, "cleared".into(), String::new())),
        serde_json::Value::String(text) => text,
        _ => return Err(ExploreError::Invalid("name must be text or null".into())),
    };
    let name = name
        .split(plexdb_store::is_py_space)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    if name.chars().count() > NAME_MAX {
        return Err(ExploreError::Invalid(format!(
            "name must be at most {NAME_MAX} characters"
        )));
    }
    let decision = if name.is_empty() { "cleared" } else { "named" };
    Ok((keyword, decision.into(), name))
}

// ---------------------------------------------------------------------------------------
// Keyword lookups

/// Every `keyword_forms` surface of each of `keywords`, shortest first then alphabetical,
/// the order `readable_forms` picks from. A keyword with no surface is absent.
pub fn keyword_spellings(conn: &Connection, keywords: &[&str]) -> rusqlite::Result<Spellings> {
    let mut wanted: Vec<&str> = keywords.to_vec();
    wanted.sort_unstable();
    wanted.dedup();
    let marks = vec!["?"; wanted.len()].join(", ");
    let mut statement = conn.prepare(&format!(
        "SELECT surface, keyword FROM keyword_forms WHERE keyword IN ({marks})"
    ))?;
    let mut rows: Vec<(String, String)> = statement
        .query_map(rusqlite::params_from_iter(&wanted), |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect::<Result<_, _>>()?;
    rows.sort_by(|a, b| (a.0.chars().count(), &a.0).cmp(&(b.0.chars().count(), &b.0)));
    let mut spellings = Spellings::new();
    for (surface, keyword) in rows {
        spellings.entry(keyword).or_default().push(surface);
    }
    Ok(spellings)
}

/// Whether some title carries `keyword`, as a keyword or a spoiler keyword.
pub fn is_carried(conn: &Connection, keyword: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM (SELECT value FROM enrichment WHERE namespace = 'keywords' \
         AND key IN ('keyword', 'spoiler_keyword')) WHERE value = ?)",
        [keyword],
        |row| row.get(0),
    )
}

/// Whether some title carries `keyword` as a stored keyword value.
fn is_stored(conn: &Connection, keyword: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM enrichment WHERE namespace = 'keywords' \
         AND key = 'keyword' AND value = ?)",
        [keyword],
        |row| row.get(0),
    )
}

// ---------------------------------------------------------------------------------------
// Display names

/// A keyword's name in the store or in the file: the name it shows (`None` when the file
/// clears it), when it was set, and whether the file differs from the store.
pub struct NameView {
    pub name: Option<String>,
    pub decided_at: String,
    pub pending: bool,
}

/// Each keyword with a name in the store or in `overlay`, in the order Python's dict holds
/// them: the table's rows, then the file's new keywords.
pub fn names_json(
    conn: &Connection,
    overlay: &Overlay,
) -> rusqlite::Result<Vec<(String, NameView)>> {
    let mut statement = conn.prepare("SELECT keyword, name, decided_at FROM keyword_names")?;
    let mut out: Vec<(String, NameView)> = Vec::new();
    let mut at: HashMap<String, usize> = HashMap::new();
    let mut stored: HashMap<String, String> = HashMap::new();
    for row in statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })? {
        let (keyword, name, decided_at) = row?;
        stored.insert(keyword.clone(), name.clone());
        at.insert(keyword.clone(), out.len());
        out.push((
            keyword,
            NameView {
                name: Some(name),
                decided_at,
                pending: false,
            },
        ));
    }
    for (keyword, (name, decided_at)) in &overlay.0 {
        let held = stored.get(keyword);
        if name.is_none() && held.is_none() {
            continue;
        }
        let view = NameView {
            name: name.clone(),
            decided_at: decided_at.clone(),
            pending: held.is_none() || held.map(String::as_str) != name.as_deref(),
        };
        match at.get(keyword) {
            Some(&index) => out[index].1 = view,
            None => {
                at.insert(keyword.clone(), out.len());
                out.push((keyword.clone(), view));
            }
        }
    }
    Ok(out)
}

pub fn name_view_json(view: &NameView) -> Json {
    Json::object([
        ("name", Json::opt_str(view.name.clone())),
        ("decided_at", Json::str(view.decided_at.clone())),
        ("pending", Json::Bool(view.pending)),
    ])
}

pub fn names_object(names: &[(String, NameView)]) -> Json {
    Json::Object(
        names
            .iter()
            .map(|(keyword, view)| (keyword.clone(), name_view_json(view)))
            .collect(),
    )
}

/// For each keyword with a `keyword_forms` row or a name, its own name, else its readable
/// spelling: never one borrowed from its merge group. The Merges tab labels a pair's two
/// sides with these, since both can sit in one group.
pub fn own_labels(
    conn: &Connection,
    overlay: &Overlay,
) -> rusqlite::Result<HashMap<String, String>> {
    let mut labels = readable_forms(conn)?;
    labels.extend(
        named(conn, overlay)?
            .into_iter()
            .map(|(keyword, (name, _))| (keyword, name)),
    );
    Ok(labels)
}

fn label(labels: &HashMap<String, String>, keyword: &str) -> String {
    labels
        .get(keyword)
        .cloned()
        .unwrap_or_else(|| keyword.to_string())
}

// ---------------------------------------------------------------------------------------
// Merges

/// A pair as the Merges tab lists it.
pub struct PairView {
    pub keyword_a: String,
    pub keyword_b: String,
    form_a: String,
    form_b: String,
    pub score: Option<f64>,
    model: Option<String>,
    pub decision: Option<String>,
    pub decided_at: Option<String>,
    pub pending: bool,
    pub bucket: Option<&'static str>,
}

impl PairView {
    pub fn json(&self) -> Json {
        Json::object([
            ("keyword_a", Json::str(self.keyword_a.clone())),
            ("keyword_b", Json::str(self.keyword_b.clone())),
            ("form_a", Json::str(self.form_a.clone())),
            ("form_b", Json::str(self.form_b.clone())),
            ("score", self.score.map_or(Json::Null, Json::Float)),
            ("model", Json::opt_str(self.model.clone())),
            ("decision", Json::opt_str(self.decision.clone())),
            ("decided_at", Json::opt_str(self.decided_at.clone())),
            ("pending", Json::Bool(self.pending)),
            ("bucket", self.bucket.map_or(Json::Null, Json::str)),
        ])
    }
}

/// Where a pair sits: `merged`, `rejected`, `proposed`, or `None` below the proposal band or
/// with no score and no decision.
fn bucket(score: Option<f64>, decision: Option<&str>) -> Option<&'static str> {
    match decision {
        Some("accepted") => return Some("merged"),
        Some("rejected") => return Some("rejected"),
        _ => {}
    }
    let score = score?;
    if score >= MERGE_AT {
        Some("merged")
    } else if score >= PROPOSE_AT {
        Some("proposed")
    } else {
        None
    }
}

/// A `keyword_pairs` row: `(a, b, score, model, decision, decided_at)`.
type PairRow = (
    String,
    String,
    Option<f64>,
    Option<String>,
    Option<String>,
    Option<String>,
);

const REVIEW_SQL: &str = "SELECT keyword_a, keyword_b, jev_score, jev_model, decision, decided_at FROM keyword_pairs WHERE ";

fn pair_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PairRow> {
    Ok((
        row.get(0)?,
        row.get(1)?,
        row.get(2)?,
        row.get(3)?,
        row.get(4)?,
        row.get(5)?,
    ))
}

fn pair_view(row: PairRow, forms: &HashMap<String, String>, overlay: &Latest) -> PairView {
    let (keyword_a, keyword_b, score, model, mut decision, mut decided_at) = row;
    let mut pending = false;
    if let Some(entry) = overlay.get(&[keyword_a.clone(), keyword_b.clone()]) {
        let new = (entry.decision != "cleared").then(|| entry.decision.clone());
        pending = new != decision;
        decided_at = new.as_ref().map(|_| entry.decided_at.clone());
        decision = new;
    }
    PairView {
        form_a: label(forms, &keyword_a),
        form_b: label(forms, &keyword_b),
        bucket: bucket(score, decision.as_deref()),
        keyword_a,
        keyword_b,
        score,
        model,
        decision,
        decided_at,
        pending,
    }
}

/// Whether a person's decision on `pair` reaches the table: `keyword_pairs` has a row for
/// it, or both keywords are stored values, so the fold writes one (ADR-0020).
pub fn decidable(conn: &Connection, pair: &[String]) -> rusqlite::Result<bool> {
    let found: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ?)",
        params![pair[0], pair[1]],
        |row| row.get(0),
    )?;
    if found {
        return Ok(true);
    }
    for keyword in pair {
        if !is_stored(conn, keyword)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `NotFound` unless a person may decide `pair`.
pub fn require_pair(conn: &Connection, a: &str, b: &str) -> Result<(), ExploreError> {
    if decidable(conn, &[a.to_string(), b.to_string()])? {
        Ok(())
    } else {
        Err(ExploreError::NotFound(format!(
            "no keyword pair {} in the store",
            py_tuple_repr(&[a, b])
        )))
    }
}

fn one_pair(
    conn: &Connection,
    pair: &[String],
    forms: &HashMap<String, String>,
    overlay: &Latest,
) -> rusqlite::Result<PairView> {
    let found = optional(conn.query_row(
        &format!("{REVIEW_SQL}keyword_a = ? AND keyword_b = ?"),
        params![pair[0], pair[1]],
        pair_row,
    ))?;
    let row = found.unwrap_or_else(|| (pair[0].clone(), pair[1].clone(), None, None, None, None));
    Ok(pair_view(row, forms, overlay))
}

/// The one `pair`, which the caller has checked with [`require_pair`].
pub fn review_pair(
    conn: &Connection,
    overlay: &Latest,
    forms: &HashMap<String, String>,
    pair: &[String],
) -> rusqlite::Result<PairView> {
    one_pair(conn, pair, forms, overlay)
}

/// Every pair in a bucket, and every pair only the file decided.
pub fn review_rows(
    conn: &Connection,
    overlay: &Latest,
    forms: &HashMap<String, String>,
) -> rusqlite::Result<Vec<PairView>> {
    let mut statement = conn.prepare(&format!(
        "{REVIEW_SQL}decision IS NOT NULL OR jev_score >= ?"
    ))?;
    let rows: Vec<PairRow> = statement
        .query_map([PROPOSE_AT], pair_row)?
        .collect::<Result<_, _>>()?;
    let mut out: Vec<PairView> = rows
        .into_iter()
        .map(|row| pair_view(row, forms, overlay))
        .collect();
    // A decision the table has not yet folded in can sit below the band, or name a pair with
    // no row yet. A `cleared` entry on such a pair changes nothing the page lists, and one
    // naming a keyword no title carries never reaches the table, so neither is listed.
    let have: HashSet<(String, String)> = out
        .iter()
        .map(|p| (p.keyword_a.clone(), p.keyword_b.clone()))
        .collect();
    for (key, entry) in &overlay.entries {
        if entry.decision == "cleared" || have.contains(&(key[0].clone(), key[1].clone())) {
            continue;
        }
        if decidable(conn, key)? {
            out.push(one_pair(conn, key, forms, overlay)?);
        }
    }
    Ok(out)
}

/// The review page's two lists: the queue, best score first, and the decided or merged
/// pairs, a person's latest decision first and Jev-confirmed pairs after, best score first.
pub fn merges_json(pairs: &[PairView]) -> Vec<(&'static str, Json)> {
    // A pair only a person decided has no score; it sorts as the lowest.
    let score = |p: &PairView| p.score.unwrap_or(-1.0);
    let by_score = |a: &&PairView, b: &&PairView| {
        score(b)
            .total_cmp(&score(a))
            .then_with(|| a.keyword_a.cmp(&b.keyword_a))
            .then_with(|| a.keyword_b.cmp(&b.keyword_b))
    };
    let mut proposed: Vec<&PairView> = pairs
        .iter()
        .filter(|p| p.bucket == Some("proposed"))
        .collect();
    proposed.sort_by(by_score);
    let at = |p: &PairView| p.decided_at.clone().unwrap_or_else(|| "None".into());
    let mut by_person: Vec<&PairView> = pairs.iter().filter(|p| p.decision.is_some()).collect();
    by_person.sort_by(|a, b| {
        (at(b), score(b), &b.keyword_a, &b.keyword_b)
            .partial_cmp(&(at(a), score(a), &a.keyword_a, &a.keyword_b))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut by_jev: Vec<&PairView> = pairs
        .iter()
        .filter(|p| p.decision.is_none() && p.bucket == Some("merged"))
        .collect();
    by_jev.sort_by(by_score);
    vec![
        (
            "proposed",
            Json::Array(proposed.iter().map(|p| p.json()).collect()),
        ),
        (
            "decided",
            Json::Array(
                by_person
                    .iter()
                    .chain(by_jev.iter())
                    .map(|p| p.json())
                    .collect(),
            ),
        ),
    ]
}

// ---------------------------------------------------------------------------------------
// Roles

/// `NotFound` unless `keyword_roles` has a verdict (not a refusal) for `keyword` in `role`.
pub fn require_role(conn: &Connection, keyword: &str, role: &str) -> Result<(), ExploreError> {
    if has_verdict(conn, keyword, role)? {
        Ok(())
    } else {
        Err(ExploreError::NotFound(format!(
            "no keyword role {} in the store",
            py_tuple_repr(&[keyword, role])
        )))
    }
}

fn has_verdict(conn: &Connection, keyword: &str, role: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM keyword_roles WHERE keyword = ? AND role = ? \
         AND error IS NULL)",
        params![keyword, role],
        |row| row.get(0),
    )
}

/// A source's verdict: `(source, score, model)`.
type Verdict = (String, Option<f64>, Option<String>);

/// The decision, its time, and whether it is still waiting for the fold.
fn role_decision(
    stored: Option<&(String, String)>,
    entry: Option<&Entry>,
) -> (Option<String>, Option<String>, bool) {
    let (decision, decided_at) = match stored {
        Some((decision, at)) => (Some(decision.clone()), Some(at.clone())),
        None => (None, None),
    };
    let Some(entry) = entry else {
        return (decision, decided_at, false);
    };
    let new = (entry.decision != "cleared").then(|| entry.decision.clone());
    let at = new.as_ref().map(|_| entry.decided_at.clone());
    let pending = new != decision;
    (new, at, pending)
}

/// A cell's verdicts: source-stated rows first, then judges, each by source name.
fn sources_json(rows: &[Verdict]) -> Json {
    let mut ordered: Vec<&Verdict> = rows.iter().collect();
    ordered.sort_by(|a, b| (a.1.is_some(), &a.0).cmp(&(b.1.is_some(), &b.0)));
    Json::Array(
        ordered
            .into_iter()
            .map(|(source, score, model)| {
                Json::object([
                    ("source", Json::str(source.clone())),
                    ("score", score.map_or(Json::Null, Json::Float)),
                    ("model", Json::opt_str(model.clone())),
                ])
            })
            .collect(),
    )
}

/// One (keyword, role): every source's verdict and the decision, the file's over the table's.
pub fn role_cell(
    conn: &Connection,
    overlay: &Latest,
    forms: &HashMap<String, String>,
    keyword: &str,
    role: &str,
) -> rusqlite::Result<Json> {
    let rows: Vec<Verdict> = conn
        .prepare(
            "SELECT source, score, model FROM keyword_roles \
             WHERE keyword = ? AND role = ? AND error IS NULL",
        )?
        .query_map(params![keyword, role], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?
        .collect::<Result<_, _>>()?;
    let stored = optional(conn.query_row(
        "SELECT decision, decided_at FROM keyword_role_decisions WHERE keyword = ? AND role = ?",
        params![keyword, role],
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
    ))?;
    let entry = overlay.get(&[keyword.to_string(), role.to_string()]);
    let (decision, decided_at, pending) = role_decision(stored.as_ref(), entry);
    Ok(Json::object([
        ("keyword", Json::str(keyword)),
        ("form", Json::Str(label(forms, keyword))),
        ("role", Json::str(role)),
        ("sources", sources_json(&rows)),
        ("decision", Json::opt_str(decision)),
        ("decided_at", Json::opt_str(decided_at)),
        ("pending", Json::Bool(pending)),
    ]))
}

/// The Roles tab's two lists: up to [`ROLE_LIST_LIMIT`] keywords whose readable or stored
/// form contains `needle`, each with one cell per role it has a verdict in, and every
/// (keyword, role) a person has ruled on, latest first.
pub fn roles_json(
    conn: &Connection,
    overlay: &Latest,
    forms: &HashMap<String, String>,
    needle: &str,
) -> rusqlite::Result<Json> {
    let every: Vec<String> = conn
        .prepare("SELECT DISTINCT keyword FROM keyword_roles WHERE error IS NULL")?
        .query_map([], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let needle = py_strip(needle).to_lowercase();
    let shown = |keyword: &str| label(forms, keyword).to_lowercase();
    let mut matched: Vec<&String> = every
        .iter()
        .filter(|k| {
            needle.is_empty() || shown(k).contains(&needle) || k.to_lowercase().contains(&needle)
        })
        .collect();
    matched.sort_by_cached_key(|k| (shown(k), (*k).clone()));
    let page: Vec<&String> = matched.iter().take(ROLE_LIST_LIMIT).copied().collect();

    let mut ruled: HashMap<(String, String), (String, String)> = HashMap::new();
    {
        let mut statement =
            conn.prepare("SELECT keyword, role, decision, decided_at FROM keyword_role_decisions")?;
        for row in statement.query_map([], |row| {
            Ok((
                (row.get::<_, String>(0)?, row.get::<_, String>(1)?),
                (row.get::<_, String>(2)?, row.get::<_, String>(3)?),
            ))
        })? {
            let (key, value) = row?;
            ruled.insert(key, value);
        }
    }

    let mut verdicts: HashMap<&str, HashMap<String, Vec<Verdict>>> =
        page.iter().map(|k| (k.as_str(), HashMap::new())).collect();
    if !page.is_empty() {
        let marks = vec!["?"; page.len()].join(",");
        let mut statement = conn.prepare(&format!(
            "SELECT keyword, role, source, score, model FROM keyword_roles \
             WHERE error IS NULL AND keyword IN ({marks})"
        ))?;
        let rows = statement.query_map(rusqlite::params_from_iter(&page), |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                (row.get(2)?, row.get(3)?, row.get(4)?),
            ))
        })?;
        for row in rows {
            let (keyword, role, verdict) = row?;
            if let Some(cells) = verdicts.get_mut(keyword.as_str()) {
                cells.entry(role).or_default().push(verdict);
            }
        }
    }

    let mut keywords = Vec::new();
    for keyword in &page {
        let mut cells: Vec<(String, Json)> = Vec::new();
        for role in ROLES {
            let Some(rows) = verdicts[keyword.as_str()]
                .get(role)
                .filter(|r| !r.is_empty())
            else {
                continue;
            };
            let key = ((*keyword).clone(), role.to_string());
            let (decision, decided_at, pending) = role_decision(
                ruled.get(&key),
                overlay.get(&[key.0.clone(), key.1.clone()]),
            );
            cells.push((
                role.to_string(),
                Json::object([
                    ("sources", sources_json(rows)),
                    ("decision", Json::opt_str(decision)),
                    ("decided_at", Json::opt_str(decided_at)),
                    ("pending", Json::Bool(pending)),
                ]),
            ));
        }
        keywords.push(Json::object([
            ("keyword", Json::Str((*keyword).clone())),
            ("form", Json::Str(label(forms, keyword))),
            ("cells", Json::Object(cells)),
        ]));
    }

    // A decision whose verdict is gone (a restored older store) is one the fold ignores and a
    // POST cannot clear, so it is not listed.
    let mut keys: BTreeSet<(String, String)> = ruled.keys().cloned().collect();
    keys.extend(
        overlay
            .entries
            .iter()
            .map(|(k, _)| (k[0].clone(), k[1].clone())),
    );
    let mut decided: Vec<(String, String, String, String, bool)> = Vec::new();
    for key in keys {
        let (decision, decided_at, pending) = role_decision(
            ruled.get(&key),
            overlay.get(&[key.0.clone(), key.1.clone()]),
        );
        let Some(decision) = decision else { continue };
        if !has_verdict(conn, &key.0, &key.1)? {
            continue;
        }
        decided.push((
            decided_at.unwrap_or_else(|| "None".into()),
            key.0,
            key.1,
            decision,
            pending,
        ));
    }
    decided.sort();
    decided.reverse();
    let decided: Vec<Json> = decided
        .into_iter()
        .map(|(at, keyword, role, decision, pending)| {
            Json::object([
                ("keyword", Json::Str(keyword.clone())),
                ("form", Json::Str(label(forms, &keyword))),
                ("role", Json::Str(role)),
                ("decision", Json::Str(decision)),
                ("decided_at", Json::Str(at)),
                ("pending", Json::Bool(pending)),
            ])
        })
        .collect();

    Ok(Json::object([
        (
            "roles",
            Json::Array(ROLES.iter().map(|r| Json::str(*r)).collect()),
        ),
        ("stored", Json::Int(every.len() as i64)),
        ("total", Json::Int(matched.len() as i64)),
        ("limit", Json::Int(ROLE_LIST_LIMIT as i64)),
        ("keywords", Json::Array(keywords)),
        ("decided", Json::Array(decided)),
    ]))
}

// ---------------------------------------------------------------------------------------
// The tag doctor

/// The `LIKE` pattern matching text that contains `needle`.
fn like(needle: &str) -> String {
    format!(
        "%{}%",
        needle
            .replace('\\', "\\\\")
            .replace('%', "\\%")
            .replace('_', "\\_")
    )
}

/// Stored keywords whose value or any raw spelling contains `q`, for the tag doctor's
/// "Merge with…": an exact match first, then those starting with it, then by how many titles
/// carry each, each with the keyword its merge group folds into.
pub fn keyword_search(
    conn: &Connection,
    q: &str,
    labels: &HashMap<String, String>,
) -> Result<Json, ExploreError> {
    let q = py_strip(q).to_lowercase();
    if q.chars().count() < KEYWORD_SEARCH_MIN {
        return Err(ExploreError::Invalid(format!(
            "q must be at least {KEYWORD_SEARCH_MIN} characters"
        )));
    }
    let pattern = like(&q);
    let mut rows: Vec<(String, i64)> = conn
        .prepare(
            "SELECT value, COUNT(DISTINCT item_id) FROM enrichment \
             WHERE namespace = 'keywords' AND key = 'keyword' AND value IN (\
             SELECT keyword FROM keyword_forms WHERE surface LIKE ?1 ESCAPE '\\' \
             OR keyword LIKE ?1 ESCAPE '\\') GROUP BY value",
        )?
        .query_map([&pattern], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    rows.sort_by(|a, b| {
        (a.0 != q, !a.0.starts_with(&q), -a.1, &a.0).cmp(&(
            b.0 != q,
            !b.0.starts_with(&q),
            -b.1,
            &b.0,
        ))
    });
    let folded = merge_map(conn)?;
    Ok(Json::Array(
        rows.into_iter()
            .take(KEYWORD_SEARCH_LIMIT)
            .map(|(value, titles)| {
                let into = folded.get(&value);
                Json::object([
                    ("value", Json::Str(value.clone())),
                    ("label", Json::Str(label(labels, &value))),
                    ("titles", Json::Int(titles)),
                    ("into", Json::opt_str(into.cloned())),
                    (
                        "into_label",
                        Json::opt_str(into.map(|into| label(labels, into))),
                    ),
                ])
            })
            .collect(),
    ))
}

/// Titles some source sent a keyword for, whose title contains `q`, for the tag doctor's
/// title search: those starting with it first, then by title and year.
pub fn doctor_titles(conn: &Connection, q: &str) -> Result<Json, ExploreError> {
    let q = py_strip(q);
    if q.chars().count() < KEYWORD_SEARCH_MIN {
        return Err(ExploreError::Invalid(format!(
            "q must be at least {KEYWORD_SEARCH_MIN} characters"
        )));
    }
    type Found = (String, String, Option<i64>, Option<String>);
    let mut rows: Vec<Found> = conn
        .prepare(
            "SELECT item_id, title, year, type FROM items WHERE title LIKE ? ESCAPE '\\' \
             AND item_id IN (SELECT item_id FROM keyword_surfaces)",
        )?
        .query_map([like(q)], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })?
        .collect::<Result<_, _>>()?;
    let low = q.to_lowercase();
    rows.sort_by_cached_key(|r| {
        (
            !r.1.to_lowercase().starts_with(&low),
            r.1.to_lowercase(),
            r.2.unwrap_or(0),
            r.0.clone(),
        )
    });
    Ok(Json::Array(
        rows.into_iter()
            .take(KEYWORD_SEARCH_LIMIT)
            .map(|(item_id, title, year, kind)| {
                Json::object([
                    ("item_id", Json::Str(item_id)),
                    ("title", Json::Str(title)),
                    ("year", year.map_or(Json::Null, Json::Int)),
                    ("kind", Json::opt_str(kind)),
                ])
            })
            .collect(),
    ))
}

/// One keyword surface a source sent for a title.
type Spelled = (String, String, Option<i64>, i64, Option<String>);

/// What the tag doctor shows for one title: per source, each raw spelling the source sent, the
/// stored value it normalizes to, the keyword its merge group folds into with the pair that
/// put it there, and any merge decision in `overlay` naming it that the store has not folded
/// in yet. Keywords show as `labels`, and `names` carries each shown keyword's own display
/// name from `named`, pending or not.
pub fn title_doctor(
    conn: &Connection,
    item_id: &str,
    overlay: &Latest,
    labels: &HashMap<String, String>,
    named: &[(String, NameView)],
) -> Result<Json, ExploreError> {
    let row: Option<(String, Option<i64>)> = optional(conn.query_row(
        "SELECT title, year FROM items WHERE item_id = ?",
        [item_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ))?;
    let Some((title, year)) = row else {
        return Err(ExploreError::NotFound(format!(
            "no title with item_id {}",
            py_repr(item_id)
        )));
    };
    let folded = merge_map(conn)?;
    let mut groups: HashMap<&str, Vec<&str>> = HashMap::new();
    for (member, head) in &folded {
        groups.entry(head).or_default().push(member);
    }
    let spellings: Vec<Spelled> = conn
        .prepare(
            "SELECT s.source, s.surface, s.rank, s.spoiler, f.keyword FROM keyword_surfaces s \
             LEFT JOIN keyword_forms f USING (surface) WHERE s.item_id = ? \
             ORDER BY s.source, s.rank IS NULL, s.rank, s.surface",
        )?
        .query_map([item_id], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })?
        .collect::<Result<_, _>>()?;
    let carried: BTreeSet<&str> = spellings.iter().filter_map(|s| s.4.as_deref()).collect();

    // Decisions in the file the fold has not written yet, on this title's keywords only. One
    // naming a keyword no title carries never reaches the table, so it is not shown.
    let mut pending: HashMap<String, Vec<Json>> = HashMap::new();
    for (key, entry) in &overlay.entries {
        if !(carried.contains(key[0].as_str()) || carried.contains(key[1].as_str()))
            || !decidable(conn, key)?
        {
            continue;
        }
        if one_pair(conn, key, labels, overlay)?.pending {
            for (keyword, other) in [(&key[0], &key[1]), (&key[1], &key[0])] {
                pending
                    .entry(keyword.clone())
                    .or_default()
                    .push(Json::object([
                        ("other", Json::Str(other.clone())),
                        ("label", Json::Str(label(labels, other))),
                        ("decision", Json::Str(entry.decision.clone())),
                    ]));
            }
        }
    }

    let via = |keyword: &str, into: &str| -> rusqlite::Result<Json> {
        // The pair between the two when it is itself merged; null when they are in one group
        // only through other members.
        let (low, high) = if keyword < into {
            (keyword, into)
        } else {
            (into, keyword)
        };
        let pair = optional(conn.query_row(
            "SELECT jev_score, jev_model, decision FROM keyword_pairs \
             WHERE keyword_a = ? AND keyword_b = ? \
             AND (decision = 'accepted' OR (decision IS NULL AND jev_score >= ?))",
            params![low, high, MERGE_AT],
            |row| {
                Ok((
                    row.get::<_, Option<f64>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        ))?;
        Ok(pair.map_or(Json::Null, |(score, model, decision)| {
            Json::object([
                ("score", score.map_or(Json::Null, Json::Float)),
                ("model", Json::opt_str(model)),
                ("decision", Json::opt_str(decision)),
            ])
        }))
    };

    let mut sources: Vec<(String, Vec<Json>)> = Vec::new();
    for (source, surface, rank, spoiler, keyword) in &spellings {
        let into = keyword.as_ref().and_then(|k| folded.get(k));
        let head = into.or(keyword.as_ref());
        let group: Vec<Json> = match head {
            None => Vec::new(),
            Some(head) => {
                let mut members: BTreeSet<&str> = BTreeSet::new();
                members.insert(head);
                members.extend(groups.get(head.as_str()).into_iter().flatten().copied());
                if let Some(keyword) = keyword {
                    members.remove(keyword.as_str());
                }
                members.into_iter().map(Json::str).collect()
            }
        };
        let row = Json::object([
            ("surface", Json::str(surface.clone())),
            ("rank", rank.map_or(Json::Null, Json::Int)),
            ("spoiler", Json::Bool(*spoiler != 0)),
            ("keyword", Json::opt_str(keyword.clone())),
            ("into", Json::opt_str(into.cloned())),
            (
                "into_label",
                Json::opt_str(into.map(|into| label(labels, into))),
            ),
            (
                "via",
                match (keyword, into) {
                    (Some(keyword), Some(into)) => via(keyword, into)?,
                    _ => Json::Null,
                },
            ),
            ("group", Json::Array(group)),
            (
                "pending",
                Json::Array(
                    keyword
                        .as_ref()
                        .and_then(|k| pending.get(k))
                        .cloned()
                        .unwrap_or_default(),
                ),
            ),
        ]);
        match sources.iter_mut().find(|(name, _)| name == source) {
            Some((_, rows)) => rows.push(row),
            None => sources.push((source.clone(), vec![row])),
        }
    }

    let mut shown: BTreeSet<&str> = carried.clone();
    shown.extend(
        carried
            .iter()
            .filter_map(|k| folded.get(*k).map(String::as_str)),
    );
    let own: HashMap<&str, &NameView> = named.iter().map(|(k, v)| (k.as_str(), v)).collect();
    Ok(Json::object([
        ("item_id", Json::str(item_id)),
        ("title", Json::Str(title)),
        ("year", year.map_or(Json::Null, Json::Int)),
        (
            "forms",
            Json::Object(
                shown
                    .iter()
                    .map(|k| ((*k).to_string(), Json::Str(label(labels, k))))
                    .collect(),
            ),
        ),
        (
            "names",
            Json::Object(
                shown
                    .iter()
                    .filter_map(|k| own.get(k).map(|v| ((*k).to_string(), name_view_json(v))))
                    .collect(),
            ),
        ),
        (
            "sources",
            Json::Array(
                sources
                    .into_iter()
                    .map(|(source, rows)| {
                        Json::object([("source", Json::Str(source)), ("rows", Json::Array(rows))])
                    })
                    .collect(),
            ),
        ),
    ]))
}
