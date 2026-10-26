//! The sweep steps this binary runs. Their order and failure policy are declared
//! in `plexdb/rust_steps.json`, which the Python sweep reads to plan a run
//! without starting this binary; this module only maps each name to its code.

use plexdb_store::{Config, StoreError, open_store};

use crate::decisions::FoldError;
use crate::sources::Sources;
use crate::{
    frames_cmd, judge_steps, keyword_verdicts, latent_users, list_decisions, list_steps, media,
    merge_decisions, migrate, name_decisions, plays, prune_frames, repair, repair_fs,
    role_decisions, source_steps, tmdb_steps, walk,
};

/// Why a step stopped. `Config` is a setting that is absent, which the sweep
/// reports as skipped rather than failed.
#[derive(Debug)]
pub enum Failure {
    Config(String),
    Failed(String),
    /// The command line is wrong: an unknown flag, a missing value.
    Usage(String),
    /// The step ran and printed its report, and its result is still a failure (a
    /// scraper whose pages stopped parsing, a spent quota): exit 1, no `error:` line.
    Reported,
}

/// A step, given the arguments after its name.
pub type Run = fn(&[String]) -> Result<(), Failure>;

/// A step that takes no arguments.
fn no_args(name: &str, args: &[String]) -> Result<(), Failure> {
    match args.first() {
        None => Ok(()),
        Some(_) => Err(Failure::Usage(format!(
            "{name} takes no arguments, got {}",
            args.join(" ")
        ))),
    }
}

pub(crate) fn failure(err: StoreError) -> Failure {
    match err {
        StoreError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}

fn migrate(args: &[String]) -> Result<(), Failure> {
    no_args(migrate::NAME, args)?;
    migrate::run().map_err(failure)
}

fn fold(result: Result<(), FoldError>) -> Result<(), Failure> {
    result.map_err(|err| match err {
        FoldError::Store(err) => failure(err),
        other => Failure::Failed(other.to_string()),
    })
}

fn fold_list_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(list_decisions::NAME, args)?;
    fold(list_decisions::run())
}

fn fold_merge_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(merge_decisions::NAME, args)?;
    fold(merge_decisions::run())
}

fn fold_role_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(role_decisions::NAME, args)?;
    fold(role_decisions::run())
}

fn fold_name_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(name_decisions::NAME, args)?;
    fold(name_decisions::run())
}

/// A step whose only argument is `--section KEY` (or `--section=KEY`).
fn section_arg(name: &str, args: &[String]) -> Result<Option<String>, Failure> {
    match args {
        [] => Ok(None),
        [flag, key] if flag == "--section" => Ok(Some(key.clone())),
        [joined] if joined.starts_with("--section=") => {
            Ok(Some(joined["--section=".len()..].to_string()))
        }
        _ => Err(Failure::Usage(format!(
            "{name} takes [--section KEY], got {}",
            args.join(" ")
        ))),
    }
}

/// A step whose only argument is `--dry-run`.
fn dry_run_arg(name: &str, args: &[String]) -> Result<bool, Failure> {
    match args {
        [] => Ok(false),
        [flag] if flag == "--dry-run" => Ok(true),
        _ => Err(Failure::Usage(format!(
            "{name} takes [--dry-run], got {}",
            args.join(" ")
        ))),
    }
}

fn walk(args: &[String]) -> Result<(), Failure> {
    let section = section_arg(walk::NAME, args)?;
    (|| {
        let config = Config::from_env()?;
        let sources = Sources::from_env();
        let client = sources.plex("walk the library")?;
        // An empty root list is silently wrong rather than harmlessly absent: a
        // GUID-less title's item_id becomes a hash of the mount point, so it joins
        // with nothing in any other store (issue #24). Refuse rather than write it.
        let roots = sources.require_roots(
            "PLEX_SOURCE_ROOTS must be set in .env to walk the library. \
             It is the mount root to strip from a Plex path before hashing it, and \
             a title with no external GUID gets its item_id from that hash — so with \
             no root set, the id encodes where the disk is mounted and matches no \
             other store. Set it to the directory the library sections live under \
             (the section paths reported by Plex all sit beneath it).",
        )?;
        let mut store = open_store(&config.store_path)?;
        let stats = walk::walk_all(&mut store.conn, &client, roots, section.as_deref())?;
        for line in walk::describe(&stats) {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

fn media_walk(args: &[String]) -> Result<(), Failure> {
    let section = section_arg(media::NAME_WALK, args)?;
    (|| {
        let config = Config::from_env()?;
        let client = Sources::from_env().plex("read Plex's media")?;
        let mut store = open_store(&config.store_path)?;
        let stats = media::media_walk(&mut store.conn, &client, section.as_deref())?;
        println!("{}", media::describe(&stats));
        Ok(())
    })()
    .map_err(failure)
}

fn ingest_plays(args: &[String]) -> Result<(), Failure> {
    no_args(plays::NAME_INGEST, args)?;
    (|| {
        let config = Config::from_env()?;
        let client = Sources::from_env().plex("ingest watch history")?;
        let mut store = open_store(&config.store_path)?;
        let stats = plays::ingest_plays(&mut store.conn, &client)?;
        for line in plays::describe_ingest(&stats) {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

fn enrich_tautulli_plays(args: &[String]) -> Result<(), Failure> {
    no_args(plays::NAME_TAUTULLI, args)?;
    (|| {
        let config = Config::from_env()?;
        let sources = Sources::from_env();
        if sources.tautulli_url.is_empty() || sources.tautulli_api_key.is_empty() {
            return Err(StoreError::Config(
                "TAUTULLI_URL and TAUTULLI_API_KEY must be set in .env to enrich plays \
                 from Tautulli"
                    .into(),
            ));
        }
        if sources.plex_url.is_empty() || sources.plex_token.is_empty() {
            return Err(StoreError::Config(
                "PLEX_URL and PLEX_TOKEN must be set in .env to resolve account ids \
                 against Plex"
                    .into(),
            ));
        }
        let plex = sources.plex("resolve account ids against Plex")?;
        let tautulli = sources.tautulli()?;
        use crate::plex::PlexSource as _;
        use crate::tautulli::TautulliSource as _;
        let account_ids = plays::build_account_id_map(&plex.accounts()?, &tautulli.users()?);
        if !account_ids.tautulli_only_names.is_empty() {
            println!(
                "Tautulli reports account name(s) with no matching Plex account: {}",
                account_ids.tautulli_only_names.join(", ")
            );
        }
        if !account_ids.plex_only_names.is_empty() {
            println!(
                "Plex reports account name(s) with no matching Tautulli user: {}",
                account_ids.plex_only_names.join(", ")
            );
        }
        let mut store = open_store(&config.store_path)?;
        let stats = plays::match_tautulli_history(&mut store.conn, &tautulli, &account_ids)?;
        for line in plays::describe_match(&stats) {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

fn repair_identities(args: &[String]) -> Result<(), Failure> {
    let dry_run = dry_run_arg(repair::NAME, args)?;
    (|| {
        let config = Config::from_env()?;
        let sources = Sources::from_env();
        let client = sources.plex(
            "repair identities — only Plex knows what media kind each rating key is, and \
             that is what separates a fused identity from a legitimately merged one",
        )?;
        // The re-walk derives a path-hashed id for every title with no external GUID, so an
        // empty root list would write ids that encode where the disk is mounted (issue #24).
        let roots = sources.require_roots(
            "PLEX_SOURCE_ROOTS must be set in .env to repair identities: the re-walk hashes \
             a GUID-less title's path with the mount root stripped, and with no root set the \
             id encodes where the disk is mounted and matches no other store",
        )?;
        let mut store = open_store(&config.store_path)?;
        for line in repair::run(&mut store.conn, &client, roots, dry_run)? {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

fn repair_fs_identities(args: &[String]) -> Result<(), Failure> {
    let dry_run = dry_run_arg(repair_fs::NAME, args)?;
    (|| {
        let config = Config::from_env()?;
        let sources = Sources::from_env();
        let client = sources.plex(
            "repair fs: identities — the corrected id needs each title's current playback \
             path from Plex",
        )?;
        let roots = sources.require_roots(
            "PLEX_SOURCE_ROOTS must be set in .env to repair fs: identities — it is the \
             mount root a corrected id is computed relative to, the same requirement \
             `plexdb walk` enforces",
        )?;
        for line in repair_fs::run(&config, &client, roots, dry_run)? {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

fn prune_frames(args: &[String]) -> Result<(), Failure> {
    no_args(prune_frames::NAME, args)?;
    (|| {
        let config = Config::from_env()?;
        let path = crate::frames::FrameConfig::from_env()?.require_frames_path()?;
        if !path.exists() {
            println!("no frames.db at {}; nothing to prune", path.display());
            return Ok(());
        }
        let live = prune_frames::live_item_ids(&config.store_path)?;
        if live.is_empty() {
            // A store re-created or restored before its first walk holds no titles,
            // and pruning against it would delete the whole Frame index — months of
            // encoder time to rebuild — when no title was actually removed.
            return Err(StoreError::Store(format!(
                "items is empty in {}; refusing to prune frames.db",
                config.store_path.display()
            )));
        }
        let mut frames = crate::frames::open_frames(&path)?;
        let stats = prune_frames::prune_orphans(&mut frames, &live)?;
        for line in prune_frames::describe(&stats) {
            println!("{line}");
        }
        Ok(())
    })()
    .map_err(failure)
}

/// Every step, by the name `plexdb/rust_steps.json` declares it under.
pub const STEPS: &[(&str, Run)] = &[
    (migrate::NAME, migrate),
    (merge_decisions::NAME, fold_merge_decisions),
    (role_decisions::NAME, fold_role_decisions),
    (name_decisions::NAME, fold_name_decisions),
    (list_decisions::NAME, fold_list_decisions),
    (tmdb_steps::KEYWORDS, tmdb_steps::run_keywords),
    (tmdb_steps::LANGUAGE, tmdb_steps::run_language),
    (tmdb_steps::EDGES, tmdb_steps::run_edges),
    (source_steps::ANILIST, source_steps::run_anilist),
    (source_steps::LETTERBOXD, source_steps::run_letterboxd),
    (source_steps::WIKIDATA, source_steps::run_wikidata),
    (
        source_steps::MDBLIST_RATINGS,
        source_steps::run_mdblist_ratings,
    ),
    (list_steps::HARVEST_MDBLIST, list_steps::harvest_mdblist),
    (
        list_steps::HARVEST_LETTERBOXD_LISTS,
        list_steps::harvest_letterboxd_lists,
    ),
    (
        list_steps::HARVEST_MDBLIST_SEARCH,
        list_steps::harvest_mdblist_search,
    ),
    (walk::NAME, walk),
    (media::NAME_WALK, media_walk),
    (repair::NAME, repair_identities),
    (repair_fs::NAME, repair_fs_identities),
    (prune_frames::NAME, prune_frames),
    (plays::NAME_INGEST, ingest_plays),
    (plays::NAME_TAUTULLI, enrich_tautulli_plays),
    (list_steps::LISTS, list_steps::lists),
    (list_steps::LIST, list_steps::list_one),
    (list_steps::LIST_DECIDE, list_steps::list_decide),
    (frames_cmd::NAME, frames_cmd::run),
    (latent_users::NAME, latent_users::run),
    (keyword_verdicts::PRUNE, keyword_verdicts::run_prune),
    (
        keyword_verdicts::FILM_SUFFIX_VERDICTS,
        keyword_verdicts::run_film_suffix,
    ),
    (judge_steps::PAIRS, judge_steps::run_pairs),
    (judge_steps::ROLES, judge_steps::run_roles),
];

pub fn find(name: &str) -> Option<Run> {
    STEPS
        .iter()
        .find(|(step, _)| *step == name)
        .map(|(_, run)| *run)
}
