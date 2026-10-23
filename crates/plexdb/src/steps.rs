//! The sweep steps this binary runs. Their order and failure policy are declared
//! in `plexdb/rust_steps.json`, which the Python sweep reads to plan a run
//! without starting this binary; this module only maps each name to its code.

use crate::list_decisions;

/// Why a step stopped. `Config` is a setting that is absent, which the sweep
/// reports as skipped rather than failed.
#[derive(Debug)]
pub enum Failure {
    Config(String),
    Failed(String),
}

pub type Run = fn() -> Result<(), Failure>;

fn fold_list_decisions() -> Result<(), Failure> {
    use list_decisions::FoldError;
    use plexdb_store::StoreError;
    list_decisions::run().map_err(|err| match err {
        FoldError::Store(StoreError::Config(message)) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    })
}

/// Every step, by the name `plexdb/rust_steps.json` declares it under.
pub const STEPS: &[(&str, Run)] = &[(list_decisions::NAME, fold_list_decisions)];

pub fn find(name: &str) -> Option<Run> {
    STEPS
        .iter()
        .find(|(step, _)| *step == name)
        .map(|(_, run)| *run)
}
