//! Walking a Plex library into `items`, `external_ids` and the rating-key map
//! (ADR-0005, ADR-0008).
//!
//! The store walks Plex itself, because watch history carries only a Plex
//! `ratingKey` and the Plex-internal `plex://` GUID — the weakest tier in
//! [`crate::identity::PRIORITY`].
//!
//! **Idempotent, on purpose.** `items` and `external_ids` are upserted, and once a
//! title is recorded against an `item_id` that mapping is never repointed at a
//! differently derived id on a later walk (ADR-0008): an existing identity is found
//! by any of the title's external ids first, in priority order, then by rating key.
//! An external id matches only within its own media kind (a bare `tmdb://1678` is
//! one film and one show), and an identity is not adopted from an external id if
//! another rating key already holds it under a different title — that record forks
//! to an id derived from only the ids nobody else has claimed.

use std::collections::{BTreeSet, HashMap};

use plexdb_store::{StoreError, py_strip};
use rusqlite::{Connection, params_from_iter};
use serde_json::Value;

use crate::identity::{PRIORITY, canonical_path, derive_item_id};
use crate::plex::{PlexSource, Section, section_passes};
use crate::pyval::{get, or, py_str, sql};

pub const NAME: &str = "walk";

/// How a stored identity was found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FoundBy {
    ExternalId,
    RatingKey,
}

/// One title whose stored `item_id` differed from the id this walk derived, and
/// which kept the stored one.
#[derive(Debug, Clone)]
pub struct KeptIdentity {
    pub found_by: FoundBy,
    pub label: String,
    pub rating_key: String,
    pub kept_id: String,
    pub derived_id: String,
}

/// One title that resolved onto an identity another rating key already held under a
/// different title, and forked to its own id instead.
#[derive(Debug, Clone)]
pub struct ForkedIdentity {
    pub label: String,
    pub rating_key: String,
    pub contested_id: String,
    pub contested_by: String,
    pub contested_title: String,
    pub forked_id: String,
}

/// What one walk touched.
#[derive(Debug, Default)]
pub struct WalkStats {
    pub sections_walked: usize,
    pub titles_seen: usize,
    pub titles_written: usize,
    /// Titles with no recognised external GUID, landed under the `fs:` fallback.
    pub fallback_to_path: usize,
    pub identities_kept: Vec<KeptIdentity>,
    pub identities_forked: Vec<ForkedIdentity>,
}

impl WalkStats {
    pub fn kept_by(&self, found_by: FoundBy) -> Vec<&KeptIdentity> {
        self.identities_kept
            .iter()
            .filter(|kept| kept.found_by == found_by)
            .collect()
    }
}

/// Which rating keys hold one `item_id`, and the title it currently carries.
struct Claim {
    title: String,
    rating_keys: BTreeSet<String>,
}

type ExternalKey = (String, String, String);

/// Plex's `Guid` array -> `(namespace, value)` pairs, in Plex's own order.
pub fn guid_pairs(record: &Value) -> Vec<(String, String)> {
    let mut pairs = Vec::new();
    for guid in get(record, "Guid").as_array().into_iter().flatten() {
        let Some(raw) = get(guid, "id").as_str().filter(|raw| !raw.is_empty()) else {
            continue;
        };
        if let Some((namespace, value)) = raw.split_once("://") {
            pairs.push((namespace.to_string(), value.to_string()));
        }
    }
    pairs
}

/// The playback path of the first `Media`/`Part` a record carries, if any.
pub fn raw_path(record: &Value) -> Option<String> {
    for media in get(record, "Media").as_array().into_iter().flatten() {
        for part in get(media, "Part").as_array().into_iter().flatten() {
            let file = get(part, "file");
            if crate::pyval::truthy(file) {
                return Some(py_str(file));
            }
        }
    }
    None
}

/// The string `derive_item_id`'s path-hash fallback hashes for this record: a movie
/// or episode's canonicalised playback path; a show has no file, so its Plex `key`
/// stands in.
pub fn canonical_for(record: &Value, source_roots: &[String]) -> String {
    if let Some(path) = raw_path(record) {
        return canonical_path(&path, source_roots);
    }
    let key = match get(record, "key") {
        key if crate::pyval::truthy(key) => py_str(key),
        _ => format!("/library/metadata/{}", {
            let rating_key = get(record, "ratingKey");
            if rating_key.is_null() {
                String::new()
            } else {
                py_str(rating_key)
            }
        }),
    };
    canonical_path(&key, source_roots)
}

fn label(record: &Value, kind: &str) -> String {
    let title = py_str(or(
        get(record, "title"),
        &Value::String("(untitled)".into()),
    ));
    if kind != "episode" {
        let year = get(record, "year");
        return if crate::pyval::truthy(year) {
            format!("{title} ({})", py_str(year))
        } else {
            title
        };
    }
    let show = py_str(or(
        get(record, "grandparentTitle"),
        &Value::String("(unknown show)".into()),
    ));
    let (season, episode) = (get(record, "parentIndex"), get(record, "index"));
    if season.is_null() || episode.is_null() {
        return format!("{show} — {title}");
    }
    let number = |value: &Value| crate::pyval::py_int(value).unwrap_or(0);
    format!(
        "{show} S{:02}E{:02} — {title}",
        number(season),
        number(episode)
    )
}

struct Resolved {
    item_id: String,
    found_by: FoundBy,
}

/// The already-recorded identity for this title, if any — external id first (in
/// priority order, within its own kind), then rating key.
fn resolve_existing(
    external_ids: &[(String, String)],
    rating_key: &str,
    kind: &str,
    by_external_id: &HashMap<ExternalKey, String>,
    by_rating_key: &HashMap<String, String>,
) -> Option<Resolved> {
    for namespace in PRIORITY {
        for (ns, value) in external_ids {
            if ns != namespace || py_strip(value).is_empty() {
                continue;
            }
            let key = (ns.clone(), value.clone(), kind.to_string());
            if let Some(matched) = by_external_id.get(&key) {
                return Some(Resolved {
                    item_id: matched.clone(),
                    found_by: FoundBy::ExternalId,
                });
            }
        }
    }
    by_rating_key.get(rating_key).map(|prior| Resolved {
        item_id: prior.clone(),
        found_by: FoundBy::RatingKey,
    })
}

/// The `(rating key, title)` already holding `item_id` under a different title, if
/// any (issue #58's discriminator). Titles compare case-folded and trimmed only; the
/// same rating key holding the identity is never contested.
fn contesting_claim(
    item_id: &str,
    rating_key: &str,
    title: &str,
    claims: &HashMap<String, Claim>,
) -> Option<(String, String)> {
    let claim = claims.get(item_id)?;
    let other = claim
        .rating_keys
        .iter()
        .find(|key| key.as_str() != rating_key)?;
    if py_strip(&claim.title).to_lowercase() == py_strip(title).to_lowercase() {
        return None;
    }
    Some((other.clone(), claim.title.clone()))
}

struct State {
    by_rating_key: HashMap<String, String>,
    by_external_id: HashMap<ExternalKey, String>,
    claims: HashMap<String, Claim>,
    show_item_ids: HashMap<String, String>,
}

impl State {
    fn load(conn: &Connection) -> Result<Self, StoreError> {
        let mut by_rating_key = HashMap::new();
        let mut stmt = conn.prepare("SELECT rating_key, item_id FROM plex_items")?;
        for row in stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (rating_key, item_id) = row?;
            by_rating_key.insert(rating_key, item_id);
        }
        let mut by_external_id = HashMap::new();
        let mut stmt = conn.prepare("SELECT ns, value, kind, item_id FROM external_ids")?;
        for row in stmt.query_map([], |row| {
            Ok((
                (
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ),
                row.get::<_, String>(3)?,
            ))
        })? {
            let (key, item_id) = row?;
            by_external_id.insert(key, item_id);
        }
        let mut claims: HashMap<String, Claim> = HashMap::new();
        let mut stmt = conn.prepare(
            "SELECT p.item_id, p.rating_key, i.title FROM plex_items p \
             JOIN items i ON i.item_id = p.item_id",
        )?;
        for row in stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })? {
            let (item_id, rating_key, title) = row?;
            claims
                .entry(item_id)
                .or_insert_with(|| Claim {
                    title: title.unwrap_or_default(),
                    rating_keys: BTreeSet::new(),
                })
                .rating_keys
                .insert(rating_key);
        }
        let mut show_item_ids = HashMap::new();
        let mut stmt = conn.prepare(
            "SELECT p.rating_key, p.item_id FROM plex_items p \
             JOIN items i ON i.item_id = p.item_id WHERE i.type = 'show'",
        )?;
        for row in stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (rating_key, item_id) = row?;
            show_item_ids.insert(rating_key, item_id);
        }
        Ok(State {
            by_rating_key,
            by_external_id,
            claims,
            show_item_ids,
        })
    }
}

/// What one section's pass shares across its records.
struct Pass<'a> {
    section: &'a Section,
    source_roots: &'a [String],
    now: &'a str,
}

/// Upsert one Plex record as an `items` row, its `external_ids`, and its
/// `plex_items` mapping. A record Plex returned with no `ratingKey` is not
/// addressable and is skipped rather than written under a made-up key.
fn write_item(
    conn: &Connection,
    record: &Value,
    kind: &str,
    pass: &Pass,
    stats: &mut WalkStats,
    state: &mut State,
) -> Result<(), StoreError> {
    let raw_rating_key = get(record, "ratingKey");
    if !crate::pyval::truthy(raw_rating_key) {
        return Ok(());
    }
    let rating_key = py_str(raw_rating_key);
    stats.titles_seen += 1;

    let external_ids = guid_pairs(record);
    let (section, source_roots, now) = (pass.section, pass.source_roots, pass.now);
    let canonical = canonical_for(record, source_roots);
    let derived_id = derive_item_id(&external_ids, &canonical);
    if derived_id.starts_with("fs:") {
        stats.fallback_to_path += 1;
    }

    let title = py_str(or(get(record, "title"), &Value::String(String::new())));
    let resolved = resolve_existing(
        &external_ids,
        &rating_key,
        kind,
        &state.by_external_id,
        &state.by_rating_key,
    );
    // A record whose own GUID set derives exactly the identity it matched is that
    // identity's rightful holder and never forks, whatever else claims it.
    let contested = match &resolved {
        Some(found) if found.found_by == FoundBy::ExternalId && found.item_id != derived_id => {
            contesting_claim(&found.item_id, &rating_key, &title, &state.claims)
        }
        _ => None,
    };

    let item_id = match (&resolved, contested) {
        (Some(found), Some((contested_by, contested_title))) => {
            // Plex gave two different titles one external id. This record takes an
            // id derived from only the external ids no other identity has claimed.
            let unclaimed: Vec<(String, String)> = external_ids
                .iter()
                .filter(|(ns, value)| {
                    !state.by_external_id.contains_key(&(
                        ns.clone(),
                        value.clone(),
                        kind.to_string(),
                    ))
                })
                .cloned()
                .collect();
            let item_id = derive_item_id(&unclaimed, &canonical);
            stats.identities_forked.push(ForkedIdentity {
                label: label(record, kind),
                rating_key: rating_key.clone(),
                contested_id: found.item_id.clone(),
                contested_by,
                contested_title,
                forked_id: item_id.clone(),
            });
            item_id
        }
        (Some(found), None) if found.item_id != derived_id => {
            // The rating key's GUID set changed, or the key changed while an id
            // stayed: adopting the derived id would orphan the old rows as a second
            // row for the same title. Keep the identity, and count it.
            stats.identities_kept.push(KeptIdentity {
                found_by: found.found_by,
                label: label(record, kind),
                rating_key: rating_key.clone(),
                kept_id: found.item_id.clone(),
                derived_id: derived_id.clone(),
            });
            found.item_id.clone()
        }
        _ => derived_id,
    };

    let episode = kind == "episode";
    let show_item_id = if episode {
        let grandparent = get(record, "grandparentRatingKey");
        if grandparent.is_null() {
            None
        } else {
            state.show_item_ids.get(&py_str(grandparent)).cloned()
        }
    } else {
        None
    };
    let only_episode = |key: &str| {
        if episode {
            sql(get(record, key))
        } else {
            rusqlite::types::Value::Null
        }
    };
    conn.execute(
        "INSERT INTO items \
            (item_id, type, title, title_sort, show_title, show_item_id, \
             season, episode, year, duration_ms, content_rating, studio) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT(item_id) DO UPDATE SET \
            type = excluded.type, title = excluded.title, title_sort = excluded.title_sort, \
            show_title = excluded.show_title, show_item_id = excluded.show_item_id, \
            season = excluded.season, episode = excluded.episode, year = excluded.year, \
            duration_ms = excluded.duration_ms, content_rating = excluded.content_rating, \
            studio = excluded.studio",
        params_from_iter([
            rusqlite::types::Value::Text(item_id.clone()),
            rusqlite::types::Value::Text(kind.to_string()),
            rusqlite::types::Value::Text(title.clone()),
            sql(get(record, "titleSort")),
            only_episode("grandparentTitle"),
            show_item_id.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
            only_episode("parentIndex"),
            only_episode("index"),
            sql(get(record, "year")),
            sql(get(record, "duration")),
            sql(get(record, "contentRating")),
            sql(get(record, "studio")),
        ]),
    )?;

    for (namespace, value) in &external_ids {
        // (ns, value, kind) is the key. On conflict the first claimant keeps the id;
        // `last_seen` always moves, because this row was in Plex's `Guid` array on
        // this walk whichever item_id ends up owning it.
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) \
             VALUES (?, ?, ?, ?, ?) \
             ON CONFLICT (ns, value, kind) DO UPDATE SET last_seen = excluded.last_seen",
            (&item_id, namespace, value, kind, now),
        )?;
    }
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
         VALUES (?, ?, ?, ?) \
         ON CONFLICT(rating_key) DO UPDATE SET \
            item_id = excluded.item_id, section_id = excluded.section_id, \
            last_seen = excluded.last_seen",
        (&rating_key, &item_id, &section.key, now),
    )?;

    state
        .by_rating_key
        .insert(rating_key.clone(), item_id.clone());
    // Mirror the two rows just written, so a later record in this same pass whose
    // only shared id points here is compared against exactly this.
    state
        .claims
        .entry(item_id.clone())
        .and_modify(|claim| {
            claim.title = title.clone();
            claim.rating_keys.insert(rating_key.clone());
        })
        .or_insert_with(|| Claim {
            title: title.clone(),
            rating_keys: BTreeSet::from([rating_key.clone()]),
        });
    for (namespace, value) in &external_ids {
        state
            .by_external_id
            .entry((namespace.clone(), value.clone(), kind.to_string()))
            .or_insert_with(|| item_id.clone());
    }
    if kind == "show" {
        state.show_item_ids.insert(rating_key, item_id);
    }
    stats.titles_written += 1;
    Ok(())
}

/// Walk every section `source` reports (or just `section_key`) into `conn`, in one
/// transaction: a failure partway leaves the store as it was.
pub fn walk_all(
    conn: &mut Connection,
    source: &dyn PlexSource,
    source_roots: &[String],
    section_key: Option<&str>,
) -> Result<WalkStats, StoreError> {
    walk_at(
        conn,
        source,
        source_roots,
        section_key,
        &crate::staleness::Now::read().iso(),
    )
}

/// [`walk_all`] with the instant it stamps `last_seen` with.
pub fn walk_at(
    conn: &mut Connection,
    source: &dyn PlexSource,
    source_roots: &[String],
    section_key: Option<&str>,
    now: &str,
) -> Result<WalkStats, StoreError> {
    let mut stats = WalkStats::default();
    let now = now.to_string();
    let mut state = State::load(conn)?;
    let sections = source.sections()?;
    let tx = conn.transaction()?;
    for section in &sections {
        if section_key.is_some_and(|key| section.key != key) {
            continue;
        }
        let passes = section_passes(&section.kind);
        if passes.is_empty() {
            continue; // a section type this walk does not understand (e.g. "photo")
        }
        stats.sections_walked += 1;
        let pass = Pass {
            section,
            source_roots,
            now: &now,
        };
        for (kind, plex_type) in passes {
            for record in source.items(&section.key, *plex_type)? {
                write_item(&tx, &record, kind, &pass, &mut stats, &mut state)?;
            }
        }
    }
    tx.commit()?;
    Ok(stats)
}

/// How many kept identities to name per resolution path before summarising the rest.
const KEPT_SHOWN: usize = 20;

/// The summary `plexdb walk` prints, line by line.
pub fn describe(stats: &WalkStats) -> Vec<String> {
    let mut lines = vec![format!(
        "walked {} section(s): {} title(s) seen, {} written, {} fell back to a path-derived id",
        stats.sections_walked, stats.titles_seen, stats.titles_written, stats.fallback_to_path
    )];
    for (path, by) in [
        (FoundBy::RatingKey, "rating key"),
        (FoundBy::ExternalId, "external id"),
    ] {
        let kept = stats.kept_by(path);
        if kept.is_empty() {
            continue;
        }
        lines.push(format!(
            "{} title(s) would have derived a different item_id this walk; kept their existing \
             one (found by {by}) rather than forking a new row",
            kept.len()
        ));
        for entry in kept.iter().take(KEPT_SHOWN) {
            lines.push(format!(
                "    {} [rating key {}]",
                entry.label, entry.rating_key
            ));
            lines.push(format!(
                "        kept {}, would have derived {}",
                entry.kept_id, entry.derived_id
            ));
        }
        if kept.len() > KEPT_SHOWN {
            lines.push(format!("    ... and {} more", kept.len() - KEPT_SHOWN));
        }
    }
    if !stats.identities_forked.is_empty() {
        lines.push(format!(
            "{} title(s) matched an id another rating key already holds under a different \
             title; took an id of their own rather than overwriting it",
            stats.identities_forked.len()
        ));
        for fork in &stats.identities_forked {
            lines.push(format!(
                "    {} [rating key {}]",
                fork.label, fork.rating_key
            ));
            lines.push(format!(
                "        landed on {}; {} is {} [rating key {}]",
                fork.forked_id,
                fork.contested_id,
                crate::decisions::py_repr(&fork.contested_title),
                fork.contested_by
            ));
        }
    }
    lines
}
