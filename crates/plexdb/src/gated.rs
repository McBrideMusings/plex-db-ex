//! What a Gated Source command is, and the steps every one of them takes
//! (`plexdb/sources.py`).
//!
//! A Gated Source is an external source whose units carry a `fetched_at` and are
//! re-fetched only once stale. Each reads `<NAME>_STALE_DAYS`, derived from its own
//! name, falling back to the one [`DEFAULT_STALE_DAYS`]; `--stale-days N` wins over
//! both. `--rewipe` deletes everything the source owns before the run, forcing a
//! full re-fetch.

use plexdb_store::{Config, StoreError, open_store, py_strip};

use crate::staleness::DEFAULT_STALE_DAYS;
use crate::steps::Failure;

/// A Gated Source command's flags.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Options {
    pub stale_days: Option<i64>,
    pub rewipe: bool,
    /// `--limit N`, for a source that caps what one run takes.
    pub limit: Option<i64>,
}

/// `[--stale-days N] [--rewipe]`; anything else is a usage error.
pub fn parse_options(name: &str, args: &[String]) -> Result<Options, Failure> {
    parse(name, args, false)
}

/// `[--stale-days N] [--limit N] [--rewipe]`, for a capped source.
pub fn parse_capped_options(name: &str, args: &[String]) -> Result<Options, Failure> {
    parse(name, args, true)
}

fn parse(name: &str, args: &[String], capped: bool) -> Result<Options, Failure> {
    let usage = |message: String| Failure::Usage(format!("{name}: {message}"));
    let int = |flag: &str, value: &str| -> Result<i64, Failure> {
        value
            .trim()
            .parse()
            .map_err(|_| usage(format!("argument {flag}: invalid int value: {value:?}")))
    };
    let mut options = Options::default();
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        if capped {
            let value = if arg == "--limit" {
                Some(
                    rest.next()
                        .ok_or_else(|| usage("argument --limit: expected one argument".into()))?
                        .as_str(),
                )
            } else {
                arg.strip_prefix("--limit=")
            };
            if let Some(value) = value {
                options.limit = Some(int("--limit", value)?);
                continue;
            }
        }
        let value = if arg == "--stale-days" {
            Some(
                rest.next()
                    .ok_or_else(|| usage("argument --stale-days: expected one argument".into()))?
                    .as_str(),
            )
        } else {
            arg.strip_prefix("--stale-days=")
        };
        if let Some(value) = value {
            options.stale_days = Some(int("--stale-days", value)?);
        } else if arg == "--rewipe" {
            options.rewipe = true;
        } else {
            return Err(usage(format!("unrecognized arguments: {arg}")));
        }
    }
    Ok(options)
}

fn config_error(message: String) -> Failure {
    Failure::Config(message)
}

/// The whole number in `var`, else `default` for an unset *or empty* variable: an
/// empty value is how a `.env` carries a placeholder, and treating it as zero would
/// re-fetch everything on every run (`sources.env_int`).
pub fn env_int(var: &str, default: i64, what: &str) -> Result<i64, Failure> {
    let raw = std::env::var(var).unwrap_or_default();
    let raw = py_strip(&raw);
    if raw.is_empty() {
        return Ok(default);
    }
    raw.parse().map_err(|_| {
        config_error(format!(
            "{var} must be {what}, got {}",
            crate::decisions::py_repr(raw)
        ))
    })
}

/// The flag wins, then `<NAME>_STALE_DAYS`, then the shared default.
pub fn resolve_stale_days(name: &str, flag: Option<i64>) -> Result<i64, Failure> {
    if let Some(days) = flag {
        return Ok(days);
    }
    env_int(
        &format!("{}_STALE_DAYS", name.to_uppercase()),
        DEFAULT_STALE_DAYS,
        "a whole number of days",
    )
}

/// The credential in `var`, or a config error saying what it is for.
pub fn credential(var: &str, purpose: &str) -> Result<String, Failure> {
    let value = std::env::var(var).unwrap_or_default();
    let value = py_strip(&value);
    if value.is_empty() {
        return Err(config_error(format!(
            "{var} must be set in .env to {purpose}"
        )));
    }
    Ok(value.to_string())
}

/// The configuration, read before anything else, as `GatedSource.run` reads it.
pub fn config() -> Result<Config, Failure> {
    Config::from_env().map_err(store_failure)
}

/// Open the store for writing.
pub fn open(config: &Config) -> Result<plexdb_store::Store, Failure> {
    open_store(&config.store_path).map_err(store_failure)
}

pub fn store_failure(err: StoreError) -> Failure {
    match err {
        StoreError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}
