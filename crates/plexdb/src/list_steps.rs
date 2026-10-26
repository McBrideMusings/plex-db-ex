//! The crowd-list commands: the three harvests (`harvest-mdblist`,
//! `harvest-letterboxd-lists`, `harvest-mdblist-search`) and the three that read or
//! decide lists (`lists`, `list`, `list-decide`). Each is the Python command it replaces
//! (`plexdb/commands/`), down to the lines it prints.

use plexdb_store::{Config, open_readonly, open_store};
use serde_json::Value;

use crate::args::{self, Parsed, Spec, int_option};
use crate::collections::{
    DEFAULT_STALE_DAYS, LETTERBOXD_SOURCE, MDBLIST_SOURCE, refresh_mdblist, report_line,
    wipe_source,
};
use crate::letterboxd::LiveLbClient;
use crate::letterboxd_lists::refresh_letterboxd_lists;
use crate::list_decisions::LIST_DECISIONS_FILE;
use crate::list_file::{DecideError, ListFile, decide};
use crate::list_search::{
    self, DEFAULT_MAX_LISTS, DEFAULT_MAX_SEEDS, DEFAULT_MIN_TITLES, Options, refresh_list_search,
    wipe_searched,
};
use crate::lists::{Obj, list_detail, list_table};
use crate::mdblist::LiveMdbClient;
use crate::steps::Failure;

pub const HARVEST_MDBLIST: &str = "harvest-mdblist";
pub const HARVEST_LETTERBOXD_LISTS: &str = "harvest-letterboxd-lists";
pub const HARVEST_MDBLIST_SEARCH: &str = "harvest-mdblist-search";
pub const LISTS: &str = "lists";
pub const LIST: &str = "list";
pub const LIST_DECIDE: &str = "list-decide";

fn failed(err: impl std::fmt::Display) -> Failure {
    Failure::Failed(err.to_string())
}

fn parse(args: &[String], spec: &Spec) -> Result<Parsed, Failure> {
    args::parse(args, spec).map_err(Failure::Usage)
}

/// `sources.env_int`: the variable's whole number, else the default for an unset or
/// empty one.
fn env_int(var: &str, default: i64, what: &str) -> Result<i64, Failure> {
    let raw = std::env::var(var).unwrap_or_default();
    let raw = plexdb_store::py_strip(&raw);
    if raw.is_empty() {
        return Ok(default);
    }
    crate::pyval::py_int_text(raw).ok_or_else(|| {
        Failure::Config(format!(
            "{var} must be {what}, got {}",
            crate::decisions::py_repr(raw)
        ))
    })
}

const HARVEST_SPEC: Spec = Spec {
    options: &["--stale-days"],
    flags: &["--rewipe"],
    positionals: 0,
};

const SEARCH_SPEC: Spec = Spec {
    options: &["--stale-days", "--limit"],
    flags: &["--rewipe"],
    positionals: 0,
};

/// `GatedSource.resolve_stale_days`: the flag, then `<NAME>_STALE_DAYS`, then the default.
fn stale_days(parsed: &Parsed, name: &str) -> Result<i64, Failure> {
    if let Some(days) = int_option(parsed, "--stale-days").map_err(Failure::Usage)? {
        return Ok(days);
    }
    env_int(
        &format!("{}_STALE_DAYS", name.to_uppercase()),
        DEFAULT_STALE_DAYS,
        "a whole number of days",
    )
}

fn api_key(purpose: &str) -> Result<String, Failure> {
    let key = std::env::var("MDBLIST_API_KEY").unwrap_or_default();
    let key = plexdb_store::py_strip(&key);
    if key.is_empty() {
        return Err(Failure::Config(format!(
            "MDBLIST_API_KEY must be set in .env to {purpose}"
        )));
    }
    Ok(key.to_string())
}

/// `plexdb harvest-mdblist [--stale-days N] [--rewipe]`.
pub fn harvest_mdblist(args: &[String]) -> Result<(), Failure> {
    let parsed = parse(args, &HARVEST_SPEC)?;
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let mut client = LiveMdbClient::new(&api_key("harvest MDBList collections")?);
    let stale = stale_days(&parsed, "mdblist")?;
    let mut store = open_store(&config.store_path).map_err(crate::steps::failure)?;
    if parsed.flag("--rewipe") {
        let (memberships, entries, collections) =
            wipe_source(&mut store.conn, MDBLIST_SOURCE).map_err(failed)?;
        println!(
            "wiped {collections} collection(s), {entries} entry(s) and {memberships} \
             membership(s) from MDBList"
        );
    }
    let stats = refresh_mdblist(&mut store.conn, &mut client, stale).map_err(failed)?;
    println!("{}", report_line("mdblist", &stats));
    Ok(())
}

/// `plexdb harvest-letterboxd-lists [--stale-days N] [--rewipe]`.
pub fn harvest_letterboxd_lists(args: &[String]) -> Result<(), Failure> {
    let parsed = parse(args, &HARVEST_SPEC)?;
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let mut client = LiveLbClient::new();
    let stale = stale_days(&parsed, "letterboxd_lists")?;
    let mut store = open_store(&config.store_path).map_err(crate::steps::failure)?;
    if parsed.flag("--rewipe") {
        let (memberships, entries, collections) =
            wipe_source(&mut store.conn, LETTERBOXD_SOURCE).map_err(failed)?;
        println!(
            "wiped {collections} collection(s), {entries} entry(s) and {memberships} \
             membership(s) from Letterboxd lists"
        );
    }
    let stats = refresh_letterboxd_lists(&mut store.conn, &mut client, stale).map_err(failed)?;
    println!("{}", report_line("letterboxd lists", &stats));
    Ok(())
}

/// `plexdb harvest-mdblist-search [--stale-days N] [--limit N] [--rewipe]`.
pub fn harvest_mdblist_search(args: &[String]) -> Result<(), Failure> {
    let parsed = parse(args, &SEARCH_SPEC)?;
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let mut client = LiveMdbClient::new(&api_key("search MDBList for lists")?);
    let stale = stale_days(&parsed, "mdblist_search")?;
    let limit = match int_option(&parsed, "--limit").map_err(Failure::Usage)? {
        Some(limit) => limit,
        None => env_int(
            "MDBLIST_SEARCH_MAX_SEEDS",
            DEFAULT_MAX_SEEDS,
            "a whole number of seeds",
        )?,
    };
    if limit < 0 {
        return Err(Failure::Config(format!(
            "the mdblist_search limit cannot be negative, got {limit}"
        )));
    }
    let mut store = open_store(&config.store_path).map_err(crate::steps::failure)?;
    if parsed.flag("--rewipe") {
        let (lists, cursors) = wipe_searched(&mut store.conn).map_err(failed)?;
        println!("wiped {lists} searched list(s) and {cursors} seed cursor(s)");
    }
    let options = Options {
        stale_days: stale,
        limit,
        min_titles: env_int(
            "MDBLIST_SEARCH_MIN_TITLES",
            DEFAULT_MIN_TITLES,
            "a whole number of library titles",
        )?,
        max_lists: env_int(
            "MDBLIST_SEARCH_MAX_LISTS",
            DEFAULT_MAX_LISTS,
            "a whole number of lists",
        )?,
    };
    let stats = refresh_list_search(&mut store.conn, &mut client, options).map_err(failed)?;
    println!("{}", list_search::report_line(&stats));
    Ok(())
}

/// `1,234`, as Python's `f"{n:,}"`.
fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (index, c) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// Python's `str(value)` of a cell the lists give: `None` for null.
fn show(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.into(),
        Some(other) => other.to_string(),
    }
}

fn int(value: Option<&Value>) -> i64 {
    value.and_then(Value::as_i64).unwrap_or(0)
}

/// `value or fallback` for a text cell.
fn text_or(value: Option<&Value>, fallback: &str) -> String {
    match value.and_then(Value::as_str) {
        Some(text) if !text.is_empty() => text.to_string(),
        _ => fallback.to_string(),
    }
}

/// A count cell or `?` where the source reported none.
fn count_or_question(value: Option<&Value>) -> String {
    match value.and_then(Value::as_i64) {
        Some(n) => thousands(n),
        None => "?".into(),
    }
}

/// `json.dumps(value, indent=2, ensure_ascii=False)`.
fn dump(value: &Value) -> String {
    serde_json::to_string_pretty(value).unwrap_or_default()
}

fn open_config_store() -> Result<(Config, rusqlite::Connection), Failure> {
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let conn = open_readonly(&config.store_path).map_err(crate::steps::failure)?;
    Ok((config, conn))
}

/// `plexdb lists [--source S] [--json]`.
pub fn lists(args: &[String]) -> Result<(), Failure> {
    let parsed = parse(
        args,
        &Spec {
            options: &["--source"],
            flags: &["--json"],
            positionals: 0,
        },
    )?;
    let (_config, conn) = open_config_store()?;
    let source = parsed.option("--source");
    let rows = list_table(&conn, source).map_err(failed)?;
    if parsed.flag("--json") {
        println!(
            "{}",
            dump(&Value::Array(rows.into_iter().map(Value::Object).collect()))
        );
        return Ok(());
    }
    if rows.is_empty() {
        match source.filter(|s| !s.is_empty()) {
            Some(source) => println!("no lists stored for source {source}"),
            None => println!("no lists stored"),
        }
        return Ok(());
    }
    for row in &rows {
        println!(
            "{}  {}  [{}, by {}]  owned {}/{} (size {})  watched {}  likes {}  fetched {}",
            show(row.get("collection_id")),
            text_or(row.get("name"), "(unnamed)"),
            show(row.get("provenance")),
            text_or(row.get("author"), "?"),
            thousands(int(row.get("owned"))),
            thousands(int(row.get("entry_count"))),
            count_or_question(row.get("size")),
            thousands(int(row.get("watched"))),
            count_or_question(row.get("likes")),
            show(row.get("fetched_at")),
        );
    }
    Ok(())
}

fn print_detail(detail: &Obj) {
    println!(
        "{}  {}",
        show(detail.get("collection_id")),
        text_or(detail.get("name"), "(unnamed)")
    );
    println!(
        "  {}  by {}",
        text_or(detail.get("url"), "(no url)"),
        text_or(detail.get("author"), "?")
    );
    println!(
        "  {}, {}; owned {}/{}, size {}, watched {}, likes {}, fetched {}",
        show(detail.get("source")),
        show(detail.get("provenance")),
        thousands(int(detail.get("owned"))),
        thousands(int(detail.get("entry_count"))),
        show(detail.get("size")),
        thousands(int(detail.get("watched"))),
        show(detail.get("likes")),
        show(detail.get("fetched_at")),
    );
    let keywords = detail.get("keywords").and_then(Value::as_array);
    if let Some(keywords) = keywords.filter(|k| !k.is_empty()) {
        println!(
            "  keywords over-represented among {} owned {} titles (library: {}):",
            thousands(int(detail.get("keyword_titles_in_list"))),
            show(detail.get("keyword_kind")),
            thousands(int(detail.get("keyword_titles_in_library"))),
        );
        for row in keywords {
            println!(
                "    {}: {} in list, {} in library, x{:.1}",
                show(row.get("keyword")),
                show(row.get("in_list")),
                thousands(int(row.get("in_library"))),
                row.get("lift").and_then(Value::as_f64).unwrap_or(0.0),
            );
        }
    }
    for entry in detail
        .get("entries")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let rank = match entry.get("rank").and_then(Value::as_i64) {
            Some(rank) => rank.to_string(),
            None => "-".into(),
        };
        let year = match entry.get("year").and_then(Value::as_i64) {
            Some(year) if year != 0 => format!(" ({year})"),
            _ => String::new(),
        };
        let owned = entry.get("owned").and_then(Value::as_bool).unwrap_or(false);
        let watched = entry
            .get("watched")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mark = match (owned, watched) {
            (true, true) => "watched",
            (true, false) => "owned",
            (false, _) => "not owned",
        };
        println!(
            "  {rank:>5}  {}{year}  [{mark}]",
            text_or(entry.get("title"), "(untitled)")
        );
    }
}

/// `plexdb list COLLECTION_ID_OR_URL [--json]`.
pub fn list_one(args: &[String]) -> Result<(), Failure> {
    let parsed = parse(
        args,
        &Spec {
            options: &[],
            flags: &["--json"],
            positionals: 1,
        },
    )?;
    let (_config, conn) = open_config_store()?;
    let reference = &parsed.positionals[0];
    let Some(detail) = list_detail(&conn, reference).map_err(failed)? else {
        return Err(Failure::Failed(format!(
            "no stored list has the id or URL {} — see `plexdb lists`",
            crate::decisions::py_repr(reference)
        )));
    };
    if parsed.flag("--json") {
        println!("{}", dump(&Value::Object(detail)));
    } else {
        print_detail(&detail);
    }
    Ok(())
}

/// `plexdb list-decide URL DECISION`.
pub fn list_decide(args: &[String]) -> Result<(), Failure> {
    let parsed = parse(
        args,
        &Spec {
            options: &[],
            flags: &[],
            positionals: 2,
        },
    )?;
    let decision = &parsed.positionals[1];
    if !crate::list_file::DECISIONS.contains(&decision.as_str()) {
        let choices: Vec<String> = crate::list_file::DECISIONS
            .iter()
            .map(|d| crate::decisions::py_repr(d))
            .collect();
        return Err(Failure::Usage(format!(
            "argument decision: invalid choice: {} (choose from {})",
            crate::decisions::py_repr(decision),
            choices.join(", ")
        )));
    }
    let config = Config::from_env().map_err(crate::steps::failure)?;
    let file = ListFile::new(config.decisions_file(LIST_DECISIONS_FILE));
    let conn = open_readonly(&config.store_path).map_err(crate::steps::failure)?;
    let entry = match decide(&conn, &file, &parsed.positionals[0], decision) {
        Ok(entry) => entry,
        Err(DecideError::Unknown(url)) => {
            return Err(Failure::Failed(format!(
                "no stored or decided list has the URL {url} — see `plexdb lists`"
            )));
        }
        Err(other) => return Err(failed(other)),
    };
    println!(
        "{} {} -> {} (applied at the next sweep)",
        entry.get("decision").and_then(Value::as_str).unwrap_or(""),
        entry.get("url").and_then(Value::as_str).unwrap_or(""),
        file.path().display()
    );
    Ok(())
}
