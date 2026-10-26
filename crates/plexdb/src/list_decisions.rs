//! `fold-list-decisions`: a person's decisions about crowd lists, from Plex
//! DB-EX's `list_decisions.json` into `list_decision` (`plexdb/list_decisions.py`,
//! ADR-0028).
//!
//! An entry names a list by URL, and the URL is canonical, so
//! `https://www.mdblist.com/lists/Lina/Top-Watched/` and the stored
//! `https://mdblist.com/lists/lina/top-watched` are one key. `plexdb list-decide`
//! and the list harvests still compute the same key in Python;
//! `tests/fixtures/canonical_list_url.json` is the specification both follow.

use std::path::Path;

use plexdb_store::{Config, StoreError, open_store, py_strip};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};

use crate::decisions::{self, DecisionsFileError, FoldStats, Spec, thousands};
use crate::pyurl::{unquote, urlparse};

pub const NAME: &str = "fold-list-decisions";
pub const LIST_DECISIONS_FILE: &str = "list_decisions.json";

pub const MDBLIST: &str = "mdblist";
pub const LETTERBOXD: &str = "letterboxd";

const MDBLIST_HOSTS: &[&str] = &["mdblist.com", "www.mdblist.com"];
const LETTERBOXD_HOSTS: &[&str] = &["letterboxd.com", "www.letterboxd.com"];

fn key_problem(key: &[String]) -> Option<&'static str> {
    match key.first().and_then(|url| canonical_list_url(url)) {
        Some(_) => None,
        None => Some("has a url that is not an MDBList or Letterboxd list"),
    }
}

/// `ListDecisions`: one entry per list, keyed by `url`.
pub const SPEC: Spec = Spec {
    key: &["url"],
    decisions: &["added", "excluded", "refetch", "retry", "removed"],
    values: &[],
    key_problem,
};

fn path_parts(raw: &str, hosts: &[&str]) -> Option<Vec<String>> {
    let url = urlparse(py_strip(raw))?;
    let host = url.hostname.unwrap_or_default();
    if !matches!(url.scheme.as_str(), "http" | "https") || !hosts.contains(&host.as_str()) {
        return None;
    }
    Some(
        url.path
            .split('/')
            .filter(|part| !part.is_empty())
            .map(unquote)
            .collect(),
    )
}

/// `(user, slug)`, lowercased, of `https://mdblist.com/lists/<user>/<slug>`.
pub fn list_url_parts(raw: &str) -> Option<(String, String)> {
    let parts = path_parts(raw, MDBLIST_HOSTS)?;
    if parts.len() != 3
        || parts[0].to_lowercase() != "lists"
        || parts[1].is_empty()
        || parts[2].is_empty()
    {
        return None;
    }
    Some((parts[1].to_lowercase(), parts[2].to_lowercase()))
}

/// `(user, slug)`, lowercased, of `https://letterboxd.com/<user>/list/<slug>/`,
/// a `/page/N/` suffix allowed.
pub fn letterboxd_list_parts(raw: &str) -> Option<(String, String)> {
    let parts = path_parts(raw, LETTERBOXD_HOSTS)?;
    if parts.len() < 3
        || parts[1].to_lowercase() != "list"
        || parts[0].is_empty()
        || parts[2].is_empty()
    {
        return None;
    }
    let rest = &parts[3..];
    let paged = rest.len() == 2
        && rest[0].to_lowercase() == "page"
        && !rest[1].is_empty()
        && rest[1].chars().all(|c| c.is_ascii_digit());
    if !rest.is_empty() && !paged {
        return None;
    }
    Some((parts[0].to_lowercase(), parts[2].to_lowercase()))
}

/// The one spelling of a list URL the store keys on, or `None` for text that
/// names neither an MDBList nor a Letterboxd list.
pub fn canonical_list_url(raw: &str) -> Option<String> {
    if let Some((user, slug)) = list_url_parts(raw) {
        return Some(format!("https://mdblist.com/lists/{user}/{slug}"));
    }
    letterboxd_list_parts(raw)
        .map(|(user, slug)| format!("https://letterboxd.com/{user}/list/{slug}"))
}

/// `collection.source` of the list a URL names, or `None` for text that is neither.
pub fn list_source(raw: &str) -> Option<&'static str> {
    if list_url_parts(raw).is_some() {
        return Some(MDBLIST);
    }
    letterboxd_list_parts(raw).map(|_| LETTERBOXD)
}

#[derive(Debug, thiserror::Error)]
pub enum FoldError {
    #[error(transparent)]
    File(#[from] DecisionsFileError),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
}

/// Apply `path` to `list_decision`; a missing file is no decisions.
///
/// Entries apply in order, so the last per canonical URL wins. `added` sets the
/// row's `added` flag, `excluded` and `removed` clear it, and `refetch` and
/// `retry` keep what the row said. The whole file is checked before the first row
/// changes, and applied in one transaction.
pub fn fold(conn: &mut Connection, path: &Path) -> Result<FoldStats, FoldError> {
    let mut stats = FoldStats::default();
    let Some(entries) = decisions::load(&SPEC, path)? else {
        return Ok(stats);
    };
    stats.file_found = true;
    stats.entries = entries.len();
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for entry in &entries {
        let Some(url) = canonical_list_url(&entry.key[0]) else {
            stats.unmatched += 1;
            continue;
        };
        stats.matched += 1;
        let held: Option<i64> = tx
            .query_row(
                "SELECT added FROM list_decision WHERE url = ?",
                [&url],
                |row| row.get(0),
            )
            .optional()?;
        let added = match entry.decision.as_str() {
            "added" => 1,
            "excluded" | "removed" => 0,
            _ => held.unwrap_or(0),
        };
        tx.execute(
            "INSERT INTO list_decision (url, decision, decided_at, added) VALUES (?, ?, ?, ?) \
             ON CONFLICT (url) DO UPDATE SET decision = excluded.decision, \
             decided_at = excluded.decided_at, added = excluded.added",
            params![url, entry.decision, entry.decided_at, added],
        )?;
    }
    tx.commit()?;
    Ok(stats)
}

pub fn describe(stats: &FoldStats, path: &Path) -> String {
    let path = path.display();
    if !stats.file_found {
        return format!("list decisions: no file at {path}");
    }
    format!(
        "list decisions: {} entr(ies) in {path}, {} named an MDBList or Letterboxd list, {} named none",
        thousands(stats.entries),
        thousands(stats.matched),
        thousands(stats.unmatched)
    )
}

/// `plexdb fold-list-decisions`.
pub fn run() -> Result<(), FoldError> {
    let config = Config::from_env()?;
    let path = config.decisions_file(LIST_DECISIONS_FILE);
    let mut store = open_store(&config.store_path)?;
    let stats = fold(&mut store.conn, &path)?;
    println!("{}", describe(&stats, &path));
    Ok(())
}
