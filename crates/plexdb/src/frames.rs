//! The Frame index: `frames.db`, a second SQLite file beside the store (ADR-0024)
//! (`plexdb/frames.py`).
//!
//! Two rules shape every write here:
//!
//! - **Rollback-journal mode, not WAL.** A reader opens the file read-only where it
//!   lies and `ATTACH`es it to the Snapshot; with no `-wal`/`-shm` sidecars it needs no
//!   write access to the directory to do that.
//! - **No transaction touches both files.** Whatever this module needs from
//!   `plexdb.db` — which item_ids exist, which section each sits in — it reads through
//!   a separate read-only connection first, then writes `frames.db` alone.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::Once;
use std::time::Duration;

use plexdb_schema::frames::{MIGRATIONS, SCHEMA_VERSION};
use plexdb_schema::{apply_chain, current_version};
use plexdb_store::{BUSY_TIMEOUT_MS, StoreError, py_path, py_strip};
use rusqlite::{Connection, OpenFlags, params};

use crate::localtime::Local;
use crate::pyjson::Json;

pub use plexdb_schema::frames::FRAME_DIMS;

/// Every frames.db table keyed by item_id, in the order a prune deletes from.
pub const TITLE_TABLES: [&str; 5] = [
    "frame_vec",
    "cuts",
    "shots",
    "frame_queue",
    "frame_index_state",
];

/// The part of `plexdb/config.py`'s `Config` the frame commands read.
#[derive(Debug, Clone)]
pub struct FrameConfig {
    pub frames_dir: Option<PathBuf>,
    pub frame_hours: String,
    pub encoder_url: String,
    pub model: String,
    pub ffmpeg: String,
    pub ffprobe: String,
    /// `cpu` or `cuda`.
    pub decode: String,
    pub media_dir: Option<PathBuf>,
    pub source_roots: Vec<String>,
    pub plex_url: String,
    pub plex_token: String,
}

fn env_text(name: &str) -> String {
    std::env::var(name)
        .map(|raw| py_strip(&raw).to_string())
        .unwrap_or_default()
}

fn env_path(name: &str) -> Option<PathBuf> {
    let raw = env_text(name);
    (!raw.is_empty()).then(|| py_path(&raw))
}

impl FrameConfig {
    pub fn from_env() -> Result<Self, StoreError> {
        let decode = env_text("PLEXDB_FRAME_DECODE").to_lowercase();
        let decode = if decode.is_empty() {
            "cpu".to_string()
        } else {
            decode
        };
        if decode != "cpu" && decode != "cuda" {
            return Err(StoreError::Config(format!(
                "PLEXDB_FRAME_DECODE is {decode:?}; use one of cpu, cuda"
            )));
        }
        let or = |name: &str, default: &str| {
            let raw = env_text(name);
            if raw.is_empty() {
                default.to_string()
            } else {
                raw
            }
        };
        Ok(FrameConfig {
            frames_dir: env_path("PLEXDB_FRAMES_DIR"),
            frame_hours: env_text("PLEXDB_FRAME_HOURS"),
            encoder_url: env_text("PLEXDB_FRAME_ENCODER_URL"),
            model: env_text("PLEXDB_FRAME_MODEL"),
            ffmpeg: or("PLEXDB_FFMPEG", "ffmpeg"),
            ffprobe: or("PLEXDB_FFPROBE", "ffprobe"),
            decode,
            media_dir: env_path("PLEXDB_MEDIA_DIR"),
            source_roots: env_text("PLEX_SOURCE_ROOTS")
                .split(',')
                .map(|root| py_strip(root).to_string())
                .filter(|root| !root.is_empty())
                .collect(),
            plex_url: env_text("PLEX_URL"),
            plex_token: env_text("PLEX_TOKEN"),
        })
    }

    /// `frames.db` inside `frames_dir`, or `None` when that is unset.
    pub fn frames_path(&self) -> Option<PathBuf> {
        self.frames_dir.as_ref().map(|dir| dir.join("frames.db"))
    }

    /// `frames.db`'s path, or a `Config` error when nobody configured it.
    pub fn require_frames_path(&self) -> Result<PathBuf, StoreError> {
        self.frames_path().ok_or_else(|| {
            StoreError::Config("PLEXDB_FRAMES_DIR is not set, so there is no Frame index".into())
        })
    }
}

/// Registers the `sqlite-vec` extension for every connection this process opens
/// afterwards, once.
pub fn load_vec_extension() {
    static LOAD: Once = Once::new();
    LOAD.call_once(|| unsafe {
        // SAFETY: `sqlite3_vec_init` is the extension entry point SQLite documents for
        // `sqlite3_auto_extension`; the cast only restates its signature as the
        // callback type.
        rusqlite::ffi::sqlite3_auto_extension(Some(std::mem::transmute::<
            *const (),
            unsafe extern "C" fn(
                *mut rusqlite::ffi::sqlite3,
                *mut *mut std::os::raw::c_char,
                *const rusqlite::ffi::sqlite3_api_routines,
            ) -> std::os::raw::c_int,
        >(
            sqlite_vec::sqlite3_vec_init as *const ()
        )));
    });
}

fn connect(path: &Path) -> Result<Connection, StoreError> {
    if path.is_dir() {
        return Err(StoreError::Store(format!(
            "{} is a directory, not a frames.db",
            path.display()
        )));
    }
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(|err| {
            StoreError::Store(format!(
                "cannot create {}: {}",
                parent.display(),
                strerror(&err)
            ))
        })?;
    }
    load_vec_extension();
    let not_frames = |err: rusqlite::Error| {
        StoreError::Store(format!("{} is not a frames.db: {err}", path.display()))
    };
    let conn = Connection::open(path).map_err(not_frames)?;
    conn.pragma_update_and_check(None, "journal_mode", "DELETE", |_| Ok(()))
        .map_err(not_frames)?;
    conn.busy_timeout(Duration::from_millis(BUSY_TIMEOUT_MS))
        .map_err(not_frames)?;
    Ok(conn)
}

/// `OSError.strerror`: the operating system's own wording.
pub fn strerror(err: &std::io::Error) -> String {
    match err.raw_os_error() {
        Some(code) => {
            // SAFETY: `strerror` returns a NUL-terminated string valid until the next call.
            let text = unsafe { std::ffi::CStr::from_ptr(libc::strerror(code)) };
            text.to_string_lossy().into_owned()
        }
        None => err.to_string(),
    }
}

/// Open `frames.db` for writing, creating it and applying any pending migration first.
/// Opening a current file changes nothing, so every caller may open it this way.
pub fn open_frames(path: &Path) -> Result<Connection, StoreError> {
    let mut conn = connect(path)?;
    apply_chain(&mut conn, MIGRATIONS)?;
    Ok(conn)
}

/// Bring `frames.db` to the current schema, creating it if absent. Returns the version
/// it was at and the one it is at now.
///
/// No copy is taken first: the file is rebuildable and too large to copy (ADR-0024), and
/// each step runs in its own transaction, so a failure leaves the last step that fully
/// landed.
pub fn migrate(path: &Path) -> Result<(i64, i64), StoreError> {
    let mut conn = connect(path)?;
    let (was, now, _) = apply_chain(&mut conn, MIGRATIONS)?;
    Ok((was, now))
}

/// Migrate `frames.db` when the Frame index is configured, and say what happened in one
/// line (`plexdb frames migrate`; the container's startup migration calls it too).
pub fn migrate_configured(config: &FrameConfig) -> Result<String, StoreError> {
    let Some(path) = config.frames_path() else {
        return Ok("frames.db: not configured (PLEXDB_FRAMES_DIR is unset)".into());
    };
    let (was, now) = migrate(&path)?;
    let resolved = resolve(&path);
    Ok(if was == now {
        format!(
            "frames.db already current at schema v{now}: {}",
            resolved.display()
        )
    } else if was == 0 {
        format!("created frames.db at schema v{now}: {}", resolved.display())
    } else {
        format!(
            "migrated frames.db v{was} -> v{now}: {}",
            resolved.display()
        )
    })
}

/// `Path.resolve()`: absolute, symlinks followed; a path that does not exist yet keeps
/// its tail as written.
pub fn resolve(path: &Path) -> PathBuf {
    let absolute = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    let mut rest = Vec::new();
    let mut existing = absolute.as_path();
    loop {
        if let Ok(real) = std::fs::canonicalize(existing) {
            return rest
                .iter()
                .rev()
                .fold(real, |joined, part| joined.join(part));
        }
        match (existing.parent(), existing.file_name()) {
            (Some(parent), Some(name)) => {
                rest.push(name.to_os_string());
                existing = parent;
            }
            _ => return absolute,
        }
    }
}

// --- decoder fallback -----------------------------------------------------------

pub fn decoder_path(frames_file: &Path) -> PathBuf {
    parent_of(frames_file).join("decoder.json")
}

fn parent_of(path: &Path) -> PathBuf {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

/// Record that the harvester asked for CUDA and decodes on the CPU, and why.
pub fn write_decoder_fallback(frames_file: &Path, reason: &str) -> std::io::Result<()> {
    let body = serde_json::json!({ "cuda_failed": reason });
    std::fs::write(decoder_path(frames_file), body.to_string())
}

pub fn clear_decoder_fallback(frames_file: &Path) {
    let _ = std::fs::remove_file(decoder_path(frames_file));
}

/// What `plexdb frames status` shows as `decoder`: the requested decoder, or
/// `cpu (cuda requested: <reason>)` after the harvester fell back.
pub fn decoder_report(frames_file: &Path, requested: &str) -> String {
    if requested == "cpu" {
        return "cpu".into();
    }
    let reason = std::fs::read_to_string(decoder_path(frames_file))
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .and_then(|body| body.get("cuda_failed").map(|r| r.to_string()))
        .map(|raw| {
            // `str(reason)`: a JSON string prints bare.
            serde_json::from_str::<String>(&raw).unwrap_or(raw)
        });
    match reason {
        Some(reason) => format!("cpu (cuda requested: {reason})"),
        None => requested.to_string(),
    }
}

// --- hours ----------------------------------------------------------------------

/// A window of wall-clock times, as microseconds since midnight.
pub type Window = (u64, u64);

fn parse_clock(raw: &str) -> Option<u64> {
    let raw = raw.trim();
    let digits_ok = |s: &str, len: usize| s.len() == len && s.chars().all(|c| c.is_ascii_digit());
    let (hms, fraction) = match raw.split_once('.') {
        Some((hms, fraction)) => (hms, Some(fraction)),
        None => (raw, None),
    };
    let parts: Vec<&str> = if hms.contains(':') {
        hms.split(':').collect()
    } else if hms.len() == 4 && fraction.is_none() || hms.len() == 6 {
        let mut out = vec![&hms[0..2], &hms[2..4]];
        if hms.len() == 6 {
            out.push(&hms[4..6]);
        }
        out
    } else {
        vec![hms]
    };
    if parts.is_empty() || parts.len() > 3 || !parts.iter().all(|p| digits_ok(p, 2)) {
        return None;
    }
    let number = |ix: usize| parts.get(ix).map_or(0, |p| p.parse::<u64>().unwrap_or(0));
    let (h, m, s) = (number(0), number(1), number(2));
    if h > 23 || m > 59 || s > 59 {
        return None;
    }
    let micro = match fraction {
        None => 0,
        Some(f) if parts.len() == 3 && !f.is_empty() && f.chars().all(|c| c.is_ascii_digit()) => {
            let mut padded: String = f.chars().take(6).collect();
            while padded.len() < 6 {
                padded.push('0');
            }
            padded.parse::<u64>().ok()?
        }
        Some(_) => return None,
    };
    Some(((h * 60 + m) * 60 + s) * 1_000_000 + micro)
}

/// `"01:00-07:00"` -> the window; empty -> `None`. A window whose end is earlier than
/// its start runs past midnight.
pub fn parse_hours(raw: &str) -> Result<Option<Window>, StoreError> {
    if raw.is_empty() {
        return Ok(None);
    }
    let bad = || {
        StoreError::Config(format!(
            "PLEXDB_FRAME_HOURS must look like 01:00-07:00, got {}",
            py_repr(raw)
        ))
    };
    let pieces: Vec<&str> = raw.split('-').collect();
    let [start_raw, end_raw] = pieces.as_slice() else {
        return Err(bad());
    };
    let (Some(start), Some(end)) = (parse_clock(start_raw), parse_clock(end_raw)) else {
        return Err(bad());
    };
    if start == end {
        return Err(StoreError::Config(format!(
            "PLEXDB_FRAME_HOURS opens and closes at the same time: {}",
            py_repr(raw)
        )));
    }
    Ok(Some((start, end)))
}

/// Python's `repr` of a plain string: single quotes unless the text holds one and no
/// double quote.
pub fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Whether `now`'s wall-clock time falls in `window`. No window, never.
pub fn inside_hours(window: Option<Window>, now: &Local) -> bool {
    let Some((start, end)) = window else {
        return false;
    };
    let at = now.micros_of_day();
    if start < end {
        start <= at && at < end
    } else {
        at >= start || at < end
    }
}

// --- sections and status --------------------------------------------------------

/// The movies and episodes in each Plex section — the titles the harvester indexes. A
/// show has no file of its own.
pub fn section_titles(store: &Connection) -> rusqlite::Result<BTreeMap<String, BTreeSet<String>>> {
    let mut sections: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut stmt = store.prepare(
        "SELECT p.section_id, p.item_id FROM plex_items p \
         JOIN items i ON i.item_id = p.item_id WHERE i.type IN ('movie', 'episode')",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, rusqlite::types::Value>(0)?,
            row.get::<_, String>(1)?,
        ))
    })?;
    for row in rows {
        let (section, item) = row?;
        let section = match section {
            rusqlite::types::Value::Text(text) => text,
            rusqlite::types::Value::Integer(n) => n.to_string(),
            other => format!("{other:?}"),
        };
        sections.entry(section).or_default().insert(item);
    }
    Ok(sections)
}

fn section_order(section_id: &str) -> (u8, u64, String) {
    // `str.isdigit` is wider than ASCII; section ids are numbers Plex hands out.
    if !section_id.is_empty() && section_id.chars().all(|c| c.is_ascii_digit()) {
        (0, section_id.parse().unwrap_or(u64::MAX), String::new())
    } else {
        (1, 0, section_id.to_string())
    }
}

/// What `plexdb frames status` takes besides the two connections.
pub struct StatusInput<'a> {
    pub path: &'a Path,
    pub hours: &'a str,
    pub now: Local,
    pub encoder_reachable: Option<bool>,
    pub ffmpeg_found: bool,
    pub decoder: String,
}

/// What `plexdb frames status` prints. Its shape is documented in docs/schema.md; a key
/// added here goes there too.
pub fn status(
    frames: &Connection,
    sections: &BTreeMap<String, BTreeSet<String>>,
    input: &StatusInput,
) -> Result<Json, StoreError> {
    let indexed: BTreeSet<String> = frames
        .prepare("SELECT item_id FROM frame_index_state WHERE error IS NULL")?
        .query_map([], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let failed_total: i64 = frames.query_row(
        "SELECT count(*) FROM frame_index_state WHERE error IS NOT NULL",
        [],
        |row| row.get(0),
    )?;
    let failed: Vec<Json> = frames
        .prepare(
            "SELECT item_id, error, reason, indexed_at FROM frame_index_state \
             WHERE error IS NOT NULL ORDER BY indexed_at DESC, item_id LIMIT -1",
        )?
        .query_map([], |row| {
            Ok(Json::object([
                ("item_id", Json::Str(row.get(0)?)),
                ("error", Json::opt_str(row.get(1)?)),
                ("reason", Json::opt_str(row.get(2)?)),
                ("at", Json::Str(row.get(3)?)),
            ]))
        })?
        .collect::<Result<_, _>>()?;
    let queued: i64 = frames.query_row("SELECT count(*) FROM frame_queue", [], |row| row.get(0))?;
    let mut ordered: Vec<(&String, &BTreeSet<String>)> = sections.iter().collect();
    ordered.sort_by_key(|(id, _)| section_order(id));
    let sections_json: Vec<Json> = ordered
        .into_iter()
        .map(|(id, titles)| {
            Json::object([
                ("section_id", Json::str(id.as_str())),
                (
                    "indexed",
                    Json::Int(titles.intersection(&indexed).count() as i64),
                ),
                ("total", Json::Int(titles.len() as i64)),
            ])
        })
        .collect();
    let window = parse_hours(input.hours)?;
    let indexing = if harvester_running(input.path) {
        indexing(frames)?
    } else {
        Json::Null
    };
    let bytes = std::fs::metadata(input.path)?.len() as i64;
    Ok(Json::object([
        (
            "frames_db",
            Json::Str(resolve(input.path).display().to_string()),
        ),
        ("schema_version", Json::Int(current_version(frames)?)),
        ("bytes", Json::Int(bytes)),
        ("sections", Json::Array(sections_json)),
        ("queued", Json::Int(queued)),
        (
            "frame_hours",
            if input.hours.is_empty() {
                Json::Null
            } else {
                Json::str(input.hours)
            },
        ),
        ("inside_hours", Json::Bool(inside_hours(window, &input.now))),
        ("indexing", indexing),
        (
            "encoder_reachable",
            input.encoder_reachable.map_or(Json::Null, Json::Bool),
        ),
        ("ffmpeg_found", Json::Bool(input.ffmpeg_found)),
        ("decoder", Json::Str(input.decoder.clone())),
        ("failed_total", Json::Int(failed_total)),
        ("failed", Json::Array(failed)),
    ]))
}

fn indexing(frames: &Connection) -> Result<Json, StoreError> {
    let row: Option<(String, String)> = frames
        .query_row("SELECT item_id, since FROM frame_harvest", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .map(Some)
        .or_else(|err| match err {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })?;
    Ok(match row {
        None => Json::Null,
        Some((item_id, since)) => {
            Json::object([("item_id", Json::Str(item_id)), ("since", Json::Str(since))])
        }
    })
}

// --- the harvester slot -----------------------------------------------------------

fn flock_request(lock_type: i16) -> libc::flock {
    // SAFETY: `flock` is plain data; zero is a valid value for every field.
    let mut request: libc::flock = unsafe { std::mem::zeroed() };
    request.l_type = lock_type;
    request.l_whence = libc::SEEK_SET as _;
    request
}

/// The harvester slot, held for as long as this value lives: an open-file-description
/// lock on `harvest.lock` beside `frames.db`. The operating system drops it when the
/// holder exits, however it exits, so a crash leaves nothing to clear. The file carries
/// the holder's pid, and the title in hand comes from the `frame_harvest` row.
#[derive(Debug)]
pub struct HarvestLock {
    _file: std::fs::File,
}

impl HarvestLock {
    pub fn acquire(path: &Path) -> Result<HarvestLock, StoreError> {
        let dir = parent_of(path);
        std::fs::create_dir_all(&dir)?;
        let lock_path = dir.join("harvest.lock");
        let mut file = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .open(&lock_path)?;
        let mut request = flock_request(libc::F_WRLCK as _);
        // SAFETY: `file` is open for the duration of the call and `request` is a valid
        // `struct flock`.
        let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_OFD_SETLK, &mut request) };
        if rc == -1 {
            let err = std::io::Error::last_os_error();
            if !matches!(err.raw_os_error(), Some(libc::EAGAIN | libc::EACCES)) {
                return Err(err.into());
            }
            let mut pid = String::new();
            file.seek(SeekFrom::Start(0))?;
            file.read_to_string(&mut pid)?;
            let pid = py_strip(&pid);
            let pid = if pid.is_empty() { "unknown" } else { pid };
            return Err(StoreError::Store(format!(
                "another harvester is running (pid {pid}, {})",
                title_in_hand(path)
            )));
        }
        file.set_len(0)?;
        file.seek(SeekFrom::Start(0))?;
        file.write_all(std::process::id().to_string().as_bytes())?;
        file.flush()?;
        Ok(HarvestLock { _file: file })
    }
}

/// Whether a harvester holds `harvest.lock` now. Asks the operating system which lock
/// would block a write lock (`F_OFD_GETLK`) and takes none, so the probe never makes a
/// starting harvester see a holder; a missing lock file means no harvester has ever run.
pub fn harvester_running(path: &Path) -> bool {
    let Ok(file) = std::fs::File::open(parent_of(path).join("harvest.lock")) else {
        return false;
    };
    let mut request = flock_request(libc::F_WRLCK as _);
    // SAFETY: as in `acquire`; `F_OFD_GETLK` only reads the lock table.
    let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_OFD_GETLK, &mut request) };
    rc != -1 && request.l_type != libc::F_UNLCK as libc::c_short
}

fn title_in_hand(path: &Path) -> String {
    let read = || -> Result<Json, rusqlite::Error> {
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        conn.busy_timeout(Duration::from_secs(1))?;
        indexing(&conn).map_err(|err| match err {
            StoreError::Sqlite(err) => err,
            other => rusqlite::Error::ToSqlConversionFailure(Box::new(other)),
        })
    };
    match read() {
        Err(_) => "title unknown".into(),
        Ok(Json::Object(pairs)) => match pairs.first() {
            Some((_, Json::Str(item_id))) => format!("indexing {item_id}"),
            _ => "title unknown".into(),
        },
        Ok(_) => "between titles".into(),
    }
}

// --- writes ---------------------------------------------------------------------

/// Record the title being indexed, or `None` when none is.
pub fn set_indexing(
    frames: &Connection,
    item_id: Option<&str>,
    now: &Local,
) -> rusqlite::Result<()> {
    match item_id {
        None => frames.execute("DELETE FROM frame_harvest", []).map(drop),
        Some(item_id) => frames
            .execute(
                "INSERT OR REPLACE INTO frame_harvest (id, item_id, since) VALUES (1, ?1, ?2)",
                params![item_id, now.isoformat()],
            )
            .map(drop),
    }
}

/// Put `item_id` at the back of the queue. Returns whether it was added; a title already
/// queued keeps its place.
///
/// `known` is whether the store's `items` holds the id, read by the caller through its
/// own read-only connection so no transaction spans both files.
pub fn queue(
    frames: &Connection,
    item_id: &str,
    known: bool,
    by: &str,
    now: &Local,
) -> Result<bool, StoreError> {
    if !known {
        return Err(StoreError::Store(format!(
            "{item_id} is not in items — nothing to queue"
        )));
    }
    let added = frames.execute(
        "INSERT OR IGNORE INTO frame_queue (item_id, queued_at, queued_by) VALUES (?1, ?2, ?3)",
        params![item_id, now.isoformat(), by],
    )?;
    Ok(added == 1)
}

/// Delete one title's vectors by rowid and return how many went.
///
/// `DELETE FROM frame_vec WHERE item_id = ?` scans the whole vec0 table (sqlite-vec
/// 0.1.9); the title's rowids come from the shadow tables by partition key instead, and a
/// rowid delete is a point lookup.
pub fn delete_vectors(frames: &Connection, item_id: &str) -> rusqlite::Result<usize> {
    let rowids: Vec<i64> = frames
        .prepare(
            "SELECT r.rowid FROM frame_vec_rowids r \
             JOIN frame_vec_chunks k ON k.chunk_id = r.chunk_id WHERE k.partition00 = ?1",
        )?
        .query_map([item_id], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let mut delete = frames.prepare("DELETE FROM frame_vec WHERE rowid = ?1")?;
    for rowid in &rowids {
        delete.execute([rowid])?;
    }
    Ok(rowids.len())
}

pub const fn schema_version() -> i64 {
    SCHEMA_VERSION
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hours_wrap_past_midnight() {
        let window = parse_hours("22:00-06:00").unwrap();
        let at = |hour| Local {
            year: 2026,
            month: 1,
            day: 1,
            hour,
            minute: 0,
            second: 0,
            micro: 0,
            offset_secs: 0,
        };
        assert!(inside_hours(window, &at(23)));
        assert!(inside_hours(window, &at(5)));
        assert!(!inside_hours(window, &at(6)));
        assert!(!inside_hours(window, &at(12)));
        assert!(!inside_hours(None, &at(12)));
    }

    #[test]
    fn bad_hours_are_a_config_error() {
        assert!(parse_hours("01:00").is_err());
        assert!(parse_hours("01:00-01:00").is_err());
        assert!(parse_hours("1:00-7:00").is_err());
        assert_eq!(parse_hours("").unwrap(), None);
    }

    #[test]
    fn migrating_builds_the_chain_and_queue_keeps_its_place() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("frames.db");
        let (was, now) = migrate(&path).unwrap();
        assert_eq!((was, now), (0, SCHEMA_VERSION));
        assert_eq!(migrate(&path).unwrap(), (SCHEMA_VERSION, SCHEMA_VERSION));
        let conn = open_frames(&path).unwrap();
        let at = Local::now();
        assert!(queue(&conn, "imdb:tt1", true, "cli", &at).unwrap());
        assert!(!queue(&conn, "imdb:tt1", true, "cli", &at).unwrap());
        assert!(queue(&conn, "imdb:tt2", false, "cli", &at).is_err());
    }
}
