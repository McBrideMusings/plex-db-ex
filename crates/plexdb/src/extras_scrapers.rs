//! Scrapers for extras (ADR-0030): executables that read a file's `source_url` and print
//! metadata.
//!
//! A scraper is a subfolder of `scrapers/` beside `extras.json`, holding an executable and a
//! `scraper.toml`:
//!
//! ```toml
//! match = ['^https://example\.com/']  # regexes tried on source_url; any one matches
//! run = "scrape.sh"                   # a file in the same folder
//! auto = false                        # run at scan time for new queued files; default false
//! timeout = 30                        # seconds per attempt; optional
//! retries = 2                         # extra attempts after a failure; optional
//! ```
//!
//! The executable gets the URL as `argv[1]` and prints one flat JSON object. The store keeps a
//! key that is a field the item's library declares, plus `tags`; any other key is reported and
//! dropped. A key the object omits changes nothing. Values are stored as proposals under the
//! scraper's name, beside a person's and a filename parser's, and never replace either. A
//! scraper fetches metadata only: nothing here downloads media.
//!
//! Running a scraper waits on the network, so [`due`] and [`execute`] hold no lock; only
//! [`apply`] writes, and it is the only part that needs the writer lock.

use std::collections::BTreeMap;
use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use plexdb_keywords::{RawKeyword, write_title_keywords};
use regex::Regex;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;

use crate::extras_decisions::{self as decisions, Decision, Entry, NAMESPACE, PERSON};
use crate::pyjson::Json;

/// The folder beside `extras.json` that holds one subfolder per scraper.
pub const DIR: &str = "scrapers";
const CONFIG: &str = "scraper.toml";
const DEFAULT_TIMEOUT: u64 = 30;
const DEFAULT_RETRIES: u32 = 2;
/// Most output bytes read from a scraper's stdout or stderr.
const MAX_OUTPUT: u64 = 1 << 20;

/// The scraper the build ships, copied into [`DIR`] the first time it is missing.
const SHIPPED: [(&str, &str, bool); 2] = [
    (
        "youtube/scraper.toml",
        include_str!("../scrapers/youtube/scraper.toml"),
        false,
    ),
    (
        "youtube/youtube.py",
        include_str!("../scrapers/youtube/youtube.py"),
        true,
    ),
];

/// A scraper as its folder declares it.
#[derive(Debug, Clone)]
pub struct Scraper {
    pub name: String,
    dir: PathBuf,
    pub patterns: Vec<String>,
    matches: Vec<Regex>,
    pub run: String,
    pub auto: bool,
    pub timeout: Duration,
    pub retries: u32,
}

/// A subfolder that cannot run, and why.
#[derive(Debug, Clone)]
pub struct Broken {
    pub name: String,
    pub why: String,
}

impl Scraper {
    pub fn handles(&self, url: &str) -> bool {
        self.matches.iter().any(|re| re.is_match(url))
    }

    fn executable(&self) -> PathBuf {
        self.dir.join(&self.run)
    }
}

/// Copy the shipped scraper into `dir` when `dir` does not exist yet. A person who deletes
/// or edits it keeps their change: the folder is never written again once it exists.
pub fn seed(dir: &Path) -> std::io::Result<bool> {
    if dir.exists() {
        return Ok(false);
    }
    for (path, body, executable) in SHIPPED {
        let target = dir.join(path);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&target, body)?;
        if executable {
            std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o755))?;
        }
    }
    Ok(true)
}

/// Sources a scraper's folder name may not take: they are whose rows a scraper must not touch.
const RESERVED: [&str; 3] = [PERSON, "path", "keywords"];

fn load_one(name: &str, dir: &Path) -> Result<Scraper, String> {
    if RESERVED.contains(&name) {
        return Err(format!(
            "the folder name {name:?} is a source a scraper may not write as"
        ));
    }
    let text = std::fs::read_to_string(dir.join(CONFIG))
        .map_err(|err| format!("cannot read {CONFIG}: {err}"))?;
    let table: toml::Table = text
        .parse()
        .map_err(|err| format!("{CONFIG} is not valid TOML: {err}"))?;
    let patterns: Vec<String> = match table.get("match") {
        Some(toml::Value::Array(list)) => list
            .iter()
            .map(|item| item.as_str().map(str::to_string))
            .collect::<Option<_>>()
            .ok_or("match must be a list of text")?,
        _ => return Err("match must be a list of regexes".into()),
    };
    if patterns.is_empty() {
        return Err("match is empty".into());
    }
    let matches = patterns
        .iter()
        .map(|pattern| Regex::new(pattern).map_err(|err| format!("match {pattern:?}: {err}")))
        .collect::<Result<Vec<_>, _>>()?;
    let run = table
        .get("run")
        .and_then(toml::Value::as_str)
        .ok_or("run must be the name of an executable in the folder")?
        .to_string();
    if run.is_empty() || run.contains('/') || run.starts_with('.') {
        return Err(format!("run {run:?} must be a file name inside the folder"));
    }
    let auto = match table.get("auto") {
        None => false,
        Some(value) => value.as_bool().ok_or("auto must be true or false")?,
    };
    let number = |key: &str, default: u64| -> Result<u64, String> {
        match table.get(key) {
            None => Ok(default),
            Some(value) => value
                .as_integer()
                .and_then(|n| u64::try_from(n).ok())
                .ok_or_else(|| format!("{key} must be a whole number")),
        }
    };
    let timeout = number("timeout", DEFAULT_TIMEOUT)?;
    if timeout == 0 {
        return Err("timeout must be at least 1 second".into());
    }
    let retries = u32::try_from(number("retries", u64::from(DEFAULT_RETRIES))?)
        .map_err(|_| "retries is too large".to_string())?;
    Ok(Scraper {
        name: name.to_string(),
        dir: dir.to_path_buf(),
        patterns,
        matches,
        run,
        auto,
        timeout: Duration::from_secs(timeout),
        retries,
    })
}

/// Every scraper under `dir` by name, and the folders that cannot run. A missing `dir` is no
/// scrapers.
pub fn discover(dir: &Path) -> (Vec<Scraper>, Vec<Broken>) {
    let mut found = Vec::new();
    let mut broken = Vec::new();
    let Ok(read) = std::fs::read_dir(dir) else {
        return (found, broken);
    };
    let mut folders: Vec<(String, PathBuf)> = read
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            (!name.starts_with('.')).then(|| (name, entry.path()))
        })
        .collect();
    folders.sort();
    for (name, path) in folders {
        match load_one(&name, &path) {
            Ok(scraper) => found.push(scraper),
            Err(why) => broken.push(Broken { name, why }),
        }
    }
    (found, broken)
}

/// One attempt: the object the scraper printed, or why it produced none.
fn attempt(scraper: &Scraper, url: &str) -> Result<serde_json::Map<String, Value>, String> {
    let mut child = Command::new(scraper.executable())
        .arg(url)
        .current_dir(&scraper.dir)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("LANG", "C.UTF-8")
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("cannot start {}: {err}", scraper.run))?;
    fn drain(stream: impl Read + Send + 'static) -> std::thread::JoinHandle<Vec<u8>> {
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stream.take(MAX_OUTPUT).read_to_end(&mut bytes);
            bytes
        })
    }
    let out = drain(child.stdout.take().expect("piped stdout"));
    let err = drain(child.stderr.take().expect("piped stderr"));
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                // A grandchild still holding the pipes would block the readers below.
                // SAFETY: kill(2) on the process group this function started.
                unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL) };
                break Ok(status);
            }
            Ok(None) if started.elapsed() >= scraper.timeout => {
                // The whole group: a shell scraper's children hold the pipes open too.
                // SAFETY: kill(2) on the process group this function started.
                unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL) };
                let _ = child.wait();
                break Err(format!("timed out after {} s", scraper.timeout.as_secs()));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(25)),
            Err(why) => break Err(format!("cannot wait for {}: {why}", scraper.run)),
        }
    };
    let out = out.join().unwrap_or_default();
    let err = String::from_utf8_lossy(&err.join().unwrap_or_default())
        .trim()
        .to_string();
    let status = status?;
    if !status.success() {
        let last = err.lines().last().unwrap_or("no message");
        return Err(format!("exited {status}: {last}"));
    }
    match serde_json::from_slice::<Value>(&out) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err("printed JSON that is not one object".into()),
        Err(why) => Err(format!("printed text that is not JSON: {why}")),
    }
}

/// A scraper's run on one item: how many attempts it took and what came of them.
#[derive(Debug)]
pub struct Done {
    pub item_id: String,
    pub scraper: String,
    pub url: String,
    pub attempts: u32,
    /// The `decided_at` of the fetch decision this run answered, if it did.
    pub answered: Option<String>,
    pub result: Result<serde_json::Map<String, Value>, String>,
}

/// Run `scraper` on `url`, trying again after a failure up to its retry limit.
pub fn run(scraper: &Scraper, item_id: &str, url: &str, answered: Option<String>) -> Done {
    let mut attempts = 0;
    let result = loop {
        attempts += 1;
        match attempt(scraper, url) {
            Ok(map) => break Ok(map),
            Err(why) if attempts > scraper.retries => break Err(why),
            Err(_) => std::thread::sleep(Duration::from_secs(u64::from(attempts))),
        }
    };
    Done {
        item_id: item_id.into(),
        scraper: scraper.name.clone(),
        url: url.into(),
        attempts,
        answered,
        result,
    }
}

/// One scraper to run on one item.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Job {
    pub item_id: String,
    pub scraper: String,
    pub url: String,
    /// The `decided_at` of the fetch decision this run answers; `None` for an auto run.
    pub answered: Option<String>,
}

/// The last run of `scraper` on `item_id`: whether there is one, and the fetch it answered.
fn last_run(
    conn: &Connection,
    item_id: &str,
    scraper: &str,
) -> rusqlite::Result<Option<(String, Option<String>)>> {
    conn.query_row(
        "SELECT url, answered FROM extra_scrape WHERE item_id = ? AND scraper = ?",
        [item_id, scraper],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .optional()
}

/// The runs owed: every `auto` scraper whose regex matches a queued item's `source_url` and
/// has not run on that URL (a failed run counts as run: only a fetch repeats it), and every matching scraper for an item a `fetch` decision names that has
/// not answered that decision.
pub fn due(
    conn: &Connection,
    scrapers: &[Scraper],
    entries: &[Entry],
) -> rusqlite::Result<Vec<Job>> {
    let mut jobs: BTreeMap<(String, String), Job> = BTreeMap::new();
    let mut add = |item_id: &str, scraper: &Scraper, url: &str, answered: Option<&str>| {
        jobs.insert(
            (item_id.to_string(), scraper.name.clone()),
            Job {
                item_id: item_id.into(),
                scraper: scraper.name.clone(),
                url: url.into(),
                answered: answered.map(str::to_string),
            },
        );
    };
    let queued: Vec<(String, String)> = {
        let mut stmt = conn.prepare(
            "SELECT item_id, source_url FROM extra_item \
             WHERE organized = 0 AND source_url IS NOT NULL ORDER BY item_id",
        )?;
        stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?
    };
    for (item_id, url) in &queued {
        for scraper in scrapers.iter().filter(|s| s.auto && s.handles(url)) {
            if last_run(conn, item_id, &scraper.name)?.is_none_or(|(ran_on, _)| ran_on != *url) {
                add(item_id, scraper, url, None);
            }
        }
    }
    for entry in entries {
        let Decision::Fetch { item_id } = &entry.decision else {
            continue;
        };
        let url: Option<Option<String>> = conn
            .query_row(
                "SELECT source_url FROM extra_item WHERE item_id = ?",
                [item_id],
                |row| row.get(0),
            )
            .optional()?;
        let Some(Some(url)) = url else { continue };
        for scraper in scrapers.iter().filter(|s| s.handles(&url)) {
            let answered =
                last_run(conn, item_id, &scraper.name)?.and_then(|(_, answered)| answered);
            if answered.as_deref() != Some(entry.decided_at.as_str()) {
                add(item_id, scraper, &url, Some(&entry.decided_at));
            }
        }
    }
    Ok(jobs.into_values().collect())
}

/// Run each job. Holds no lock; a scraper waits on the network.
pub fn execute(scrapers: &[Scraper], jobs: &[Job]) -> Vec<Done> {
    jobs.iter()
        .filter_map(|job| {
            let scraper = scrapers.iter().find(|s| s.name == job.scraper)?;
            Some(run(scraper, &job.item_id, &job.url, job.answered.clone()))
        })
        .collect()
}

/// What [`apply`] did, for its report.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Stats {
    pub succeeded: usize,
    pub failed: usize,
    /// Items that no longer exist or whose `source_url` changed while the scraper ran.
    pub stale: usize,
    pub fields: usize,
    pub tags: usize,
    /// `scraper: key` for every key the library does not declare, dropped.
    pub ignored: Vec<String>,
    /// `item scraper: why` for every failed run.
    pub failures: Vec<String>,
}

fn as_values(key: &str, value: &Value, kind: &str) -> Result<Vec<String>, String> {
    let scalar = |value: &Value| match value {
        Value::String(text) => Ok(text.clone()),
        Value::Number(number) => Ok(number.to_string()),
        other => Err(format!("{key} is {other}, not text or a number")),
    };
    let parts: Vec<String> = match value {
        Value::Null => return Ok(Vec::new()),
        Value::Array(list) => list.iter().map(scalar).collect::<Result<_, _>>()?,
        other => vec![scalar(other)?],
    };
    if kind == "list" {
        Ok(parts
            .iter()
            .flat_map(|part| part.split(';'))
            .map(str::trim)
            .filter(|part| !part.is_empty())
            .map(str::to_string)
            .collect())
    } else if parts.len() == 1 {
        decisions::field_values(kind, key, &parts[0])
    } else {
        Err(format!("{key} is a {kind}, which takes one value"))
    }
}

/// Record each run and store what the successful ones proposed. Replaces only the rows this
/// scraper wrote for a key it printed; rows from a person or a filename parser stay.
pub fn apply(conn: &mut Connection, done: &[Done], now: &str) -> rusqlite::Result<Stats> {
    let mut stats = Stats::default();
    for run in done {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: Option<(String, Option<String>)> = tx
            .query_row(
                "SELECT library, source_url FROM extra_item WHERE item_id = ?",
                [&run.item_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((library, Some(url))) = current else {
            stats.stale += 1;
            continue;
        };
        if url != run.url {
            stats.stale += 1;
            continue;
        }
        let mut error: Option<String> = None;
        if let Ok(map) = &run.result {
            let declared: BTreeMap<String, String> = {
                let mut stmt =
                    tx.prepare("SELECT name, kind FROM extra_field WHERE library = ?")?;
                stmt.query_map([&library], |row| Ok((row.get(0)?, row.get(1)?)))?
                    .collect::<Result<_, _>>()?
            };
            for (key, value) in map {
                if key == "tags" {
                    match as_values(key, value, "list") {
                        Ok(tags) if tags.is_empty() => {}
                        Ok(tags) => {
                            stats.tags += tags.len();
                            write_title_keywords(
                                &tx,
                                &run.item_id,
                                &run.scraper,
                                tags.into_iter().map(RawKeyword::plain),
                                now,
                            )?;
                        }
                        Err(why) => error = Some(why),
                    }
                    continue;
                }
                let Some(kind) = declared.get(key) else {
                    stats.ignored.push(format!("{}: {key}", run.scraper));
                    continue;
                };
                let values = match as_values(key, value, kind) {
                    Ok(values) => values,
                    Err(why) => {
                        error = Some(why);
                        continue;
                    }
                };
                if values.is_empty() {
                    continue;
                }
                tx.execute(
                    "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? \
                     AND key = ? AND source = ?",
                    params![run.item_id, NAMESPACE, key, run.scraper],
                )?;
                for (rank, value) in values.iter().enumerate() {
                    tx.execute(
                        "INSERT OR REPLACE INTO enrichment \
                         (item_id, namespace, source, key, value, fetched_at, rank) \
                         VALUES (?, ?, ?, ?, ?, ?, ?)",
                        params![
                            run.item_id,
                            NAMESPACE,
                            run.scraper,
                            key,
                            value,
                            now,
                            rank as i64
                        ],
                    )?;
                }
                stats.fields += 1;
            }
        }
        let failure = run.result.as_ref().err().cloned().or(error);
        let ok = failure.is_none();
        match &failure {
            None => stats.succeeded += 1,
            Some(why) => {
                stats.failed += 1;
                stats
                    .failures
                    .push(format!("{} {}: {why}", run.item_id, run.scraper));
            }
        }
        tx.execute(
            "INSERT OR REPLACE INTO extra_scrape \
             (item_id, scraper, url, ran_at, ok, attempts, error, answered) \
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                run.item_id,
                run.scraper,
                run.url,
                now,
                ok as i64,
                run.attempts,
                failure,
                run.answered
            ],
        )?;
        tx.commit()?;
    }
    Ok(stats)
}

pub fn describe(stats: &Stats, ran: usize) -> Vec<String> {
    let mut lines = vec![format!(
        "extras scrapers: {ran} run(s), {} succeeded ({} field(s), {} tag(s) proposed), {} failed, \
         {} no longer current",
        stats.succeeded, stats.fields, stats.tags, stats.failed, stats.stale
    )];
    lines.extend(stats.failures.iter().map(|why| format!("  failed: {why}")));
    lines.extend(
        stats
            .ignored
            .iter()
            .map(|key| format!("  ignored, not a declared field: {key}")),
    );
    lines
}

/// Each scraper with its regexes, its auto setting and its last result, for the settings
/// page. A folder that cannot run is listed with why.
pub fn listing(conn: &Connection, dir: &Path) -> rusqlite::Result<Json> {
    let (scrapers, broken) = discover(dir);
    let mut out = Vec::new();
    for scraper in &scrapers {
        let last: Option<(String, String, i64, Option<String>)> = conn
            .query_row(
                "SELECT ran_at, item_id, ok, error FROM extra_scrape WHERE scraper = ? \
                 ORDER BY ran_at DESC LIMIT 1",
                [&scraper.name],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()?;
        let (ok, failed): (i64, i64) = conn.query_row(
            "SELECT COALESCE(SUM(ok), 0), COALESCE(SUM(1 - ok), 0) FROM extra_scrape \
             WHERE scraper = ?",
            [&scraper.name],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        let executable = std::fs::metadata(scraper.executable())
            .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
        out.push(Json::object([
            ("name", Json::str(&scraper.name)),
            (
                "match",
                Json::Array(scraper.patterns.iter().map(Json::str).collect()),
            ),
            ("run", Json::str(&scraper.run)),
            ("auto", Json::Bool(scraper.auto)),
            ("executable", Json::Bool(executable)),
            ("runs_ok", Json::Int(ok)),
            ("runs_failed", Json::Int(failed)),
            (
                "last",
                match last {
                    None => Json::Null,
                    Some((ran_at, item_id, ok, error)) => Json::object([
                        ("ran_at", Json::Str(ran_at)),
                        ("item_id", Json::Str(item_id)),
                        ("ok", Json::Bool(ok == 1)),
                        ("error", Json::opt_str(error)),
                    ]),
                },
            ),
            ("problem", Json::Null),
        ]));
    }
    for folder in &broken {
        out.push(Json::object([
            ("name", Json::str(&folder.name)),
            ("match", Json::Array(Vec::new())),
            ("run", Json::Null),
            ("auto", Json::Bool(false)),
            ("executable", Json::Bool(false)),
            ("runs_ok", Json::Int(0)),
            ("runs_failed", Json::Int(0)),
            ("last", Json::Null),
            ("problem", Json::str(&folder.why)),
        ]));
    }
    Ok(Json::object([("scrapers", Json::Array(out))]))
}
