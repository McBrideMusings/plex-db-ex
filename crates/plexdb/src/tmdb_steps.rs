//! `enrich-tmdb-keywords`, `enrich-tmdb-language` and `enrich-tmdb-edges`: the three
//! sweeps that ask TMDB about the walked movies and shows (`plexdb/commands/enrich_tmdb_*.py`).
//!
//! TMDB is an optional source behind an adapter (ADR-0004). Staleness gating means a
//! skipped pass costs nothing but freshness: the next run picks up exactly the
//! titles this one missed.

use crate::enrich_tmdb::{self, EnrichStats, SweepError};
use crate::enrich_tmdb_language::{self, LanguageStats};
use crate::gated::{config, credential, open, parse_options, resolve_stale_days};
use crate::steps::Failure;
use crate::tmdb::LiveClient;
use crate::tmdb_edges::{self, EdgeStats};

pub const KEYWORDS: &str = "enrich-tmdb-keywords";
pub const LANGUAGE: &str = "enrich-tmdb-language";
pub const EDGES: &str = "enrich-tmdb-edges";

const CREDENTIAL: &str = "TMDB_API_KEY";

fn failed(err: SweepError) -> Failure {
    Failure::Failed(err.to_string())
}

fn sqlite(err: rusqlite::Error) -> Failure {
    Failure::Failed(err.to_string())
}

/// What every one of the three steps does before it sweeps: flags, credential,
/// staleness window, an open store, and the rewipe when asked.
struct Prepared {
    client: LiveClient,
    stale_days: i64,
    store: plexdb_store::Store,
    rewipe: bool,
}

fn prepare(
    step: &str,
    args: &[String],
    stale_var: &str,
    purpose: &str,
) -> Result<Prepared, Failure> {
    let options = parse_options(step, args)?;
    let config = config()?;
    let key = credential(CREDENTIAL, purpose)?;
    let stale_days = resolve_stale_days(stale_var, options.stale_days)?;
    Ok(Prepared {
        client: LiveClient::new(&key),
        stale_days,
        store: open(&config)?,
        rewipe: options.rewipe,
    })
}

pub fn keywords_report(stats: &EnrichStats) -> Vec<String> {
    let mut lines = vec![format!(
        "tmdb keywords: {} title(s) seen, {} fetched, {} already cached, \
         {} skipped (no tmdb id), {} failed, {} keyword(s) written",
        stats.titles_seen,
        stats.titles_fetched,
        stats.titles_cached,
        stats.titles_skipped_no_tmdb_id,
        stats.titles_failed,
        stats.keywords_written
    )];
    if stats.titles_failed > 0 {
        lines.push(format!(
            "{} title(s) failed and were not cached — re-run to retry them",
            stats.titles_failed
        ));
    }
    lines
}

pub fn run_keywords(args: &[String]) -> Result<(), Failure> {
    let mut p = prepare(KEYWORDS, args, "tmdb_keywords", "fetch TMDB keywords")?;
    if p.rewipe {
        let removed = enrich_tmdb::wipe_namespace(&mut p.store.conn).map_err(sqlite)?;
        println!(
            "wiped {removed} row(s) from the {} namespace",
            plexdb_keywords::NAMESPACE
        );
    }
    let stats = enrich_tmdb::enrich_tmdb_keywords(&mut p.store.conn, &p.client, p.stale_days)
        .map_err(failed)?;
    keywords_report(&stats)
        .iter()
        .for_each(|line| println!("{line}"));
    Ok(())
}

pub fn language_report(stats: &LanguageStats) -> Vec<String> {
    let mut lines = vec![format!(
        "tmdb language: {} title(s) seen, {} fetched, {} already cached, \
         {} skipped (no tmdb id), {} failed, {} language(s) written, {} without one",
        stats.titles_seen,
        stats.titles_fetched,
        stats.titles_cached,
        stats.titles_skipped_no_tmdb_id,
        stats.titles_failed,
        stats.languages_written,
        stats.titles_without_language
    )];
    if stats.titles_failed > 0 {
        lines.push(format!(
            "{} title(s) failed and were not cached — re-run to retry them",
            stats.titles_failed
        ));
    }
    lines
}

pub fn run_language(args: &[String]) -> Result<(), Failure> {
    let mut p = prepare(
        LANGUAGE,
        args,
        "tmdb_language",
        "fetch TMDB original languages",
    )?;
    if p.rewipe {
        let removed = enrich_tmdb_language::wipe(&mut p.store.conn).map_err(sqlite)?;
        println!("wiped {removed} original-language and cursor row(s) from the tmdb namespace");
    }
    let stats =
        enrich_tmdb_language::enrich_tmdb_language(&mut p.store.conn, &p.client, p.stale_days)
            .map_err(failed)?;
    language_report(&stats)
        .iter()
        .for_each(|line| println!("{line}"));
    Ok(())
}

pub fn edges_report(by_type: &[(&str, EdgeStats)]) -> Vec<String> {
    let mut lines: Vec<String> = by_type
        .iter()
        .map(|(edge_type, stats)| {
            format!(
                "{edge_type}: {} title(s) seen, {} fetched, {} already cached, \
                 {} skipped (no tmdb id), {} failed, {} edge(s) written, \
                 {} edge(s) skipped (target not in library)",
                stats.titles_seen,
                stats.titles_fetched,
                stats.titles_cached,
                stats.titles_skipped_no_tmdb_id,
                stats.titles_failed,
                stats.edges_written,
                stats.edges_skipped_not_in_library
            )
        })
        .collect();
    if by_type.iter().any(|(_, stats)| stats.titles_failed > 0) {
        lines.push("some titles failed and were not cached — re-run to retry them".into());
    }
    lines
}

pub fn run_edges(args: &[String]) -> Result<(), Failure> {
    let mut p = prepare(EDGES, args, "tmdb_edges", "fetch TMDB edges")?;
    if p.rewipe {
        for edge_type in [
            tmdb_edges::RECOMMENDATIONS_EDGE_TYPE,
            tmdb_edges::SIMILAR_EDGE_TYPE,
        ] {
            let (edges, _) =
                tmdb_edges::wipe_edge_type(&mut p.store.conn, edge_type).map_err(sqlite)?;
            println!("wiped {edges} edge(s) of type {edge_type}");
        }
    }
    let by_type = tmdb_edges::refresh_tmdb_edges(&mut p.store.conn, &p.client, p.stale_days)
        .map_err(failed)?;
    edges_report(&by_type)
        .iter()
        .for_each(|line| println!("{line}"));
    Ok(())
}
