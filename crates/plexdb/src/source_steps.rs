//! `enrich-anilist`, `enrich-wikidata`, `enrich-mdblist-ratings` and `enrich-letterboxd`: the
//! sweeps that ask the optional sources AniList, Wikidata, MDBList and Letterboxd about the
//! walked titles (`plexdb/commands/enrich_*.py`).
//!
//! Each source is optional behind an adapter (ADR-0004). Staleness gating means a skipped pass
//! costs nothing but freshness: the next run picks up exactly the titles this one missed.

use crate::anilist::LiveAniListClient;
use crate::enrich_anilist::{self, AniListStats};
use crate::enrich_letterboxd::{
    self, DEFAULT_MAX_TITLES, LetterboxdStats, MAX_PARSE_FAILURE_SHARE,
};
use crate::enrich_mdblist_ratings::{self, DEFAULT_MAX_REQUESTS, RatingsStats};
use crate::enrich_wikidata::{self, BATCH_SIZE, WikidataStats};
use crate::gated::{
    config, credential, open, parse_capped_options, parse_options, resolve_limit,
    resolve_stale_days,
};
use crate::letterboxd::LiveLbClient;
use crate::mdblist::LiveMdbClient;
use crate::steps::Failure;
use crate::wikidata::LiveWikidataClient;

pub const ANILIST: &str = "enrich-anilist";
pub const WIKIDATA: &str = "enrich-wikidata";
pub const MDBLIST_RATINGS: &str = "enrich-mdblist-ratings";
pub const LETTERBOXD: &str = "enrich-letterboxd";

fn sqlite(err: rusqlite::Error) -> Failure {
    Failure::Failed(err.to_string())
}

fn print_all(lines: &[String]) {
    lines.iter().for_each(|line| println!("{line}"));
}

pub fn anilist_report(stats: &AniListStats) -> Vec<String> {
    let mut lines = vec![
        format!(
            "anilist: {} title(s) seen, {} anime, {} fetched ({} matched) in {} batch(es) of \
             {} anilist id(s), {} already cached, {} failed",
            stats.titles_seen,
            stats.titles_anime,
            stats.titles_fetched,
            stats.titles_matched,
            stats.batches_sent,
            stats.anilist_ids_asked,
            stats.titles_cached,
            stats.titles_failed
        ),
        format!(
            "anilist: {} keyword(s), {} spoiler keyword(s), {} role row(s) written",
            stats.keywords_written, stats.spoiler_keywords_written, stats.roles_stated
        ),
    ];
    if stats.titles_failed > 0 {
        lines.push(format!(
            "{} title(s) failed and were not cached — re-run to retry them",
            stats.titles_failed
        ));
    }
    lines
}

/// `plexdb enrich-anilist [--stale-days N] [--rewipe]`.
pub fn run_anilist(args: &[String]) -> Result<(), Failure> {
    let options = parse_options(ANILIST, args)?;
    let config = config()?;
    let mut client = LiveAniListClient::new();
    let stale_days = resolve_stale_days("anilist", options.stale_days)?;
    let mut store = open(&config)?;
    if options.rewipe {
        let removed = enrich_anilist::wipe(&mut store.conn).map_err(sqlite)?;
        println!("wiped {removed} anilist row(s) from keywords, roles and cursors");
    }
    let stats = enrich_anilist::enrich_anilist(&mut store.conn, &mut client, stale_days)?;
    print_all(&anilist_report(&stats));
    Ok(())
}

pub fn wikidata_report(stats: &WikidataStats) -> Vec<String> {
    let mut lines = vec![
        format!(
            "wikidata: {} title(s) seen, {} fetched ({} matched) in {} quer{}, {} already \
             cached, {} skipped (no imdb id), {} failed",
            stats.titles_seen,
            stats.titles_fetched,
            stats.titles_matched,
            stats.queries_sent,
            if stats.queries_sent == 1 { "y" } else { "ies" },
            stats.titles_cached,
            stats.titles_skipped_no_imdb_id,
            stats.titles_failed
        ),
        format!(
            "wikidata: {} keyword(s), {} role row(s), {} award(s) written, {} role row(s) \
             deleted, no title stating them",
            stats.keywords_written, stats.roles_stated, stats.awards_written, stats.roles_pruned
        ),
    ];
    if stats.titles_failed > 0 {
        lines.push(format!(
            "{} title(s) failed and were not cached — re-run to retry them",
            stats.titles_failed
        ));
    }
    lines
}

/// `plexdb enrich-wikidata [--stale-days N] [--rewipe]`.
pub fn run_wikidata(args: &[String]) -> Result<(), Failure> {
    let options = parse_options(WIKIDATA, args)?;
    let config = config()?;
    let mut client = LiveWikidataClient::new();
    let stale_days = resolve_stale_days("wikidata", options.stale_days)?;
    let mut store = open(&config)?;
    if options.rewipe {
        let removed = enrich_wikidata::wipe(&mut store.conn).map_err(sqlite)?;
        println!("wiped {removed} wikidata row(s) from keywords, awards, roles and cursors");
    }
    let stats =
        enrich_wikidata::enrich_wikidata(&mut store.conn, &mut client, stale_days, BATCH_SIZE)?;
    print_all(&wikidata_report(&stats));
    Ok(())
}

pub fn ratings_report(stats: &RatingsStats) -> Vec<String> {
    let mut lines = vec![
        format!(
            "mdblist ratings: {} title(s) seen, {} fetched ({} rated, {} not found), {} already \
             cached, {} without an IMDb id, {} left for the next run, {} failed",
            stats.titles_seen,
            stats.titles_fetched,
            stats.titles_rated,
            stats.titles_not_found,
            stats.titles_cached,
            stats.titles_skipped_no_imdb_id,
            stats.titles_capped,
            stats.titles_failed
        ),
        format!(
            "mdblist ratings: {} request(s) sent, {} failed; {} rating(s) and {} vote count(s) \
             written",
            stats.requests_sent, stats.requests_failed, stats.ratings_written, stats.votes_written
        ),
    ];
    if stats.titles_failed > 0 {
        lines.push(format!(
            "{} title(s) failed and were not cached — re-run to retry them",
            stats.titles_failed
        ));
    }
    if let Some(quota) = &stats.quota_spent {
        lines.push(format!("mdblist ratings: stopped early — {quota}"));
    }
    lines
}

/// `plexdb enrich-mdblist-ratings [--stale-days N] [--limit N] [--rewipe]`. Exits 1 after its
/// report when MDBList's daily quota ran out.
pub fn run_mdblist_ratings(args: &[String]) -> Result<(), Failure> {
    let options = parse_capped_options(MDBLIST_RATINGS, args)?;
    let config = config()?;
    let mut client = LiveMdbClient::new(&credential("MDBLIST_API_KEY", "fetch MDBList ratings")?);
    let stale_days = resolve_stale_days("mdblist_ratings", options.stale_days)?;
    let limit = resolve_limit(
        "mdblist_ratings",
        "request",
        options.limit,
        DEFAULT_MAX_REQUESTS,
    )?;
    let mut store = open(&config)?;
    if options.rewipe {
        let removed = enrich_mdblist_ratings::wipe(&mut store.conn).map_err(sqlite)?;
        println!("wiped {removed} mdblist row(s) from ratings and cursors");
    }
    let stats = enrich_mdblist_ratings::enrich_mdblist_ratings(
        &mut store.conn,
        &mut client,
        stale_days,
        usize::try_from(limit).unwrap_or(usize::MAX),
    )?;
    print_all(&ratings_report(&stats));
    if stats.quota_spent.is_some() {
        return Err(Failure::Reported);
    }
    Ok(())
}

pub fn letterboxd_report(stats: &LetterboxdStats) -> Vec<String> {
    let mut lines = vec![
        format!(
            "letterboxd: {} movie(s) seen, {} fetched ({} with themes, {} not listed), {} \
             already cached, {} without a TMDB id, {} left for the next run, {} failed",
            stats.titles_seen,
            stats.titles_fetched,
            stats.titles_matched,
            stats.titles_not_listed,
            stats.titles_cached,
            stats.titles_skipped_no_tmdb_id,
            stats.titles_capped,
            stats.titles_failed
        ),
        format!(
            "letterboxd: {} keyword(s) written; {} parse failure(s) of {} film page(s)",
            stats.keywords_written,
            stats.parse_failures,
            stats.pages_fetched()
        ),
    ];
    if stats.titles_failed > 0 {
        lines.push(format!(
            "{} title(s) failed and were not cached — re-run to retry them",
            stats.titles_failed
        ));
    }
    if stats.parse_failed() {
        lines.push(format!(
            "letterboxd: {} of {} film page(s) had no film marker, over the {:.0}% limit — the \
             page layout may have changed",
            stats.parse_failures,
            stats.pages_fetched(),
            MAX_PARSE_FAILURE_SHARE * 100.0
        ));
    }
    lines
}

/// `plexdb enrich-letterboxd [--stale-days N] [--limit N] [--rewipe]`. Exits 1 after its report
/// when too many film pages stopped parsing.
pub fn run_letterboxd(args: &[String]) -> Result<(), Failure> {
    let options = parse_capped_options(LETTERBOXD, args)?;
    let config = config()?;
    let mut client = LiveLbClient::new();
    let stale_days = resolve_stale_days("letterboxd", options.stale_days)?;
    let limit = resolve_limit("letterboxd", "title", options.limit, DEFAULT_MAX_TITLES)?;
    let mut store = open(&config)?;
    if options.rewipe {
        let removed = enrich_letterboxd::wipe(&mut store.conn).map_err(sqlite)?;
        println!("wiped {removed} letterboxd row(s) from keywords and cursors");
    }
    let stats = enrich_letterboxd::enrich_letterboxd(
        &mut store.conn,
        &mut client,
        stale_days,
        usize::try_from(limit).unwrap_or(usize::MAX),
    )?;
    print_all(&letterboxd_report(&stats));
    if stats.parse_failed() {
        return Err(Failure::Reported);
    }
    Ok(())
}
