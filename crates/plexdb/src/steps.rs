//! The sweep steps this binary runs. Their order and failure policy are declared
//! in `plexdb/rust_steps.json`, which the Python sweep reads to plan a run
//! without starting this binary; this module only maps each name to its code.

use plexdb_store::StoreError;

use crate::decisions::FoldError;
use crate::{list_decisions, merge_decisions, migrate, name_decisions, role_decisions, tmdb_steps};

/// Why a step stopped. `Config` is a setting that is absent, which the sweep
/// reports as skipped rather than failed.
#[derive(Debug)]
pub enum Failure {
    Config(String),
    Failed(String),
    /// The command line is wrong: an unknown flag, a missing value.
    Usage(String),
}

/// A step, given the arguments after its name.
pub type Run = fn(&[String]) -> Result<(), Failure>;

/// A step that takes no arguments.
fn no_args(name: &str, args: &[String]) -> Result<(), Failure> {
    match args.first() {
        None => Ok(()),
        Some(_) => Err(Failure::Usage(format!(
            "{name} takes no arguments, got {}",
            args.join(" ")
        ))),
    }
}

fn failure(err: StoreError) -> Failure {
    match err {
        StoreError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}

fn migrate(args: &[String]) -> Result<(), Failure> {
    no_args(migrate::NAME, args)?;
    migrate::run().map_err(failure)
}

fn fold(result: Result<(), FoldError>) -> Result<(), Failure> {
    result.map_err(|err| match err {
        FoldError::Store(err) => failure(err),
        other => Failure::Failed(other.to_string()),
    })
}

fn fold_list_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(list_decisions::NAME, args)?;
    fold(list_decisions::run())
}

fn fold_merge_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(merge_decisions::NAME, args)?;
    fold(merge_decisions::run())
}

fn fold_role_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(role_decisions::NAME, args)?;
    fold(role_decisions::run())
}

fn fold_name_decisions(args: &[String]) -> Result<(), Failure> {
    no_args(name_decisions::NAME, args)?;
    fold(name_decisions::run())
}

/// Every step, by the name `plexdb/rust_steps.json` declares it under.
pub const STEPS: &[(&str, Run)] = &[
    (migrate::NAME, migrate),
    (merge_decisions::NAME, fold_merge_decisions),
    (role_decisions::NAME, fold_role_decisions),
    (name_decisions::NAME, fold_name_decisions),
    (list_decisions::NAME, fold_list_decisions),
    (tmdb_steps::KEYWORDS, tmdb_steps::run_keywords),
    (tmdb_steps::LANGUAGE, tmdb_steps::run_language),
    (tmdb_steps::EDGES, tmdb_steps::run_edges),
];

pub fn find(name: &str) -> Option<Run> {
    STEPS
        .iter()
        .find(|(step, _)| *step == name)
        .map(|(_, run)| *run)
}
