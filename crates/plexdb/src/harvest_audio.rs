//! `plexdb harvest-audio` — audio facts per file, computed once per file hash
//! (plex-db-ex-9ak.1, extras and library order plex-db-ex-9ak.5).
//!
//! `PLEXDB_AUDIO_LIBRARIES` names, in order, the libraries to work through: each is a Plex
//! section **title** or an `extra_library.name` ([`crate::libraries`]). The run takes the
//! libraries one at a time in list order; inside a library, parts never tried come before
//! due retries (Plex parts by `part_id`, extras by `extra_item.first_seen, item_id`). For
//! every file it finds the file under `PLEXDB_MEDIA_DIR`, hashes it with the frame
//! harvester's [`file_hash`](crate::frame_media::file_hash) and, when `audio_facts` has no
//! row for that hash, runs the analyzer (`tools/audio-analyze.py`, installed as
//! `plexdb-audio-analyze`) and stores the JSON object it prints. Facts are keyed by hash,
//! so the same content under two files, or at a new path, is analyzed once. A Plex part's
//! content is recorded in `audio_part` and its bookkeeping in `audio_harvest_state`, keyed
//! by `part_id`; an extra's (its primary `extra_file` row that is not missing) in
//! `audio_extra` and `audio_extra_state`, keyed by `extra_file.path` with a cascading
//! foreign key, so extras-organize renaming the path keeps them (ADR-0013). A [`Key`] says
//! which of the two a file is; one loop serves both.
//!
//! **Outcomes, one per file.** `done`: facts stored. `failed`: the analyzer (or, for an
//! extra, the duration probe) exited non-zero or printed something unusable; kept while the
//! file's size is unchanged. `unreachable`: the file could not be stat'ed or read; retried
//! after an hour, doubling to a day. `excluded`: no duration, or longer than
//! [`MAX_DURATION_MS`] (the analyzer decodes the whole track into memory). A Plex part's
//! duration is known to the store, so it is decided without opening the file. Nothing fills
//! `extra_file.duration_ms`, so an extra with none is probed with ffprobe
//! (`PLEXDB_FFPROBE`) once it is reachable; the probe never writes the column.
//!
//! **Names that match nothing.** A listed name that is neither a section title nor an extras
//! library is reported (in the run's report line and in `plexdb audio status`) and skipped;
//! the rest still run. Section titles come from one Plex sections call at run start.
//!
//! **Skipping without hashing.** A file whose state row carries the same size and a
//! terminal outcome (`done`, `failed`, `excluded`) is skipped before the file is touched, so
//! a steady-state run costs one SQL query. The cost is that a file overwritten with the same
//! size is not noticed: the re-hash happens when the recorded size changes. A `failed` file
//! is retried when its hash changes, which only a size change can bring about here.
//!
//! **Blocked.** Before each file, and whenever a file is unreachable, the media root is
//! stat'ed. If the root itself fails, the run stops with `blocked: media root … unreadable`
//! and the file in hand is not marked unreachable: an unmounted share is not a missing file.
//!
//! **Locks.** Analysis holds no store lock. Each result is written in its own short
//! transaction through [`open_store`], so the sweep is never held up by a decode. A run
//! lock beside the store (`audio-harvest.lock`) stops two runs overlapping.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs::{File, OpenOptions, TryLockError};
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use plexdb_store::{Config, StoreError, open_readonly, open_store};
use rusqlite::types::Value as SqlValue;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;

use crate::frame_media::{file_hash, local_path};
use crate::frames::FrameConfig;
use crate::libraries::{
    Library, LibraryKind, Resolved, extra_library_names, names_from_env, resolve, unknown_note,
};
use crate::plex::{PlexSource, Section};
use crate::sources::Sources;
use crate::staleness::{Now, parse_micros};
use crate::steps::{Failure, failure};

pub const NAME: &str = "harvest-audio";

pub const LIBRARIES_VAR: &str = "PLEXDB_AUDIO_LIBRARIES";
pub const ANALYZER_VAR: &str = "PLEXDB_AUDIO_ANALYZER";
pub const DEFAULT_ANALYZER: &str = "plexdb-audio-analyze";
pub const TIMEOUT_VAR: &str = "PLEXDB_AUDIO_TIMEOUT";
/// Seconds one analysis may run before it is killed and recorded `failed`.
pub const DEFAULT_TIMEOUT_SECS: u64 = 900;
/// Seconds one duration probe may run before it is killed and recorded `failed`.
pub const PROBE_TIMEOUT_SECS: u64 = 60;

/// A file longer than this is `excluded`: the analyzer decodes the whole track to memory.
pub const MAX_DURATION_MS: i64 = 1_800_000;
const RETRY_BASE_SECS: i64 = 3600;
const RETRY_CAP_SECS: i64 = 86_400;
const STDERR_TAIL_CHARS: usize = 300;

/// Said of a configured Plex section that has no `media_part` rows: media-walk covers only
/// movie and show libraries, so a music or photo section looks like a healthy empty one.
pub const EMPTY_SECTION_NOTE: &str = "no media_part rows — only movie and show sections are walked";
/// Said of an extras library with no file the harvest can read.
pub const EMPTY_EXTRAS_NOTE: &str = "no extra_file rows with a present primary file";

pub const LOCK_FILE: &str = "audio-harvest.lock";

/// What a file's state row says happened to it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Done,
    Failed,
    Unreachable,
    Excluded,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Done => "done",
            Outcome::Failed => "failed",
            Outcome::Unreachable => "unreachable",
            Outcome::Excluded => "excluded",
        }
    }

    fn parse(text: &str) -> Option<Outcome> {
        Some(match text {
            "done" => Outcome::Done,
            "failed" => Outcome::Failed,
            "unreachable" => Outcome::Unreachable,
            "excluded" => Outcome::Excluded,
            _ => return None,
        })
    }
}

/// What identifies a file in the harvest's own tables: a Plex media part, or an extra's
/// `extra_file.path`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Key {
    Part(i64),
    Extra(String),
}

impl Key {
    /// `(content table, state table, key column)`.
    fn tables(&self) -> (&'static str, &'static str, &'static str) {
        match self {
            Key::Part(_) => ("audio_part", "audio_harvest_state", "part_id"),
            Key::Extra(_) => ("audio_extra", "audio_extra_state", "path"),
        }
    }

    fn value(&self) -> SqlValue {
        match self {
            Key::Part(id) => SqlValue::Integer(*id),
            Key::Extra(path) => SqlValue::Text(path.clone()),
        }
    }

    pub fn label(&self) -> String {
        match self {
            Key::Part(id) => format!("part {id}"),
            Key::Extra(path) => format!("extra {path}"),
        }
    }

    fn is_extra(&self) -> bool {
        matches!(self, Key::Extra(_))
    }
}

/// The settings a run reads from the environment.
#[derive(Debug, Clone)]
pub struct Settings {
    /// `PLEXDB_AUDIO_LIBRARIES`, in list order.
    pub libraries: Vec<String>,
    pub analyzer: String,
    pub ffprobe: String,
    pub timeout: Duration,
    pub media_dir: PathBuf,
    pub source_roots: Vec<String>,
}

impl Settings {
    pub fn from_env() -> Result<Settings, StoreError> {
        let libraries = names_from_env(LIBRARIES_VAR);
        if libraries.is_empty() {
            return Err(StoreError::Config(format!(
                "{LIBRARIES_VAR} is not set, so no library is analyzed; name the Plex section \
                 titles and extras libraries to analyze, comma-separated, in the order to work"
            )));
        }
        let frames = FrameConfig::from_env()?;
        let media_dir = frames.media_dir.ok_or_else(|| {
            StoreError::Config(
                "PLEXDB_MEDIA_DIR is not set, so no audio file can be read; mount the library \
                 read-only and name the directory Plex calls PLEX_SOURCE_ROOTS"
                    .into(),
            )
        })?;
        if frames.source_roots.is_empty() {
            return Err(StoreError::Config(
                "PLEX_SOURCE_ROOTS is not set, so Plex's file paths cannot be mapped".into(),
            ));
        }
        let analyzer = std::env::var(ANALYZER_VAR)
            .ok()
            .map(|raw| raw.trim().to_string())
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| DEFAULT_ANALYZER.to_string());
        let timeout = match std::env::var(TIMEOUT_VAR)
            .ok()
            .map(|raw| raw.trim().to_string())
        {
            Some(raw) if !raw.is_empty() => match raw.parse::<u64>() {
                Ok(secs) if secs > 0 => Duration::from_secs(secs),
                _ => {
                    return Err(StoreError::Config(format!(
                        "{TIMEOUT_VAR} must be a whole number of seconds above 0, got {raw:?}"
                    )));
                }
            },
            _ => Duration::from_secs(DEFAULT_TIMEOUT_SECS),
        };
        Ok(Settings {
            libraries,
            analyzer,
            ffprobe: frames.ffprobe,
            timeout,
            media_dir,
            source_roots: frames.source_roots,
        })
    }
}

/// Resolve `names` against Plex's `sections` and the store's extras libraries.
pub fn resolve_libraries(
    conn: &Connection,
    names: &[String],
    sections: &[Section],
) -> rusqlite::Result<Resolved> {
    Ok(resolve(names, sections, &extra_library_names(conn)?))
}

// --- the analyzer ---------------------------------------------------------------------

/// Why an analysis did not produce facts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AnalyzeError {
    /// The analyzer ran and rejected this file: recorded as `failed`.
    Failed(String),
    /// The analyzer could not be started at all: the run stops, nothing is recorded.
    Unavailable(String),
}

/// Something that turns one audio file into a JSON object of facts, and can say how long a
/// file plays.
pub trait Analyzer {
    fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError>;

    /// The file's playing time in milliseconds, or `None` when it has no duration. Asked
    /// only for an extra whose `extra_file.duration_ms` is NULL.
    fn duration_ms(&self, path: &Path) -> Result<Option<i64>, AnalyzeError>;
}

/// The analyzer program, run as `<program> <file>`: one JSON object on stdout and exit 0.
/// It runs idle: SCHED_IDLE, the idle I/O class and nice 19 on Linux (nice 19 elsewhere),
/// one math-library thread, so a decode never takes CPU or disk from Plex or the sweep.
pub struct ProcessAnalyzer {
    pub program: String,
    /// The program that probes a duration (`PLEXDB_FFPROBE`), run as `<program> … <file>`.
    pub ffprobe: String,
    /// A run past this is killed, with everything it started, and is `failed`.
    pub timeout: Duration,
}

const SINGLE_THREAD_VARS: [&str; 4] = [
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
];

/// Runs in the child between fork and exec. Failures are ignored: a container without the
/// right to change its scheduling class still analyzes, just not politely.
fn lower_priority() {
    // SAFETY: plain syscalls on the calling (child) process with valid arguments.
    unsafe {
        #[cfg(target_os = "linux")]
        {
            let param = libc::sched_param { sched_priority: 0 };
            libc::sched_setscheduler(0, libc::SCHED_IDLE, &param);
            // ioprio_set(IOPRIO_WHO_PROCESS = 1, this process, IOPRIO_CLASS_IDLE = 3 << 13)
            libc::syscall(
                libc::SYS_ioprio_set,
                1 as libc::c_long,
                0 as libc::c_long,
                (3 << 13) as libc::c_long,
            );
        }
        libc::setpriority(libc::PRIO_PROCESS, 0, 19);
    }
}

/// Run `program args…` idle, to the end or `timeout`, and return its stdout. A run past the
/// timeout is killed with everything it started and is `Failed`, as is a non-zero exit;
/// a program that cannot be started is `Unavailable`. `what` names it in a timeout message.
fn run_idle(
    program: &str,
    args: &[&OsStr],
    timeout: Duration,
    what: &str,
) -> Result<Vec<u8>, AnalyzeError> {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for var in SINGLE_THREAD_VARS {
        command.env(var, "1");
    }
    // SAFETY: `lower_priority` and `setpgid` only make async-signal-safe syscalls. The
    // child leads its own process group so a timeout can kill what it started too.
    unsafe {
        command.pre_exec(|| {
            libc::setpgid(0, 0);
            lower_priority();
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .map_err(|err| AnalyzeError::Unavailable(format!("cannot run {program}: {err}")))?;
    let drain = |pipe: Option<Box<dyn Read + Send>>| {
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            if let Some(mut pipe) = pipe {
                let _ = pipe.read_to_end(&mut bytes);
            }
            bytes
        })
    };
    let out_reader = drain(
        child
            .stdout
            .take()
            .map(|p| Box::new(p) as Box<dyn Read + Send>),
    );
    let err_reader = drain(
        child
            .stderr
            .take()
            .map(|p| Box::new(p) as Box<dyn Read + Send>),
    );
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() >= deadline => break None,
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(err) => {
                kill_group(&mut child);
                return Err(AnalyzeError::Unavailable(format!(
                    "cannot wait for {program}: {err}"
                )));
            }
        }
    };
    let Some(status) = status else {
        kill_group(&mut child);
        let _ = out_reader.join();
        let _ = err_reader.join();
        return Err(AnalyzeError::Failed(format!(
            "{what} timed out after {}s",
            timeout.as_secs()
        )));
    };
    let output = std::process::Output {
        status,
        stdout: out_reader.join().unwrap_or_default(),
        stderr: err_reader.join().unwrap_or_default(),
    };
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let tail = tail_chars(stderr.trim(), STDERR_TAIL_CHARS);
        let how = match (output.status.code(), output.status.signal()) {
            (Some(code), _) => format!("exited {code}"),
            (None, Some(signal)) => format!("was killed by signal {signal}"),
            (None, None) => "was killed".to_string(),
        };
        return Err(AnalyzeError::Failed(if tail.is_empty() {
            format!("{program} {how}")
        } else {
            format!("{program} {how}: {tail}")
        }));
    }
    Ok(output.stdout)
}

impl Analyzer for ProcessAnalyzer {
    fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError> {
        let stdout = run_idle(&self.program, &[path.as_os_str()], self.timeout, "analyzer")?;
        let value: Value = serde_json::from_slice(&stdout).map_err(|err| {
            AnalyzeError::Failed(format!("{} printed no JSON: {err}", self.program))
        })?;
        if !value.is_object() {
            return Err(AnalyzeError::Failed(format!(
                "{} printed JSON that is not an object",
                self.program
            )));
        }
        Ok(value)
    }

    fn duration_ms(&self, path: &Path) -> Result<Option<i64>, AnalyzeError> {
        let args: [&OsStr; 7] = [
            OsStr::new("-v"),
            OsStr::new("error"),
            OsStr::new("-show_entries"),
            OsStr::new("format=duration"),
            OsStr::new("-of"),
            OsStr::new("default=noprint_wrappers=1:nokey=1"),
            path.as_os_str(),
        ];
        let stdout = run_idle(
            &self.ffprobe,
            &args,
            Duration::from_secs(PROBE_TIMEOUT_SECS),
            "ffprobe",
        )?;
        parse_probe_duration(&String::from_utf8_lossy(&stdout))
            .map_err(|text| AnalyzeError::Failed(format!("{} printed {text:?}", self.ffprobe)))
    }
}

/// ffprobe's `format=duration` in seconds as milliseconds; `N/A` or nothing is no duration.
/// An `Err` carries the text that is not a number.
pub fn parse_probe_duration(text: &str) -> Result<Option<i64>, String> {
    let text = text.trim();
    if text.is_empty() || text == "N/A" {
        return Ok(None);
    }
    match text.parse::<f64>() {
        Ok(secs) if secs.is_finite() && secs > 0.0 => Ok(Some((secs * 1000.0).round() as i64)),
        Ok(_) => Ok(None),
        Err(_) => Err(text.to_string()),
    }
}

/// Kill the analyzer's whole process group (a shell or Python wrapper's children hold the
/// output pipes open) and reap it.
fn kill_group(child: &mut std::process::Child) {
    // SAFETY: signalling the group the child made for itself in `pre_exec`.
    unsafe {
        libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn tail_chars(text: &str, keep: usize) -> String {
    let count = text.chars().count();
    if count <= keep {
        return text.to_string();
    }
    text.chars().skip(count - keep).collect()
}

// --- candidates and state -------------------------------------------------------------

/// One file of a configured library: a Plex media part or an extra's primary file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub key: Key,
    pub item_id: String,
    /// Index into the resolved libraries, which is the order the run works in.
    pub library: usize,
    /// The path as Plex (or `extra_file.path`) names it; mapped with `local_path`.
    pub file: String,
    pub size: Option<i64>,
    pub duration_ms: Option<i64>,
}

/// A file's `audio_harvest_state` / `audio_extra_state` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct State {
    pub size: Option<i64>,
    pub file_hash: Option<String>,
    pub outcome: Outcome,
    pub attempts: i64,
    pub next_try_at: Option<String>,
}

/// Every file of `libraries`, library by library in the order given: a section's media
/// parts by `part_id`, an extras library's primary files by `first_seen, item_id`.
pub fn candidates(conn: &Connection, libraries: &[Library]) -> rusqlite::Result<Vec<Candidate>> {
    let mut out = Vec::new();
    for (ix, library) in libraries.iter().enumerate() {
        match &library.kind {
            LibraryKind::Section(section_id) => {
                let mut stmt = conn.prepare(
                    "SELECT mp.part_id, mp.item_id, mp.file, mp.size, mp.duration_ms \
                     FROM media_part mp JOIN plex_items pi ON pi.rating_key = mp.rating_key \
                     WHERE pi.section_id = ?1 ORDER BY mp.part_id",
                )?;
                let rows = stmt.query_map([section_id], |row| {
                    Ok(Candidate {
                        key: Key::Part(row.get(0)?),
                        item_id: row.get(1)?,
                        library: ix,
                        file: row.get(2)?,
                        size: row.get(3)?,
                        duration_ms: row.get(4)?,
                    })
                })?;
                for row in rows {
                    out.push(row?);
                }
            }
            LibraryKind::Extras => {
                let mut stmt = conn.prepare(
                    "SELECT ef.path, ei.item_id, ef.size, ef.duration_ms \
                     FROM extra_item ei \
                     JOIN extra_file ef ON ef.item_id = ei.item_id AND ef.primary_file = 1 \
                     WHERE ei.library = ?1 AND ef.missing_since IS NULL \
                     ORDER BY ei.first_seen, ei.item_id",
                )?;
                let rows = stmt.query_map([&library.name], |row| {
                    let path: String = row.get(0)?;
                    Ok(Candidate {
                        key: Key::Extra(path.clone()),
                        item_id: row.get(1)?,
                        library: ix,
                        file: path,
                        size: row.get(2)?,
                        duration_ms: row.get(3)?,
                    })
                })?;
                for row in rows {
                    out.push(row?);
                }
            }
        }
    }
    Ok(out)
}

pub fn states(conn: &Connection) -> rusqlite::Result<HashMap<Key, State>> {
    let mut out = HashMap::new();
    load_states(conn, &Key::Part(0), &mut out)?;
    load_states(conn, &Key::Extra(String::new()), &mut out)?;
    Ok(out)
}

/// The state rows of `kind`'s table, keyed as `kind` keys them.
fn load_states(
    conn: &Connection,
    kind: &Key,
    out: &mut HashMap<Key, State>,
) -> rusqlite::Result<()> {
    let (_, table, column) = kind.tables();
    let mut stmt = conn.prepare(&format!(
        "SELECT {column}, size, file_hash, outcome, attempts, next_try_at FROM {table}"
    ))?;
    let rows = stmt.query_map([], |row| {
        let key = match kind {
            Key::Part(_) => Key::Part(row.get(0)?),
            Key::Extra(_) => Key::Extra(row.get(0)?),
        };
        let outcome: String = row.get(3)?;
        Ok((
            key,
            State {
                size: row.get(1)?,
                file_hash: row.get(2)?,
                outcome: Outcome::parse(&outcome).unwrap_or(Outcome::Failed),
                attempts: row.get(4)?,
                next_try_at: row.get(5)?,
            },
        ))
    })?;
    for row in rows {
        let (key, state) = row?;
        out.insert(key, state);
    }
    Ok(())
}

/// What a run does with a file, decided from its row and the clock alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disposition {
    /// Nothing to do; the state row's outcome stands.
    Settled(Outcome),
    /// Record `excluded` without opening the file.
    Exclude,
    /// Work on it now. `retry` is true for a file tried before (a due `unreachable`).
    Work { retry: bool },
}

/// `now_micros` is the clock in microseconds since the epoch. An extra with no duration is
/// not excluded here: the run probes it once the file is reachable.
pub fn classify(cand: &Candidate, state: Option<&State>, now_micros: i64) -> Disposition {
    if let Some(state) = state
        && state.size == cand.size
        && matches!(
            state.outcome,
            Outcome::Done | Outcome::Failed | Outcome::Excluded
        )
    {
        return Disposition::Settled(state.outcome);
    }
    let probeable = cand.key.is_extra() && cand.duration_ms.is_none();
    if !probeable && cand.duration_ms.is_none_or(|ms| ms > MAX_DURATION_MS) {
        return Disposition::Exclude;
    }
    match state {
        Some(state) if state.outcome == Outcome::Unreachable => {
            let due = state
                .next_try_at
                .as_deref()
                .and_then(parse_micros)
                .is_none_or(|at| at <= now_micros);
            if due {
                Disposition::Work { retry: true }
            } else {
                Disposition::Settled(Outcome::Unreachable)
            }
        }
        _ => Disposition::Work { retry: false },
    }
}

/// Why a duration is not analyzed.
fn exclusion(duration_ms: Option<i64>) -> String {
    match duration_ms {
        None => "duration unknown; not analyzed".to_string(),
        Some(ms) => format!(
            "{} min is longer than the {} min limit; not analyzed",
            ms / 60_000,
            MAX_DURATION_MS / 60_000
        ),
    }
}

/// Seconds to wait after the `attempts`-th failed attempt: an hour, doubling, at most a day.
pub fn backoff_secs(attempts: i64) -> i64 {
    let doublings = (attempts.max(1) - 1).min(16) as u32;
    (RETRY_BASE_SECS << doublings).min(RETRY_CAP_SECS)
}

// --- the run lock ---------------------------------------------------------------------

fn lock_path(store: &Path) -> PathBuf {
    store.with_file_name(LOCK_FILE)
}

/// Held for the life of a run, so two runs never analyze at once. The operating system
/// drops it when the process exits however it exits.
#[derive(Debug)]
pub struct RunLock {
    _file: File,
}

impl RunLock {
    pub fn acquire(store: &Path) -> Result<RunLock, Failure> {
        let path = lock_path(store);
        let file = OpenOptions::new()
            .append(true)
            .create(true)
            .open(&path)
            .map_err(|err| Failure::Failed(format!("cannot open {}: {err}", path.display())))?;
        match file.try_lock() {
            Ok(()) => Ok(RunLock { _file: file }),
            Err(TryLockError::WouldBlock) => {
                Err(Failure::Busy("another audio harvest is running".into()))
            }
            Err(TryLockError::Error(err)) => Err(Failure::Failed(format!(
                "cannot lock {}: {err}",
                path.display()
            ))),
        }
    }
}

/// Whether a run holds the lock now; a missing lock file means none has ever run.
pub fn harvest_running(store: &Path) -> bool {
    let Ok(file) = File::open(lock_path(store)) else {
        return false;
    };
    match file.try_lock_shared() {
        Ok(()) => {
            let _ = file.unlock();
            false
        }
        Err(TryLockError::WouldBlock) => true,
        Err(TryLockError::Error(_)) => false,
    }
}

// --- the run --------------------------------------------------------------------------

/// What one run did.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Report {
    pub candidates: usize,
    pub settled: usize,
    pub analyzed: usize,
    /// Files given the facts of a hash already analyzed, with no analysis.
    pub unchanged: usize,
    pub failed: usize,
    pub unreachable: usize,
    pub excluded: usize,
    /// Set when the media root failed stat and the run stopped there.
    pub blocked: Option<String>,
    /// Resolved libraries with nothing to analyze, with what to say of each.
    pub empty_libraries: Vec<(String, &'static str)>,
    /// Listed names that are neither a Plex section title nor an extras library.
    pub unknown_libraries: Vec<String>,
    /// Why Plex's sections were unavailable; the names that needed them are in
    /// `plex_skipped`, not `unknown_libraries`.
    pub plex_error: Option<String>,
    pub plex_skipped: Vec<String>,
}

impl Report {
    pub fn describe(&self) -> String {
        let mut line = format!(
            "harvest-audio: {} file(s): {} analyzed, {} unchanged hash skipped, {} failed, \
             {} unreachable, {} excluded, {} already settled",
            self.candidates,
            self.analyzed,
            self.unchanged,
            self.failed,
            self.unreachable,
            self.excluded,
            self.settled
        );
        for (library, note) in &self.empty_libraries {
            line.push_str(&format!("; library {library}: {note}"));
        }
        for name in &self.unknown_libraries {
            line.push_str(&format!("; {}", unknown_note(name)));
        }
        if let Some(error) = &self.plex_error {
            line.push_str(&format!(
                "; plex sections unavailable: {error}, skipped: {}",
                self.plex_skipped.join(", ")
            ));
        }
        if let Some(reason) = &self.blocked {
            line.push_str(&format!("; {reason}"));
        }
        line
    }
}

fn store_failure(err: rusqlite::Error) -> Failure {
    Failure::Failed(format!("store: {err}"))
}

/// One file's new state row, and the content row / `audio_facts` row that go with it.
struct Write<'a> {
    cand: &'a Candidate,
    outcome: Outcome,
    file_hash: Option<&'a str>,
    error: Option<&'a str>,
    attempts: i64,
    next_try_at: Option<String>,
    /// The analyzer's object, to insert under `file_hash`.
    facts: Option<&'a Value>,
    /// Point the content row at `file_hash` (done).
    link: bool,
}

/// Record a result in its own short transaction, holding the writer lock only for it.
fn record(store: &Path, write: &Write, now: &Now) -> Result<(), Failure> {
    let (content, state, column) = write.cand.key.tables();
    let key = write.cand.key.value();
    let mut opened = open_store(store).map_err(failure)?;
    let tx = opened.conn.transaction().map_err(store_failure)?;
    let at = now.iso();
    if let (Some(hash), Some(facts)) = (write.file_hash, write.facts) {
        tx.execute(
            "INSERT INTO audio_facts (file_hash, facts, analyzed_at) VALUES (?1, ?2, ?3) \
             ON CONFLICT(file_hash) DO NOTHING",
            params![hash, facts.to_string(), at],
        )
        .map_err(store_failure)?;
    }
    if write.outcome != Outcome::Done {
        // The file no longer has the content its old row described.
        tx.execute(
            &format!("DELETE FROM {content} WHERE {column} = ?1"),
            [&key],
        )
        .map_err(store_failure)?;
    }
    if let (true, Some(hash)) = (write.link, write.file_hash) {
        tx.execute(
            &format!(
                "INSERT INTO {content} ({column}, item_id, file_hash) VALUES (?1, ?2, ?3) \
                 ON CONFLICT({column}) DO UPDATE SET item_id = excluded.item_id, \
                 file_hash = excluded.file_hash"
            ),
            params![key, write.cand.item_id, hash],
        )
        .map_err(store_failure)?;
    }
    tx.execute(
        &format!(
            "INSERT INTO {state} ({column}, item_id, size, file_hash, outcome, error, \
             attempts, next_try_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
             ON CONFLICT({column}) DO UPDATE SET item_id = excluded.item_id, \
             size = excluded.size, file_hash = excluded.file_hash, \
             outcome = excluded.outcome, error = excluded.error, \
             attempts = excluded.attempts, next_try_at = excluded.next_try_at, \
             updated_at = excluded.updated_at"
        ),
        params![
            key,
            write.cand.item_id,
            write.cand.size,
            write.file_hash,
            write.outcome.as_str(),
            write.error,
            write.attempts,
            write.next_try_at,
            at
        ],
    )
    .map_err(store_failure)?;
    tx.commit().map_err(store_failure)
}

fn facts_exist(conn: &Connection, hash: &str) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT 1 FROM audio_facts WHERE file_hash = ?1",
        [hash],
        |_| Ok(()),
    )
    .optional()
    .map(|found| found.is_some())
}

pub fn media_root_error(root: &Path) -> Option<String> {
    std::fs::metadata(root)
        .err()
        .map(|err| format!("blocked: media root {} unreadable: {err}", root.display()))
}

fn empty_note(kind: &LibraryKind) -> &'static str {
    match kind {
        LibraryKind::Section(_) => EMPTY_SECTION_NOTE,
        LibraryKind::Extras => EMPTY_EXTRAS_NOTE,
    }
}

/// Run the harvest once: analyze what has no facts yet, library by library in list order.
/// `sections` is Plex's section list, to turn titles into section ids; `clock` gives the
/// seconds since the epoch each time it is asked. Returns the report; a stopped run
/// (`blocked`) is a report with `blocked` set, an error is a [`Failure`].
pub fn harvest(
    store: &Path,
    settings: &Settings,
    sections: &[Section],
    analyzer: &dyn Analyzer,
    clock: &dyn Fn() -> i64,
    log: &mut dyn FnMut(&str),
) -> Result<Report, Failure> {
    harvest_with(store, settings, sections, None, analyzer, clock, log)
}

/// [`harvest`] with the error that made `sections` unavailable, if any: names that match
/// no extras library are then reported as skipped for want of Plex, not as unknown.
pub fn harvest_with(
    store: &Path,
    settings: &Settings,
    sections: &[Section],
    plex_error: Option<&str>,
    analyzer: &dyn Analyzer,
    clock: &dyn Fn() -> i64,
    log: &mut dyn FnMut(&str),
) -> Result<Report, Failure> {
    let _lock = RunLock::acquire(store)?;
    let (resolved, found, known) = {
        let conn = open_readonly(store).map_err(failure)?;
        let resolved =
            resolve_libraries(&conn, &settings.libraries, sections).map_err(store_failure)?;
        let found = candidates(&conn, &resolved.libraries).map_err(store_failure)?;
        (resolved, found, states(&conn).map_err(store_failure)?)
    };
    let mut report = Report {
        candidates: found.len(),
        empty_libraries: resolved
            .libraries
            .iter()
            .enumerate()
            .filter(|(ix, _)| !found.iter().any(|cand| cand.library == *ix))
            .map(|(_, library)| (library.name.clone(), empty_note(&library.kind)))
            .collect(),
        unknown_libraries: if plex_error.is_some() {
            Vec::new()
        } else {
            resolved.unknown.clone()
        },
        plex_error: plex_error.map(str::to_string),
        plex_skipped: if plex_error.is_some() {
            resolved.unknown.clone()
        } else {
            Vec::new()
        },
        ..Report::default()
    };
    if let Some(error) = plex_error {
        log(&format!("plex sections unavailable: {error}"));
    } else {
        for name in &resolved.unknown {
            log(&unknown_note(name));
        }
    }

    // Library by library in list order; never-tried files first, then due retries.
    let start = Now::at_secs(clock());
    let mut fresh: Vec<Vec<&Candidate>> = vec![Vec::new(); resolved.libraries.len()];
    let mut retries: Vec<Vec<&Candidate>> = vec![Vec::new(); resolved.libraries.len()];
    for cand in &found {
        match classify(cand, known.get(&cand.key), start.secs() * 1_000_000) {
            Disposition::Settled(_) => report.settled += 1,
            Disposition::Exclude => {
                let now = Now::at_secs(clock());
                let why = exclusion(cand.duration_ms);
                record(
                    store,
                    &Write {
                        cand,
                        outcome: Outcome::Excluded,
                        file_hash: None,
                        error: Some(&why),
                        attempts: 0,
                        next_try_at: None,
                        facts: None,
                        link: false,
                    },
                    &now,
                )?;
                report.excluded += 1;
                log(&format!("{} excluded: {why}", cand.key.label()));
            }
            Disposition::Work { retry: false } => fresh[cand.library].push(cand),
            Disposition::Work { retry: true } => retries[cand.library].push(cand),
        }
    }
    let order: Vec<&Candidate> = fresh
        .into_iter()
        .zip(retries)
        .flat_map(|(fresh, retries)| fresh.into_iter().chain(retries))
        .collect();

    for cand in order {
        let label = cand.key.label();
        let prior = known.get(&cand.key).cloned();
        if let Some(reason) = media_root_error(&settings.media_dir) {
            report.blocked = Some(reason);
            return Ok(report);
        }
        let now = Now::at_secs(clock());
        // A Plex part's path the settings cannot map is a configuration error, not a flaky
        // file: stop and record nothing for it. An extra's path is one file's problem: it
        // is `failed` (kept while its size holds) and later libraries still run.
        let path = match local_path(&settings.media_dir, &cand.file, &settings.source_roots) {
            Ok(path) => path,
            Err(err) if cand.key.is_extra() => {
                record(
                    store,
                    &Write {
                        cand,
                        outcome: Outcome::Failed,
                        file_hash: None,
                        error: Some(&err.message),
                        attempts: prior.as_ref().map_or(0, |state| state.attempts) + 1,
                        next_try_at: None,
                        facts: None,
                        link: false,
                    },
                    &now,
                )?;
                report.failed += 1;
                log(&format!("{label} failed: {}", err.message));
                continue;
            }
            Err(err) => {
                report.blocked = Some(format!("blocked: {}", err.message));
                return Ok(report);
            }
        };
        let hashed = file_hash(&path, cand.size.unwrap_or(0)).map(|hash| (path, hash));
        let (path, hash) = match hashed {
            Ok(found) => found,
            Err(err) => {
                // A mount that went away makes every file unreachable; that is the root's
                // fault, not this file's, so the file keeps its state and the run stops.
                if let Some(reason) = media_root_error(&settings.media_dir) {
                    report.blocked = Some(reason);
                    return Ok(report);
                }
                let attempts = prior
                    .as_ref()
                    .filter(|state| state.outcome == Outcome::Unreachable)
                    .map_or(0, |state| state.attempts)
                    + 1;
                let retry_at = Now::at_secs(now.secs() + backoff_secs(attempts)).iso();
                record(
                    store,
                    &Write {
                        cand,
                        outcome: Outcome::Unreachable,
                        file_hash: None,
                        error: Some(&err.message),
                        attempts,
                        next_try_at: Some(retry_at.clone()),
                        facts: None,
                        link: false,
                    },
                    &now,
                )?;
                report.unreachable += 1;
                log(&format!(
                    "{label} unreachable (attempt {attempts}, next try {retry_at}): {}",
                    err.message
                ));
                continue;
            }
        };

        if prior.as_ref().is_some_and(|state| {
            state.outcome == Outcome::Failed && state.file_hash.as_deref() == Some(hash.as_str())
        }) {
            report.settled += 1;
            continue;
        }

        let has_facts = {
            let conn = open_readonly(store).map_err(failure)?;
            facts_exist(&conn, &hash).map_err(store_failure)?
        };
        if has_facts {
            record(
                store,
                &Write {
                    cand,
                    outcome: Outcome::Done,
                    file_hash: Some(&hash),
                    error: None,
                    attempts: 0,
                    next_try_at: None,
                    facts: None,
                    link: true,
                },
                &now,
            )?;
            report.unchanged += 1;
            log(&format!("{label} unchanged hash {} skipped", &hash[..12]));
            continue;
        }

        // An extra with no recorded duration is probed now that its file is reachable.
        if cand.key.is_extra() && cand.duration_ms.is_none() {
            match analyzer.duration_ms(&path) {
                Ok(duration) if duration.is_some_and(|ms| ms <= MAX_DURATION_MS) => {}
                Ok(duration) => {
                    let now = Now::at_secs(clock());
                    let why = exclusion(duration);
                    record(
                        store,
                        &Write {
                            cand,
                            outcome: Outcome::Excluded,
                            file_hash: None,
                            error: Some(&why),
                            attempts: 0,
                            next_try_at: None,
                            facts: None,
                            link: false,
                        },
                        &now,
                    )?;
                    report.excluded += 1;
                    log(&format!("{label} excluded: {why}"));
                    continue;
                }
                Err(AnalyzeError::Failed(message)) => {
                    let now = Now::at_secs(clock());
                    let message = format!("duration probe: {message}");
                    record(
                        store,
                        &Write {
                            cand,
                            outcome: Outcome::Failed,
                            file_hash: Some(&hash),
                            error: Some(&message),
                            attempts: prior.as_ref().map_or(0, |state| state.attempts) + 1,
                            next_try_at: None,
                            facts: None,
                            link: false,
                        },
                        &now,
                    )?;
                    report.failed += 1;
                    log(&format!("{label} failed: {message}"));
                    continue;
                }
                Err(AnalyzeError::Unavailable(message)) => return Err(Failure::Failed(message)),
            }
        }

        match analyzer.analyze(&path) {
            Ok(facts) => {
                let now = Now::at_secs(clock());
                record(
                    store,
                    &Write {
                        cand,
                        outcome: Outcome::Done,
                        file_hash: Some(&hash),
                        error: None,
                        attempts: 1,
                        next_try_at: None,
                        facts: Some(&facts),
                        link: true,
                    },
                    &now,
                )?;
                report.analyzed += 1;
                log(&format!("{label} done {}", &hash[..12]));
            }
            Err(AnalyzeError::Failed(message)) => {
                let now = Now::at_secs(clock());
                record(
                    store,
                    &Write {
                        cand,
                        outcome: Outcome::Failed,
                        file_hash: Some(&hash),
                        error: Some(&message),
                        attempts: prior.as_ref().map_or(0, |state| state.attempts) + 1,
                        next_try_at: None,
                        facts: None,
                        link: false,
                    },
                    &now,
                )?;
                report.failed += 1;
                log(&format!("{label} failed: {message}"));
            }
            Err(AnalyzeError::Unavailable(message)) => return Err(Failure::Failed(message)),
        }
    }
    Ok(report)
}

// --- status ---------------------------------------------------------------------------

/// One library's files by where they stand. `pending` is a file with no terminal state or
/// a due `unreachable`; `unreachable` is one still waiting for its retry.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Counts {
    /// Every file of the library, whatever its state; 0 means nothing to analyze there.
    pub parts: usize,
    pub done: usize,
    pub pending: usize,
    pub failed: usize,
    pub unreachable: usize,
    pub excluded: usize,
}

/// Counts per resolved library, in list order.
pub fn status(
    store: &Path,
    resolved: &Resolved,
    now_secs: i64,
) -> Result<Vec<(Library, Counts)>, StoreError> {
    let conn = open_readonly(store)?;
    let found = candidates(&conn, &resolved.libraries)?;
    let known = states(&conn)?;
    let mut by_library: Vec<(Library, Counts)> = resolved
        .libraries
        .iter()
        .map(|library| (library.clone(), Counts::default()))
        .collect();
    for cand in &found {
        let counts = &mut by_library[cand.library].1;
        counts.parts += 1;
        match classify(cand, known.get(&cand.key), now_secs * 1_000_000) {
            Disposition::Settled(Outcome::Done) => counts.done += 1,
            Disposition::Settled(Outcome::Failed) => counts.failed += 1,
            Disposition::Settled(Outcome::Excluded) => counts.excluded += 1,
            Disposition::Settled(Outcome::Unreachable) => counts.unreachable += 1,
            Disposition::Exclude | Disposition::Work { .. } => counts.pending += 1,
        }
    }
    Ok(by_library)
}

// --- the command ----------------------------------------------------------------------

fn now_secs() -> i64 {
    Now::read().secs()
}

/// `plexdb harvest-audio`.
pub fn run(args: &[String]) -> Result<(), Failure> {
    if let Some(extra) = args.first() {
        return Err(Failure::Usage(format!(
            "{NAME} takes no arguments, got {extra}"
        )));
    }
    let settings = Settings::from_env().map_err(failure)?;
    if !crate::frames_cmd::which(&settings.analyzer) {
        return Err(Failure::Config(format!(
            "the audio analyzer {:?} is not on PATH; install it or set {ANALYZER_VAR}",
            settings.analyzer
        )));
    }
    let config = Config::from_env().map_err(failure)?;
    let (sections, plex_error) = match Sources::from_env()
        .plex("resolve the names in PLEXDB_AUDIO_LIBRARIES")
        .and_then(|plex| plex.sections())
    {
        Ok(sections) => (sections, None),
        Err(err) => (Vec::new(), Some(err.to_string())),
    };
    let resolved = {
        let conn = open_readonly(&config.store_path).map_err(failure)?;
        resolve_libraries(&conn, &settings.libraries, &sections).map_err(store_failure)?
    };
    if resolved
        .libraries
        .iter()
        .any(|library| library.kind == LibraryKind::Extras)
        && !crate::frames_cmd::which(&settings.ffprobe)
    {
        return Err(Failure::Config(format!(
            "{:?} is not on PATH; extras with no recorded duration are probed with it \
             (set PLEXDB_FFPROBE)",
            settings.ffprobe
        )));
    }
    let analyzer = ProcessAnalyzer {
        program: settings.analyzer.clone(),
        ffprobe: settings.ffprobe.clone(),
        timeout: settings.timeout,
    };
    let report = harvest_with(
        &config.store_path,
        &settings,
        &sections,
        plex_error.as_deref(),
        &analyzer,
        &now_secs,
        &mut |line| println!("{line}"),
    )?;
    println!("{}", report.describe());
    if let Some(reason) = report.blocked {
        return Err(Failure::Failed(reason));
    }
    // The extras ran; the failed Plex call still fails the run so the tick logs it.
    match report.plex_error {
        Some(error) => Err(Failure::Failed(format!(
            "plex sections unavailable: {error}"
        ))),
        None => Ok(()),
    }
}
