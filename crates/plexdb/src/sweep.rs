//! Why one of the optional-source sweeps (AniList, Wikidata, MDBList ratings, Letterboxd)
//! stopped. The message of a source failure is the source's own, which the step prints
//! after `error:`.

use crate::staleness::BadTime;
use crate::steps::Failure;

#[derive(Debug, thiserror::Error)]
pub enum SweepError {
    /// The source failed, or failed enough times in a row that the sweep gave up.
    #[error("{0}")]
    Source(String),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Stale(#[from] BadTime),
}

impl From<SweepError> for Failure {
    fn from(err: SweepError) -> Self {
        Failure::Failed(err.to_string())
    }
}

/// The message the sweeps raise after their consecutive-failure limit.
pub fn abort_message(
    count: usize,
    unit: &str,
    fetched: usize,
    failed: usize,
    tripping: &dyn std::fmt::Display,
) -> String {
    format!(
        "aborting after {count} consecutive failed {unit}: {fetched} title(s) fetched, \
         {failed} failed; tripping error: {tripping}"
    )
}
