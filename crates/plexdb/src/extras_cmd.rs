//! The extras steps as commands: `fold-extras`, `extras-walk`, `extras status` and the
//! `extras-tick` that `plexdb schedule` runs every `PLEXDB_EXTRAS_EVERY` minutes
//! (ADR-0030).

use plexdb_store::{Config, StoreError, open_readonly, open_store, open_store_exclusive};
use rusqlite::Connection;

use crate::extras_declaration::{self as declaration, EXTRAS_FILE, ExtrasError};
use crate::extras_walk;
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

/// `extras-tick`: fold, then walk, as the store's only writer. A sweep or another step
/// holding the writer lock refuses the tick (exit 75) rather than queueing it behind. A
/// refused `extras.json` does not stop the walk, which uses the declaration stored; the
/// tick then ends as a failure carrying why.
pub fn run_tick(args: &[String]) -> Result<(), Failure> {
    no_args(TICK, args)?;
    let config = Config::from_env().map_err(failure)?;
    let roots = Sources::from_env();
    let roots = roots.require_roots(NEEDS_ROOTS).map_err(failure)?;
    // Listing and hashing is the slow part and only reads, so it runs before the store is
    // asked for; the lock is held for the fold and the writes alone.
    let prepared = {
        let conn = open_readonly(&config.store_path).map_err(failure)?;
        extras_walk::prepare(&conn, None).map_err(failure)?
    };
    let Some(mut store) = open_store_exclusive(&config.store_path).map_err(failure)? else {
        return Err(Failure::Busy("a writer has the store open".into()));
    };
    let folded = fold(&mut store.conn, &config, roots);
    let stats = extras_walk::apply_prepared(&mut store.conn, &prepared, roots, &Now::read().iso())
        .map_err(failure)?;
    for line in extras_walk::describe(&stats) {
        println!("{line}");
    }
    folded.map_err(extras_failure)
}

/// One line per library: files, items, in queue, organized, missing, more than one file.
pub fn status_lines(conn: &Connection) -> Result<Vec<String>, StoreError> {
    let mut stmt = conn.prepare(
        "SELECT l.name, \
           (SELECT COUNT(*) FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
              WHERE i.library = l.name AND f.missing_since IS NULL), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND i.organized = 0), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND i.organized = 1), \
           (SELECT COUNT(*) FROM extra_file f JOIN extra_item i ON i.item_id = f.item_id \
              WHERE i.library = l.name AND f.missing_since IS NOT NULL), \
           (SELECT COUNT(*) FROM extra_item i WHERE i.library = l.name AND \
              (SELECT COUNT(*) FROM extra_file f WHERE f.item_id = i.item_id \
                 AND f.missing_since IS NULL) > 1) \
         FROM extra_library l ORDER BY l.name",
    )?;
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
