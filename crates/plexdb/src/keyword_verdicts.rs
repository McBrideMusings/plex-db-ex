//! `prune-keyword-verdicts` and `film-suffix-verdicts`: the two sweep steps that keep
//! the judge tables (`keyword_pairs`, `keyword_roles`, `keyword_role_decisions`,
//! `keyword_names`) in line with the keywords titles carry
//! (`plexdb/keywords.py` `prune_keyword_verdicts` and `write_film_suffix_verdicts`,
//! ADR-0018). Neither needs a service.

use std::collections::BTreeSet;

use rusqlite::{Connection, params};

use crate::decisions::thousands;
use crate::gated::{config, open};
use crate::staleness::Now;
use crate::steps::Failure;

pub const PRUNE: &str = "prune-keyword-verdicts";
pub const FILM_SUFFIX_VERDICTS: &str = "film-suffix-verdicts";

/// The `keyword_pairs.jev_model` the film-suffix rule writes, so a reader can tell its
/// verdicts from Jev's.
pub const FILM_SUFFIX_MODEL: &str = "rule:film-suffix";

/// Stored keywords whose "<keyword> film" means something else, so the film-suffix rule
/// never pairs the two: "art film" is not art, "student film" is not about students,
/// "race film" is a historical genre, "essay film" a form.
const FILM_SUFFIX_KEEP: [&str; 4] = ["art", "essay", "race", "student"];

const SUFFIX: &str = " film";

/// Every keyword value some title carries, as a subquery. AniList stores a tag it flags
/// as a spoiler under `spoiler_keyword`, and still states a role for it.
const CARRIED: &str = "SELECT value FROM enrichment WHERE namespace = 'keywords' \
                       AND key IN ('keyword', 'spoiler_keyword')";

#[derive(Debug, Default, PartialEq, Eq)]
pub struct PruneStats {
    /// Distinct stored keyword values some title carries.
    pub keywords_stored: i64,
    pub pairs_deleted: usize,
    pub roles_deleted: usize,
    pub decisions_deleted: usize,
    pub names_deleted: usize,
    /// The keyword values whose rows were deleted, sorted.
    pub keywords_pruned: Vec<String>,
}

/// Delete every judge-table row keyed on a keyword value no `enrichment` row carries.
///
/// A value leaves `enrichment` when a source stops listing it or a change to
/// `normalize_keyword` rewrites it on a title's next fetch; the rows keyed on it then
/// describe nothing. A person's decision or name goes with its rows: the decisions file
/// it came from keeps it, and the fold restores it once the value, or the pair, has a
/// verdict again. One transaction.
pub fn prune_keyword_verdicts(conn: &mut Connection) -> rusqlite::Result<PruneStats> {
    let tx = conn.transaction()?;
    let mut stats = PruneStats {
        keywords_stored: tx.query_row(
            &format!("SELECT COUNT(*) FROM (SELECT DISTINCT * FROM ({CARRIED}))"),
            [],
            |row| row.get(0),
        )?,
        ..PruneStats::default()
    };
    stats.keywords_pruned = tx
        .prepare(&format!(
            "SELECT keyword_a FROM keyword_pairs UNION SELECT keyword_b FROM keyword_pairs \
             UNION SELECT keyword FROM keyword_roles \
             UNION SELECT keyword FROM keyword_role_decisions \
             UNION SELECT keyword FROM keyword_names \
             EXCEPT {CARRIED} ORDER BY 1"
        ))?
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<Result<_, _>>()?;
    stats.pairs_deleted = tx.execute(
        &format!(
            "DELETE FROM keyword_pairs \
             WHERE keyword_a NOT IN ({CARRIED}) OR keyword_b NOT IN ({CARRIED})"
        ),
        [],
    )?;
    stats.roles_deleted = tx.execute(
        &format!("DELETE FROM keyword_roles WHERE keyword NOT IN ({CARRIED})"),
        [],
    )?;
    stats.decisions_deleted = tx.execute(
        &format!("DELETE FROM keyword_role_decisions WHERE keyword NOT IN ({CARRIED})"),
        [],
    )?;
    stats.names_deleted = tx.execute(
        &format!("DELETE FROM keyword_names WHERE keyword NOT IN ({CARRIED})"),
        [],
    )?;
    tx.commit()?;
    Ok(stats)
}

pub fn prune_report(stats: &PruneStats) -> Vec<String> {
    let mut lines = vec![format!(
        "pruned {} keyword value(s) no title carries (of {} stored): {} pair row(s), \
         {} role row(s), {} decision row(s), {} name row(s)",
        thousands(stats.keywords_pruned.len()),
        thousands(usize::try_from(stats.keywords_stored).unwrap_or_default()),
        thousands(stats.pairs_deleted),
        thousands(stats.roles_deleted),
        thousands(stats.decisions_deleted),
        thousands(stats.names_deleted),
    )];
    lines.extend(
        stats
            .keywords_pruned
            .iter()
            .map(|keyword| format!("pruned keyword {}", crate::decisions::py_repr(keyword))),
    );
    lines
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct FilmSuffixStats {
    /// Pairs the rule holds this run.
    pub pairs: usize,
    /// Of those, pairs that had no rule row before: new, or a Jev score replaced.
    pub pairs_written: usize,
    /// Rule rows deleted because the rule no longer holds for them.
    pub pairs_removed: usize,
}

/// Pair every stored "X film" with its bare "X" when some title carries both values (on
/// any titles), unless X is in [`FILM_SUFFIX_KEEP`].
///
/// Each pair gets `jev_score` 1.0 under [`FILM_SUFFIX_MODEL`], replacing any score Jev
/// gave it; a person's `decision` on the pair stays. A rule row the rule no longer holds
/// for is deleted, unless a person has decided it, so the next judge run asks Jev about
/// it again. One transaction.
pub fn write_film_suffix_verdicts(
    conn: &mut Connection,
    judged_at: &str,
) -> rusqlite::Result<FilmSuffixStats> {
    let tx = conn.transaction()?;
    let carried: BTreeSet<String> = tx
        .prepare(&format!("SELECT DISTINCT value FROM ({CARRIED})"))?
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<Result<_, _>>()?;
    let pairs: BTreeSet<(String, String)> = carried
        .iter()
        .filter_map(|value| {
            let bare = value.strip_suffix(SUFFIX)?;
            (carried.contains(bare) && !FILM_SUFFIX_KEEP.contains(&bare))
                .then(|| order(bare.to_string(), value.clone()))
        })
        .collect();
    let mut stats = FilmSuffixStats {
        pairs: pairs.len(),
        ..FilmSuffixStats::default()
    };
    let held: BTreeSet<(String, String)> = tx
        .prepare("SELECT keyword_a, keyword_b FROM keyword_pairs WHERE jev_model = ?")?
        .query_map([FILM_SUFFIX_MODEL], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    for pair in held.difference(&pairs) {
        stats.pairs_removed += tx.execute(
            "DELETE FROM keyword_pairs WHERE keyword_a = ? AND keyword_b = ? \
             AND decision IS NULL",
            params![pair.0, pair.1],
        )?;
    }
    let fresh: Vec<&(String, String)> = pairs.difference(&held).collect();
    {
        let mut upsert = tx.prepare(
            "INSERT INTO keyword_pairs \
             (keyword_a, keyword_b, jev_score, jev_model, judged_at, jev_error) \
             VALUES (?, ?, 1.0, ?, ?, NULL) \
             ON CONFLICT (keyword_a, keyword_b) DO UPDATE SET jev_score = 1.0, \
             jev_model = excluded.jev_model, judged_at = excluded.judged_at, jev_error = NULL",
        )?;
        for (a, b) in &fresh {
            upsert.execute(params![a, b, FILM_SUFFIX_MODEL, judged_at])?;
        }
    }
    stats.pairs_written = fresh.len();
    tx.commit()?;
    Ok(stats)
}

fn order(a: String, b: String) -> (String, String) {
    if a < b { (a, b) } else { (b, a) }
}

pub fn film_suffix_report(stats: &FilmSuffixStats) -> String {
    format!(
        "film-suffix rule: {} pair(s) held, {} written now, {} removed",
        thousands(stats.pairs),
        thousands(stats.pairs_written),
        thousands(stats.pairs_removed)
    )
}

fn no_args(name: &str, args: &[String]) -> Result<(), Failure> {
    match args.first() {
        None => Ok(()),
        Some(_) => Err(Failure::Usage(format!(
            "{name} takes no arguments, got {}",
            args.join(" ")
        ))),
    }
}

fn sqlite(err: rusqlite::Error) -> Failure {
    Failure::Failed(err.to_string())
}

/// `plexdb prune-keyword-verdicts`.
pub fn run_prune(args: &[String]) -> Result<(), Failure> {
    no_args(PRUNE, args)?;
    let mut store = open(&config()?)?;
    let stats = prune_keyword_verdicts(&mut store.conn).map_err(sqlite)?;
    prune_report(&stats)
        .iter()
        .for_each(|line| println!("{line}"));
    Ok(())
}

/// `plexdb film-suffix-verdicts`.
pub fn run_film_suffix(args: &[String]) -> Result<(), Failure> {
    no_args(FILM_SUFFIX_VERDICTS, args)?;
    let mut store = open(&config()?)?;
    let now = Now::read().iso();
    let stats = write_film_suffix_verdicts(&mut store.conn, &now).map_err(sqlite)?;
    println!("{}", film_suffix_report(&stats));
    Ok(())
}
