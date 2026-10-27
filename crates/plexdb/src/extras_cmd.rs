//! The extras steps as commands: `fold-extras`, `extras-walk`, `extras status` and the
//! `extras-tick` that `plexdb schedule` runs every `PLEXDB_EXTRAS_EVERY` minutes
//! (ADR-0030), and the commands that record a person's decisions: `extras set|tag|organize`
//! append to `extra_decisions.json`, `extras undo` reverses a move.

use plexdb_store::{Config, StoreError, open_readonly, open_store, open_store_exclusive};
use rusqlite::Connection;

use crate::decisions::FoldError;
use crate::extras_decisions::{self as decisions, Decision, Entry, SOURCE_URL};
use crate::extras_declaration::{self as declaration, EXTRAS_FILE, ExtrasError};
use crate::extras_organize;
use crate::extras_walk;
use crate::sources::Sources;
use crate::staleness::Now;
use crate::steps::{Failure, failure};

pub const DECISIONS: &str = decisions::NAME;
pub const ORGANIZE: &str = extras_organize::NAME;
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

fn fold_failure(err: FoldError) -> Failure {
    match err {
        FoldError::File(err) => Failure::Failed(err.0),
        FoldError::Store(err) => failure(err),
        FoldError::Sqlite(err) => failure(StoreError::from(err)),
    }
}

/// Fold `extra_decisions.json`, then act on its organize decisions, printing the report.
fn fold_decisions(
    conn: &mut Connection,
    config: &Config,
    roots: &[String],
) -> Result<(), FoldError> {
    let path = config.decisions_file(decisions::FILE);
    let now = Now::read().iso();
    let stats = decisions::fold(conn, &path, roots, &now)?;
    for line in decisions::describe(&stats, &path) {
        println!("{line}");
    }
    if stats.file_found {
        for line in extras_organize::run_pending(conn, &path, &now)? {
            println!("{line}");
        }
    }
    Ok(())
}

/// `fold-extra-decisions`: apply `extra_decisions.json` (set, tag, delete, split).
pub fn run_fold_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(DECISIONS, args)?;
    let config = Config::from_env().map_err(failure)?;
    let roots = Sources::from_env();
    let roots = roots.require_roots(NEEDS_ROOTS).map_err(failure)?;
    let mut store = open_store(&config.store_path).map_err(failure)?;
    let path = config.decisions_file(decisions::FILE);
    let stats =
        decisions::fold(&mut store.conn, &path, roots, &Now::read().iso()).map_err(fold_failure)?;
    for line in decisions::describe(&stats, &path) {
        println!("{line}");
    }
    Ok(())
}

/// `extras-organize`: act on the organize decisions in `extra_decisions.json`.
pub fn run_organize(args: &[String]) -> Result<(), Failure> {
    no_args(ORGANIZE, args)?;
    let config = Config::from_env().map_err(failure)?;
    let mut store = open_store(&config.store_path).map_err(failure)?;
    let path = config.decisions_file(decisions::FILE);
    let lines = extras_organize::run_pending(&mut store.conn, &path, &Now::read().iso())
        .map_err(fold_failure)?;
    for line in lines {
        println!("{line}");
    }
    Ok(())
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

/// `extras-tick`: fold, walk, fold the decisions and organize, as the store's only writer. A sweep or another step
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
    let decided = fold_decisions(&mut store.conn, &config, roots);
    folded.map_err(extras_failure)?;
    decided.map_err(fold_failure)
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

fn usage() -> Failure {
    Failure::Usage(
        "extras takes status, set <item_id> <field> <value>, tag <item_id> <tag>, \
         organize [--dry-run] [<item_id>...] or undo <move_id>"
            .into(),
    )
}

fn record(config: &Config, decision: Decision) -> Result<(), Failure> {
    let path = config.decisions_file(decisions::FILE);
    let entry = Entry {
        decision,
        decided_at: Now::read().iso(),
    };
    decisions::record(&path, &entry).map_err(|err| Failure::Failed(err.0))?;
    println!("recorded in {}", path.display());
    Ok(())
}

fn require_item(conn: &Connection, item_id: &str) -> Result<String, Failure> {
    decisions::library_of(conn, item_id)
        .map_err(|err| failure(StoreError::from(err)))?
        .ok_or_else(|| Failure::Failed(format!("{item_id} is not an extra in the store")))
}

fn run_set(config: &Config, item_id: &str, field: &str, value: &str) -> Result<(), Failure> {
    let conn = open_readonly(&config.store_path).map_err(failure)?;
    let library = require_item(&conn, item_id)?;
    if field != SOURCE_URL {
        let kind: Option<String> = conn
            .query_row(
                "SELECT kind FROM extra_field WHERE library = ? AND name = ?",
                [&library, field],
                |row| row.get(0),
            )
            .ok();
        let Some(kind) = kind else {
            return Err(Failure::Failed(format!(
                "{library} declares no field {field}"
            )));
        };
        decisions::field_values(&kind, field, value).map_err(Failure::Failed)?;
    }
    record(
        config,
        Decision::Set {
            item_id: item_id.into(),
            field: field.into(),
            value: value.into(),
        },
    )
}

fn run_tag(config: &Config, item_id: &str, tag: &str) -> Result<(), Failure> {
    let conn = open_readonly(&config.store_path).map_err(failure)?;
    require_item(&conn, item_id)?;
    if tag.trim().is_empty() {
        return Err(Failure::Failed("the tag is empty".into()));
    }
    record(
        config,
        Decision::Tag {
            item_id: item_id.into(),
            tag: tag.into(),
        },
    )
}

fn run_organize_decision(config: &Config, args: &[String]) -> Result<(), Failure> {
    let dry = args.iter().any(|arg| arg == "--dry-run");
    let items: Vec<&String> = args.iter().filter(|arg| *arg != "--dry-run").collect();
    let conn = open_readonly(&config.store_path).map_err(failure)?;
    let path = config.decisions_file(decisions::FILE);
    let entries = decisions::load(&path)
        .map_err(|err| Failure::Failed(err.0))?
        .unwrap_or_default();
    let overlay = extras_organize::Overlay::of(&conn, &entries).map_err(failure)?;
    if dry {
        let wanted: Vec<String> = if items.is_empty() {
            extras_organize::pending(&conn, &entries).map_err(failure)?
        } else {
            items.iter().map(|item| (*item).clone()).collect()
        };
        println!(
            "extras organize --dry-run: {} item(s), nothing moved",
            wanted.len()
        );
        for item_id in wanted {
            println!(
                "{}",
                extras_organize::plan(&conn, &item_id, &overlay)
                    .map_err(failure)?
                    .line()
            );
        }
        return Ok(());
    }
    if items.is_empty() {
        return Err(Failure::Usage(
            "extras organize takes one or more item ids, or --dry-run alone".into(),
        ));
    }
    for item_id in &items {
        require_item(&conn, item_id)?;
        let plan = extras_organize::plan(&conn, item_id, &overlay).map_err(failure)?;
        if let extras_organize::Action::Refused(why) = &plan.action {
            return Err(Failure::Failed(format!(
                "{item_id}: {why}; nothing was recorded"
            )));
        }
    }
    for item_id in &items {
        record(
            config,
            Decision::Organize {
                item_id: (*item_id).clone(),
            },
        )?;
    }
    Ok(())
}

fn run_undo(config: &Config, id: &str) -> Result<(), Failure> {
    let move_id: i64 = id
        .parse()
        .map_err(|_| Failure::Usage(format!("extras undo takes a move id, got {id}")))?;
    let mut store = open_store(&config.store_path).map_err(failure)?;
    let line =
        extras_organize::undo(&mut store.conn, move_id, &Now::read().iso()).map_err(failure)?;
    println!("{line}");
    Ok(())
}

/// `plexdb extras <subcommand>`.
pub fn run(args: &[String]) -> Result<(), Failure> {
    let config = || Config::from_env().map_err(failure);
    match args {
        [sub] if sub == "status" => {
            let conn = open_readonly(&config()?.store_path).map_err(failure)?;
            for line in status_lines(&conn).map_err(failure)? {
                println!("{line}");
            }
            Ok(())
        }
        [sub, item_id, field, value] if sub == "set" => run_set(&config()?, item_id, field, value),
        [sub, item_id, tag] if sub == "tag" => run_tag(&config()?, item_id, tag),
        [sub, rest @ ..] if sub == "organize" => run_organize_decision(&config()?, rest),
        [sub, id] if sub == "undo" => run_undo(&config()?, id),
        _ => Err(usage()),
    }
}
