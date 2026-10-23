//! The sweep steps this binary runs, with the order and failure policy each had
//! as a Python command module (`ORDER`, `SWEEP`, ADR-0014). The Python sweep
//! reads this list through `plexdb-rs steps --json` and merges it into its plan.

use serde::Serialize;

use crate::list_decisions;

/// What a step's failure means to the sweep (`plexdb.sweep.Step`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum Policy {
    /// Its failure ends the sweep, and nothing is published.
    #[serde(rename = "required")]
    Required,
    /// Its failure is reported and the sweep continues.
    #[serde(rename = "best-effort")]
    BestEffort,
}

/// Why a step stopped. `Config` is a setting that is absent, which the sweep
/// reports as skipped rather than failed.
#[derive(Debug)]
pub enum Failure {
    Config(String),
    Failed(String),
}

#[derive(Debug, Serialize)]
pub struct Step {
    pub name: &'static str,
    pub order: u32,
    pub policy: Policy,
    #[serde(skip)]
    pub run: fn() -> Result<(), Failure>,
}

fn fold_list_decisions() -> Result<(), Failure> {
    use list_decisions::FoldError;
    use plexdb_store::StoreError;
    list_decisions::run().map_err(|err| match err {
        FoldError::Store(StoreError::Config(message)) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    })
}

/// Every step, in sweep order.
pub const STEPS: &[Step] = &[Step {
    name: list_decisions::NAME,
    // Right after `fold-name-decisions`: a sweep starts by applying what a
    // person decided since the last one; the harvest that acts on it runs later.
    order: 14,
    // A malformed file costs the sweep one step's report, not a night's read.
    policy: Policy::BestEffort,
    run: fold_list_decisions,
}];

pub fn find(name: &str) -> Option<&'static Step> {
    STEPS.iter().find(|step| step.name == name)
}
