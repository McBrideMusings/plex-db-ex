//! The part of `plexdb/config.py`'s `Config` the Plex and Tautulli steps need.
//! `.env` is not read here: the Python `plexdb` that hands a step over has already
//! loaded it into the environment this process inherits.

use plexdb_store::{StoreError, py_strip};

use crate::plex::PlexClient;
use crate::tautulli::TautulliClient;

#[derive(Clone, Default)]
pub struct Sources {
    pub plex_url: String,
    pub plex_token: String,
    /// `PLEX_SOURCE_ROOTS`: the mount roots stripped from a Plex path before it is hashed.
    pub source_roots: Vec<String>,
    pub tautulli_url: String,
    pub tautulli_api_key: String,
}

fn env(name: &str) -> String {
    std::env::var(name)
        .map(|raw| py_strip(&raw).to_string())
        .unwrap_or_default()
}

impl Sources {
    pub fn from_env() -> Self {
        Sources {
            plex_url: env("PLEX_URL"),
            plex_token: env("PLEX_TOKEN"),
            source_roots: env("PLEX_SOURCE_ROOTS")
                .split(',')
                .map(|root| py_strip(root).to_string())
                .filter(|root| !root.is_empty())
                .collect(),
            tautulli_url: env("TAUTULLI_URL"),
            tautulli_api_key: env("TAUTULLI_API_KEY"),
        }
    }

    /// A Plex client, or a `Config` error saying which settings are absent.
    pub fn plex(&self, need: &str) -> Result<PlexClient, StoreError> {
        if self.plex_url.is_empty() || self.plex_token.is_empty() {
            return Err(StoreError::Config(format!(
                "PLEX_URL and PLEX_TOKEN must be set in .env to {need}"
            )));
        }
        PlexClient::new(&self.plex_url, &self.plex_token)
    }

    pub fn tautulli(&self) -> Result<TautulliClient, StoreError> {
        TautulliClient::new(&self.tautulli_url, &self.tautulli_api_key)
    }

    /// `PLEX_SOURCE_ROOTS`, refused when empty with `why`: an empty list is silently
    /// wrong rather than harmlessly absent, because a GUID-less title's id would
    /// hash the mount point and join with nothing in any other store.
    pub fn require_roots(&self, why: &str) -> Result<&[String], StoreError> {
        if self.source_roots.is_empty() {
            return Err(StoreError::Config(why.to_string()));
        }
        Ok(&self.source_roots)
    }
}
