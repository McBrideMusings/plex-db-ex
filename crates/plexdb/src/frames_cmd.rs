//! `plexdb frames migrate | status | queue <item_id> [--by WHO]` — the Frame index's
//! operator surface (ADR-0024) (`plexdb/commands/frames.py`).
//!
//! Each creates `frames.db` and applies its migrations when needed, and none writes
//! `plexdb.db`. `probe-encoder` and `quant-agreement` still run in Python.

use plexdb_store::{Config, StoreError};
use rusqlite::{Connection, OpenFlags};

use crate::frame_encoder::configured_reachable;
use crate::frames::{
    self, FrameConfig, StatusInput, decoder_report, migrate_configured, open_frames, queue,
    section_titles,
};
use crate::localtime::Local;
use crate::pyjson::dumps_indent2;
use crate::steps::Failure;

pub const NAME: &str = "frames";

fn failure(err: StoreError) -> Failure {
    match err {
        StoreError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}

fn open_readonly(path: &std::path::Path) -> Result<Connection, StoreError> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|err| StoreError::Store(format!("cannot open {}: {err}", path.display())))?;
    conn.busy_timeout(std::time::Duration::from_millis(
        plexdb_store::BUSY_TIMEOUT_MS,
    ))?;
    Ok(conn)
}

pub(crate) fn which(program: &str) -> bool {
    if program.contains('/') {
        return std::path::Path::new(program).is_file();
    }
    std::env::var_os("PATH")
        .is_some_and(|paths| std::env::split_paths(&paths).any(|dir| dir.join(program).is_file()))
}

pub fn run(args: &[String]) -> Result<(), Failure> {
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    match words.as_slice() {
        ["migrate"] => migrate().map_err(failure),
        ["status"] => status().map_err(failure),
        ["queue", item_id] => queue_title(item_id, "cli").map_err(failure),
        ["queue", item_id, "--by", by] | ["queue", "--by", by, item_id] => {
            queue_title(item_id, by).map_err(failure)
        }
        _ => Err(Failure::Usage(
            "usage: frames migrate | status | queue <item_id> [--by WHO]".into(),
        )),
    }
}

fn migrate() -> Result<(), StoreError> {
    let config = FrameConfig::from_env()?;
    config.require_frames_path()?;
    println!("{}", migrate_configured(&config)?);
    Ok(())
}

fn status() -> Result<(), StoreError> {
    let config = FrameConfig::from_env()?;
    let path = config.require_frames_path()?;
    let store = Config::from_env()?;
    let reachable = configured_reachable(&config);
    let frames_conn = open_frames(&path)?;
    let store_conn = open_readonly(&store.store_path)?;
    let sections = section_titles(&store_conn)?;
    let report = frames::status(
        &frames_conn,
        &sections,
        &StatusInput {
            path: &path,
            hours: &config.frame_hours,
            now: Local::now(),
            encoder_reachable: reachable,
            ffmpeg_found: which(&config.ffmpeg) && which(&config.ffprobe),
            decoder: decoder_report(&path, &config.decode),
        },
    )?;
    println!("{}", dumps_indent2(&report));
    Ok(())
}

fn queue_title(item_id: &str, by: &str) -> Result<(), StoreError> {
    let config = FrameConfig::from_env()?;
    let path = config.require_frames_path()?;
    let store = Config::from_env()?;
    let known = open_readonly(&store.store_path)?
        .query_row("SELECT 1 FROM items WHERE item_id = ?1", [item_id], |_| {
            Ok(())
        })
        .is_ok();
    let frames_conn = open_frames(&path)?;
    let added = queue(&frames_conn, item_id, known, by, &Local::now())?;
    let waiting: i64 =
        frames_conn.query_row("SELECT count(*) FROM frame_queue", [], |row| row.get(0))?;
    let verb = if added { "queued" } else { "already queued" };
    println!(
        "{verb} {item_id}; {} title(s) in the queue",
        group_thousands(waiting)
    );
    Ok(())
}

/// Python's `f"{n:,}"`.
pub(crate) fn group_thousands(n: i64) -> String {
    let digits = n.abs().to_string();
    let mut out = String::new();
    for (ix, c) in digits.chars().enumerate() {
        if ix > 0 && (digits.len() - ix).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}
