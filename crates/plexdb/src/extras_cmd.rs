//! The extras steps as commands: `fold-extras`, `extras-walk`, `extras status` and the
//! `extras-tick` that `plexdb schedule` runs every `PLEXDB_EXTRAS_EVERY` minutes
//! (ADR-0030).

use plexdb_store::{Config, StoreError, open_readonly, open_store, open_store_exclusive};
use rusqlite::Connection;

use crate::extras_declaration::{self as declaration, EXTRAS_FILE, ExtrasError};
use crate::extras_near;
use crate::extras_walk;
use crate::frames::FrameConfig;
use crate::sources::Sources;
use crate::staleness::Now;
use crate::steps::{Failure, failure};

pub const TICK: &str = "extras-tick";
pub const COMMAND: &str = "extras";

const NEEDS_ROOTS: &str = "PLEX_SOURCE_ROOTS must be set in .env to fold or walk extras \
     libraries: every library root must sit under one, and an extra's item_id hashes its path \
     with that root stripped";

fn extras_failure(err: ExtrasError) -> Failure {
    match err {
        ExtrasError::Refused(message) => Failure::Failed(message),
        ExtrasError::Store(err) => failure(err),
    }
}

/// Fold `extras.json` into the store, printing the report line.
fn fold(conn: &mut Connection, config: &Config, roots: &[String]) -> Result<(), ExtrasError> {
    let path = config.decisions_file(EXTRAS_FILE);
    let now = Now::read().iso();
    let stats = declaration::fold_file(conn, &path, roots, &now)?;
    println!("{}", declaration::describe(&stats, &path));
    Ok(())
}

fn walk(conn: &mut Connection, roots: &[String]) -> Result<(), StoreError> {
    let stats = extras_walk::walk_all(conn, roots, None, &Now::read().iso())?;
    for line in extras_walk::describe(&stats) {
        println!("{line}");
    }
    Ok(())
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

/// `fold-extras`.
pub fn run_fold(args: &[String]) -> Result<(), Failure> {
    no_args(declaration::NAME, args)?;
    let config = Config::from_env().map_err(failure)?;
    let roots = Sources::from_env();
    let roots = roots.require_roots(NEEDS_ROOTS).map_err(failure)?;
    let mut store = open_store(&config.store_path).map_err(failure)?;
    fold(&mut store.conn, &config, roots).map_err(extras_failure)
}

/// `extras-walk`.
pub fn run_walk(args: &[String]) -> Result<(), Failure> {
    no_args(extras_walk::NAME, args)?;
    let config = Config::from_env().map_err(failure)?;
    let roots = Sources::from_env();
    let roots = roots.require_roots(NEEDS_ROOTS).map_err(failure)?;
    let mut store = open_store(&config.store_path).map_err(failure)?;
    walk(&mut store.conn, roots).map_err(failure)
}

/// What the near-duplicate pass compared before the writer lock, with the Frame index it read
/// from. `None` when no Frame index is configured or built yet.
fn near_prepare(
    config: &Config,
) -> Result<Option<(std::path::PathBuf, extras_near::Prepared)>, StoreError> {
    let Some(frames_path) = FrameConfig::from_env()?
        .frames_path()
        .filter(|p| p.exists())
    else {
        return Ok(None);
    };
    let threshold =
        extras_near::threshold_from(std::env::var(extras_near::THRESHOLD_VAR).ok().as_deref())?;
    let store = open_readonly(&config.store_path)?;
    let frames = extras_near::open_frames_readonly(&frames_path)?;
    let prepared = extras_near::prepare(&store, &frames, threshold)?;
    Ok(Some((frames_path, prepared)))
}

/// Write the folds the pass found, then take the folded items out of the Frame index.
fn near_finish(
    config: &Config,
    store: &mut Connection,
    found: Option<(std::path::PathBuf, extras_near::Prepared)>,
) -> Result<(), StoreError> {
    let Some((frames_path, prepared)) = found else {
        return Ok(());
    };
    let stats = extras_near::apply(store, &prepared, &Now::read().iso())?;
    let cleaned = extras_near::clean_frames(&frames_path, &open_readonly(&config.store_path)?)?;
    println!("{}", extras_near::describe(&prepared, &stats, cleaned));
    Ok(())
}

/// `extras-tick`: fold, then walk, then compare the Frame index's new extras, as the store's
/// only writer. A sweep or another step holding the writer lock refuses the tick (exit 75)
/// rather than queueing it behind. A refused `extras.json` does not stop the walk, which uses
/// the declaration stored; the tick then ends as a failure carrying why.
pub fn run_tick(args: &[String]) -> Result<(), Failure> {
    no_args(TICK, args)?;
    let config = Config::from_env().map_err(failure)?;
    let roots = Sources::from_env();
    let roots = roots.require_roots(NEEDS_ROOTS).map_err(failure)?;
    // Listing, hashing and comparing vectors is the slow part and only reads, so it runs before
    // the store is asked for; the lock is held for the fold and the writes alone.
    let prepared = {
        let conn = open_readonly(&config.store_path).map_err(failure)?;
        extras_walk::prepare(&conn, None).map_err(failure)?
    };
    let near = near_prepare(&config);
    let Some(mut store) = open_store_exclusive(&config.store_path).map_err(failure)? else {
        return Err(Failure::Busy("a writer has the store open".into()));
    };
    let folded = fold(&mut store.conn, &config, roots);
    let stats = extras_walk::apply_prepared(&mut store.conn, &prepared, roots, &Now::read().iso())
        .map_err(failure)?;
    for line in extras_walk::describe(&stats) {
        println!("{line}");
    }
    let near = near.and_then(|found| near_finish(&config, &mut store.conn, found));
    if let Err(err) = &near {
        eprintln!("{}: {err}", extras_near::NAME);
    }
    folded.map_err(extras_failure)?;
    near.map_err(failure)
}

/// One line per library: files, items, in queue, organized, missing, more than one file.
pub fn status_lines(conn: &Connection) -> Result<Vec<String>, StoreError> {
    // A near duplicate folded into another item (`extra_merge`) is that item's file, not an item
    // of its own: it leaves the item counts and its files count toward the item it joined.
    const LIVE: &str =
        "NOT EXISTS (SELECT 1 FROM extra_merge m WHERE m.merged_item_id = i.item_id)";
    let sql = format!(
        "SELECT l.name, \
           (SELECT COUNT(*) FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
              WHERE i.library = l.name AND f.missing_since IS NULL), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND {LIVE}), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND i.organized = 0 \
              AND {LIVE}), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND i.organized = 1 \
              AND {LIVE}), \
           (SELECT COUNT(*) FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
              WHERE i.library = l.name AND f.missing_since IS NOT NULL), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND {LIVE} AND \
              (SELECT COUNT(*) FROM extra_file f WHERE f.missing_since IS NULL AND \
                 (f.item_id = i.item_id OR f.item_id IN \
                    (SELECT merged_item_id FROM extra_merge m WHERE m.kept_item_id = i.item_id))) \
              > 1) \
         FROM extra_library l ORDER BY l.name"
    );
    let mut stmt = conn.prepare(&sql)?;
    let lines = stmt
        .query_map([], |row| {
            Ok(format!(
                "{}: files {}, items {}, in queue {}, organized {}, missing {}, \
                 more than one file {}",
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, i64>(6)?,
            ))
        })?
        .collect::<Result<Vec<_>, _>>()?;
    if lines.is_empty() {
        return Ok(vec!["no extras library declared".into()]);
    }
    Ok(lines)
}

/// `plexdb extras <subcommand>`; `status` is the one there is.
pub fn run(args: &[String]) -> Result<(), Failure> {
    match args {
        [sub] if sub == "status" => {
            let config = Config::from_env().map_err(failure)?;
            let conn = open_readonly(&config.store_path).map_err(failure)?;
            for line in status_lines(&conn).map_err(failure)? {
                println!("{line}");
            }
            Ok(())
        }
        _ => Err(Failure::Usage(format!(
            "extras takes status, got {}",
            args.join(" ")
        ))),
    }
}
