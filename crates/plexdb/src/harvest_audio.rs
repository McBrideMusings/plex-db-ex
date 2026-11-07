//! `plexdb harvest-audio` — audio facts per file, computed once per file hash
//! (plex-db-ex-9ak.1).
//!
//! For every media part of the libraries named in `PLEXDB_AUDIO_SECTIONS` it finds the
//! file under `PLEXDB_MEDIA_DIR`, hashes it with the frame harvester's
//! [`file_hash`](crate::frame_media::file_hash) and, when `audio_facts` has no row for that
//! hash, runs the analyzer (`tools/audio-analyze.py`, installed as `plexdb-audio-analyze`)
//! and stores the JSON object it prints. Facts are keyed by hash, so the same content under
//! two parts, or at a new path, is analyzed once; `audio_part` says which content each part
//! has. The run's bookkeeping is `audio_harvest_state`, one row per part (ADR-0013).
//!
//! **Outcomes, one per part.** `done`: facts stored. `failed`: the analyzer exited non-zero
//! or printed something that is not a JSON object; kept while the part's size is unchanged.
//! `unreachable`: the file could not be stat'ed or read; retried after an hour, doubling to
//! a day. `excluded`: no duration, or longer than [`MAX_DURATION_MS`] (the analyzer decodes
//! the whole track into memory); decided without opening the file.
//!
//! **Skipping without hashing.** A part whose state row carries the same size and a
//! terminal outcome (`done`, `failed`, `excluded`) is skipped before the file is touched, so
//! a steady-state run costs one SQL query. The cost is that a file overwritten with the same
//! size is not noticed: the re-hash happens when Plex's part size changes. A `failed` part
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
use std::fs::{File, OpenOptions, TryLockError};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use plexdb_store::{Config, StoreError, open_readonly, open_store};
use rusqlite::{Connection, OptionalExtension, params, params_from_iter};
use serde_json::Value;

use crate::frame_media::{file_hash, local_path};
use crate::frames::FrameConfig;
use crate::staleness::{Now, parse_micros};
use crate::steps::{Failure, failure};

pub const NAME: &str = "harvest-audio";

pub const SECTIONS_VAR: &str = "PLEXDB_AUDIO_SECTIONS";
pub const ANALYZER_VAR: &str = "PLEXDB_AUDIO_ANALYZER";
pub const DEFAULT_ANALYZER: &str = "plexdb-audio-analyze";

/// A part longer than this is `excluded`: the analyzer decodes the whole track to memory.
pub const MAX_DURATION_MS: i64 = 1_800_000;
const RETRY_BASE_SECS: i64 = 3600;
const RETRY_CAP_SECS: i64 = 86_400;
const STDERR_TAIL_CHARS: usize = 300;

pub const LOCK_FILE: &str = "audio-harvest.lock";

/// What a part's state row says happened to it.
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

/// The settings a run reads from the environment.
#[derive(Debug, Clone)]
pub struct Settings {
    pub sections: Vec<String>,
    pub analyzer: String,
    pub media_dir: PathBuf,
    pub source_roots: Vec<String>,
}

/// `PLEXDB_AUDIO_SECTIONS`: comma-separated section ids; empty or unset means off.
pub fn sections_from_env() -> Vec<String> {
    parse_sections(&std::env::var(SECTIONS_VAR).unwrap_or_default())
}

pub fn parse_sections(raw: &str) -> Vec<String> {
    let mut sections: Vec<String> = Vec::new();
    for part in raw.split(',') {
        let id = part.trim();
        if !id.is_empty() && !sections.iter().any(|seen| seen == id) {
            sections.push(id.to_string());
        }
    }
    sections
}

impl Settings {
    pub fn from_env() -> Result<Settings, StoreError> {
        let sections = sections_from_env();
        if sections.is_empty() {
            return Err(StoreError::Config(format!(
                "{SECTIONS_VAR} is not set, so no library is analyzed; name the Plex section \
                 ids to analyze, comma-separated"
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
        Ok(Settings {
            sections,
            analyzer,
            media_dir,
            source_roots: frames.source_roots,
        })
    }
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

/// Something that turns one audio file into a JSON object of facts.
pub trait Analyzer {
    fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError>;
}

/// The analyzer program, run as `<program> <file>`: one JSON object on stdout and exit 0.
/// It runs idle: SCHED_IDLE, the idle I/O class and nice 19 on Linux (nice 19 elsewhere),
/// one math-library thread, so a decode never takes CPU or disk from Plex or the sweep.
pub struct ProcessAnalyzer {
    pub program: String,
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

impl Analyzer for ProcessAnalyzer {
    fn analyze(&self, path: &Path) -> Result<Value, AnalyzeError> {
        let mut command = Command::new(&self.program);
        command
            .arg(path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for var in SINGLE_THREAD_VARS {
            command.env(var, "1");
        }
        // SAFETY: `lower_priority` only makes async-signal-safe syscalls.
        unsafe {
            command.pre_exec(|| {
                lower_priority();
                Ok(())
            });
        }
        let output = command.output().map_err(|err| {
            AnalyzeError::Unavailable(format!("cannot run {}: {err}", self.program))
        })?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let tail = tail_chars(stderr.trim(), STDERR_TAIL_CHARS);
            let how = match output.status.code() {
                Some(code) => format!("exited {code}"),
                None => "was killed by a signal".to_string(),
            };
            return Err(AnalyzeError::Failed(if tail.is_empty() {
                format!("{} {how}", self.program)
            } else {
                format!("{} {how}: {tail}", self.program)
            }));
        }
        let value: Value = serde_json::from_slice(&output.stdout).map_err(|err| {
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
}

fn tail_chars(text: &str, keep: usize) -> String {
    let count = text.chars().count();
    if count <= keep {
        return text.to_string();
    }
    text.chars().skip(count - keep).collect()
}

// --- candidates and state -------------------------------------------------------------

/// One media part of a configured section.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub part_id: i64,
    pub item_id: String,
    pub section_id: String,
    pub file: String,
    pub size: Option<i64>,
    pub duration_ms: Option<i64>,
}

/// A part's `audio_harvest_state` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct State {
    pub size: Option<i64>,
    pub file_hash: Option<String>,
    pub outcome: Outcome,
    pub attempts: i64,
    pub next_try_at: Option<String>,
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(",")
}

pub fn candidates(conn: &Connection, sections: &[String]) -> rusqlite::Result<Vec<Candidate>> {
    if sections.is_empty() {
        return Ok(Vec::new());
    }
    let sql = format!(
        "SELECT mp.part_id, mp.item_id, pi.section_id, mp.file, mp.size, mp.duration_ms \
         FROM media_part mp JOIN plex_items pi ON pi.rating_key = mp.rating_key \
         WHERE pi.section_id IN ({}) ORDER BY mp.part_id",
        placeholders(sections.len())
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params_from_iter(sections), |row| {
        Ok(Candidate {
            part_id: row.get(0)?,
            item_id: row.get(1)?,
            section_id: row.get(2)?,
            file: row.get(3)?,
            size: row.get(4)?,
            duration_ms: row.get(5)?,
        })
    })?;
    rows.collect()
}

pub fn states(conn: &Connection) -> rusqlite::Result<HashMap<i64, State>> {
    let mut stmt = conn.prepare(
        "SELECT part_id, size, file_hash, outcome, attempts, next_try_at FROM audio_harvest_state",
    )?;
    let rows = stmt.query_map([], |row| {
        let outcome: String = row.get(3)?;
        Ok((
            row.get::<_, i64>(0)?,
            State {
                size: row.get(1)?,
                file_hash: row.get(2)?,
                outcome: Outcome::parse(&outcome).unwrap_or(Outcome::Failed),
                attempts: row.get(4)?,
                next_try_at: row.get(5)?,
            },
        ))
    })?;
    rows.collect()
}

/// What a run does with a part, decided from its row and the clock alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disposition {
    /// Nothing to do; the state row's outcome stands.
    Settled(Outcome),
    /// Record `excluded` without opening the file.
    Exclude,
    /// Work on it now. `retry` is true for a part tried before (a due `unreachable`).
    Work { retry: bool },
}

/// `now_micros` is the clock in microseconds since the epoch.
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
    if cand.duration_ms.is_none_or(|ms| ms > MAX_DURATION_MS) {
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
    /// Parts given the facts of a hash already analyzed, with no analysis.
    pub unchanged: usize,
    pub failed: usize,
    pub unreachable: usize,
    pub excluded: usize,
    /// Set when the media root failed stat and the run stopped there.
    pub blocked: Option<String>,
}

impl Report {
    pub fn describe(&self) -> String {
        let mut line = format!(
            "harvest-audio: {} part(s): {} analyzed, {} unchanged hash skipped, {} failed, \
             {} unreachable, {} excluded, {} already settled",
            self.candidates,
            self.analyzed,
            self.unchanged,
            self.failed,
            self.unreachable,
            self.excluded,
            self.settled
        );
        if let Some(reason) = &self.blocked {
            line.push_str(&format!("; {reason}"));
        }
        line
    }
}

fn store_failure(err: rusqlite::Error) -> Failure {
    Failure::Failed(format!("store: {err}"))
}

/// One part's new state row, and the `audio_part` / `audio_facts` rows that go with it.
struct Write<'a> {
    cand: &'a Candidate,
    outcome: Outcome,
    file_hash: Option<&'a str>,
    error: Option<&'a str>,
    attempts: i64,
    next_try_at: Option<String>,
    /// The analyzer's object, to insert under `file_hash`.
    facts: Option<&'a Value>,
    /// Point `audio_part` at `file_hash` (done).
    link: bool,
}

/// Record a result in its own short transaction, holding the writer lock only for it.
fn record(store: &Path, write: &Write, now: &Now) -> Result<(), Failure> {
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
    if let (true, Some(hash)) = (write.link, write.file_hash) {
        tx.execute(
            "INSERT INTO audio_part (part_id, item_id, file_hash) VALUES (?1, ?2, ?3) \
             ON CONFLICT(part_id) DO UPDATE SET item_id = excluded.item_id, \
             file_hash = excluded.file_hash",
            params![write.cand.part_id, write.cand.item_id, hash],
        )
        .map_err(store_failure)?;
    }
    tx.execute(
        "INSERT INTO audio_harvest_state (part_id, item_id, size, file_hash, outcome, error, \
         attempts, next_try_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
         ON CONFLICT(part_id) DO UPDATE SET item_id = excluded.item_id, size = excluded.size, \
         file_hash = excluded.file_hash, outcome = excluded.outcome, error = excluded.error, \
         attempts = excluded.attempts, next_try_at = excluded.next_try_at, \
         updated_at = excluded.updated_at",
        params![
            write.cand.part_id,
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

fn media_root_error(root: &Path) -> Option<String> {
    std::fs::metadata(root)
        .err()
        .map(|err| format!("blocked: media root {} unreadable: {err}", root.display()))
}

/// Run the harvest once: analyze what has no facts yet, `clock` giving the seconds since
/// the epoch each time it is asked. Returns the report; a stopped run (`blocked`) is a
/// report with `blocked` set, an error is a [`Failure`].
pub fn harvest(
    store: &Path,
    settings: &Settings,
    analyzer: &dyn Analyzer,
    clock: &dyn Fn() -> i64,
    log: &mut dyn FnMut(&str),
) -> Result<Report, Failure> {
    let _lock = RunLock::acquire(store)?;
    let (found, known) = {
        let conn = open_readonly(store).map_err(failure)?;
        (
            candidates(&conn, &settings.sections).map_err(store_failure)?,
            states(&conn).map_err(store_failure)?,
        )
    };
    let mut report = Report {
        candidates: found.len(),
        ..Report::default()
    };

    // Never-tried parts first, then due retries; part_id order inside each.
    let start = Now::at_secs(clock());
    let mut fresh: Vec<&Candidate> = Vec::new();
    let mut retries: Vec<&Candidate> = Vec::new();
    for cand in &found {
        match classify(cand, known.get(&cand.part_id), start.secs() * 1_000_000) {
            Disposition::Settled(_) => report.settled += 1,
            Disposition::Exclude => {
                let now = Now::at_secs(clock());
                let why = match cand.duration_ms {
                    None => "duration unknown; not analyzed".to_string(),
                    Some(ms) => format!(
                        "{} min is longer than the {} min limit; not analyzed",
                        ms / 60_000,
                        MAX_DURATION_MS / 60_000
                    ),
                };
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
                log(&format!("part {} excluded: {why}", cand.part_id));
            }
            Disposition::Work { retry: false } => fresh.push(cand),
            Disposition::Work { retry: true } => retries.push(cand),
        }
    }

    for cand in fresh.into_iter().chain(retries) {
        let prior = known.get(&cand.part_id).cloned();
        if let Some(reason) = media_root_error(&settings.media_dir) {
            report.blocked = Some(reason);
            return Ok(report);
        }
        let now = Now::at_secs(clock());
        let hashed = local_path(&settings.media_dir, &cand.file, &settings.source_roots)
            .and_then(|path| file_hash(&path, cand.size.unwrap_or(0)).map(|hash| (path, hash)));
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
                    "part {} unreachable (attempt {attempts}, next try {retry_at}): {}",
                    cand.part_id, err.message
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
            log(&format!(
                "part {} unchanged hash {} skipped",
                cand.part_id,
                &hash[..12]
            ));
            continue;
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
                log(&format!("part {} done {}", cand.part_id, &hash[..12]));
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
                log(&format!("part {} failed: {message}", cand.part_id));
            }
            Err(AnalyzeError::Unavailable(message)) => return Err(Failure::Failed(message)),
        }
    }
    Ok(report)
}

// --- status ---------------------------------------------------------------------------

/// One section's parts by where they stand. `pending` is a part with no terminal state or
/// a due `unreachable`; `unreachable` is one still waiting for its retry.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Counts {
    pub done: usize,
    pub pending: usize,
    pub failed: usize,
    pub unreachable: usize,
    pub excluded: usize,
}

/// Counts per configured section, in the order the sections were named.
pub fn status(
    store: &Path,
    sections: &[String],
    now_secs: i64,
) -> Result<Vec<(String, Counts)>, StoreError> {
    let conn = open_readonly(store)?;
    let found = candidates(&conn, sections)?;
    let known = states(&conn)?;
    let mut by_section: Vec<(String, Counts)> = sections
        .iter()
        .map(|id| (id.clone(), Counts::default()))
        .collect();
    for cand in &found {
        let Some((_, counts)) = by_section.iter_mut().find(|(id, _)| *id == cand.section_id) else {
            continue;
        };
        match classify(cand, known.get(&cand.part_id), now_secs * 1_000_000) {
            Disposition::Settled(Outcome::Done) => counts.done += 1,
            Disposition::Settled(Outcome::Failed) => counts.failed += 1,
            Disposition::Settled(Outcome::Excluded) => counts.excluded += 1,
            Disposition::Settled(Outcome::Unreachable) => counts.unreachable += 1,
            Disposition::Exclude | Disposition::Work { .. } => counts.pending += 1,
        }
    }
    Ok(by_section)
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
    let analyzer = ProcessAnalyzer {
        program: settings.analyzer.clone(),
    };
    let report = harvest(
        &config.store_path,
        &settings,
        &analyzer,
        &now_secs,
        &mut |line| println!("{line}"),
    )?;
    println!("{}", report.describe());
    match report.blocked {
        Some(reason) => Err(Failure::Failed(reason)),
        None => Ok(()),
    }
}
