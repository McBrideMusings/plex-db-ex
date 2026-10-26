//! The sweep steps this binary runs. Their order and failure policy are declared
//! in `plexdb/rust_steps.json`, which the Python sweep reads to plan a run
//! without starting this binary; this module only maps each name to its code.

use plexdb_store::StoreError;

use crate::{list_decisions, list_steps, migrate};

/// Why a step stopped. `Config` is a setting that is absent, which the sweep
/// reports as skipped rather than failed.
#[derive(Debug)]
pub enum Failure {
    Config(String),
    Failed(String),
    /// Arguments the step does not accept, which the caller sees as exit code 2.
    Usage(String),
}

pub type Run = fn(&[String]) -> Result<(), Failure>;

pub(crate) fn failure(err: StoreError) -> Failure {
    match err {
        StoreError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}

/// A step that takes no arguments refuses any.
fn no_arguments(name: &str, args: &[String]) -> Result<(), Failure> {
    if args.is_empty() {
        return Ok(());
    }
    Err(Failure::Usage(format!(
        "{name} takes no arguments, got {}",
        args.join(" ")
    )))
}

fn migrate(args: &[String]) -> Result<(), Failure> {
    no_arguments(migrate::NAME, args)?;
    migrate::run().map_err(failure)
}

fn fold_list_decisions(args: &[String]) -> Result<(), Failure> {
    use list_decisions::FoldError;
    no_arguments(list_decisions::NAME, args)?;
    list_decisions::run().map_err(|err| match err {
        FoldError::Store(err) => failure(err),
        other => Failure::Failed(other.to_string()),
    })
}

/// Every step, by the name `plexdb/rust_steps.json` declares it under.
pub const STEPS: &[(&str, Run)] = &[
    (migrate::NAME, migrate),
    (list_decisions::NAME, fold_list_decisions),
    (list_steps::HARVEST_MDBLIST, list_steps::harvest_mdblist),
    (
        list_steps::HARVEST_LETTERBOXD_LISTS,
        list_steps::harvest_letterboxd_lists,
    ),
    (
        list_steps::HARVEST_MDBLIST_SEARCH,
        list_steps::harvest_mdblist_search,
    ),
    (list_steps::LISTS, list_steps::lists),
    (list_steps::LIST, list_steps::list_one),
    (list_steps::LIST_DECIDE, list_steps::list_decide),
];

pub fn find(name: &str) -> Option<Run> {
    STEPS
        .iter()
        .find(|(step, _)| *step == name)
        .map(|(_, run)| *run)
}
