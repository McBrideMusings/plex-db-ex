//! Every accessor as one value: a [`Call`] names a method and its arguments, and
//! [`Reader::answer`] runs it and returns the result as JSON text.
//!
//! `plexdb-read` builds a `Call` from its command line and Plex DB-EX's `/api/store/*`
//! routes build one from a request (ADR-0031), so both answer every accessor, with the same
//! arguments, in the same JSON.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::{Reader, ReaderError, Search};

/// One accessor and its arguments.
#[derive(Debug, Clone, PartialEq)]
pub enum Call {
    EnrichmentFor {
        item_id: String,
        namespace: String,
    },
    EnrichmentForMany {
        item_ids: Vec<String>,
        namespace: String,
    },
    AudioFactsFor {
        item_id: String,
    },
    AudioFactsForMany {
        item_ids: Vec<String>,
    },
    KeywordForSurface {
        surface: String,
    },
    SurfacesForKeyword {
        keyword: String,
    },
    DisplayName {
        keyword: String,
    },
    EdgesFrom {
        item_id: String,
        edge_type: String,
    },
    EdgesTo {
        item_id: String,
        edge_type: String,
    },
    TagNeighbours {
        kind: String,
        tag: String,
    },
    CollectionsFor {
        item_id: String,
    },
    TasteVectorFor {
        account: i64,
    },
    PooledTasteVector,
    WatchedUnitsFor {
        account: i64,
    },
    WatchedUnits,
    /// [`Reader::search_drawn`]: the ranking, or with `draw` the `draw` picks.
    Search {
        pool: Vec<String>,
        phrase: String,
        negatives: Vec<String>,
        weight: Option<f32>,
        temperature: Option<f32>,
        draw: Option<usize>,
        seed: u64,
    },
    Cuts {
        item_id: String,
    },
    Chapters {
        item_id: String,
    },
    Capabilities,
}

/// Which files a [`Reader`] must be opened on to answer a [`Call`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Needs {
    /// The store alone.
    Store,
    /// `frames.db` attached.
    Frames,
    /// `frames.db` and a Phrase cache.
    Search,
    /// Whatever there is: the answer reports which.
    Any,
}

impl Call {
    #[must_use]
    pub fn needs(&self) -> Needs {
        match self {
            Call::Search { .. } => Needs::Search,
            Call::Cuts { .. } | Call::Chapters { .. } => Needs::Frames,
            Call::Capabilities => Needs::Any,
            _ => Needs::Store,
        }
    }
}

/// Serialized straight to text: going through `serde_json::Value` would widen every `f32`
/// to an `f64` and print digits the similarity never had.
fn text<T: Serialize>(value: &T) -> Result<String, ReaderError> {
    serde_json::to_string(value).map_err(ReaderError::Reply)
}

impl Reader {
    /// Run `call` and return its result as JSON: a list as an array, an `Option` as the
    /// value or `null`, a `_for_many` map as an object keyed by `item_id` in id order.
    pub fn answer(&self, call: &Call) -> Result<String, ReaderError> {
        match call {
            Call::EnrichmentFor { item_id, namespace } => {
                text(&self.enrichment_for(item_id, namespace)?)
            }
            Call::EnrichmentForMany {
                item_ids,
                namespace,
            } => text(&self.enrichment_for_many(item_ids.iter().map(String::as_str), namespace)?),
            Call::AudioFactsFor { item_id } => text(&self.audio_facts_for(item_id)?),
            Call::AudioFactsForMany { item_ids } => {
                let found: BTreeMap<String, serde_json::Value> = self
                    .audio_facts_for_many(item_ids.iter().map(String::as_str))?
                    .into_iter()
                    .collect();
                text(&found)
            }
            Call::KeywordForSurface { surface } => text(&self.keyword_for_surface(surface)?),
            Call::SurfacesForKeyword { keyword } => text(&self.surfaces_for_keyword(keyword)?),
            Call::DisplayName { keyword } => text(&self.display_name(keyword)?),
            Call::EdgesFrom { item_id, edge_type } => text(&self.edges_from(item_id, edge_type)?),
            Call::EdgesTo { item_id, edge_type } => text(&self.edges_to(item_id, edge_type)?),
            Call::TagNeighbours { kind, tag } => text(&self.tag_neighbours(kind, tag)?),
            Call::CollectionsFor { item_id } => text(&self.collections_for(item_id)?),
            Call::TasteVectorFor { account } => text(&self.taste_vector_for(*account)?),
            Call::PooledTasteVector => text(&self.pooled_taste_vector()?),
            Call::WatchedUnitsFor { account } => text(&self.watched_units_for(*account)?),
            Call::WatchedUnits => text(&self.watched_units()?),
            Call::Search {
                pool,
                phrase,
                negatives,
                weight,
                temperature,
                draw,
                seed,
            } => {
                let pool: Vec<&str> = pool.iter().map(String::as_str).collect();
                let negatives: Vec<&str> = negatives.iter().map(String::as_str).collect();
                let q = Search {
                    pool: &pool,
                    phrase,
                    negatives: &negatives,
                    weight: *weight,
                    temperature: *temperature,
                };
                text(&self.search_drawn(&q, *draw, *seed)?)
            }
            Call::Cuts { item_id } => text(&self.cuts(item_id)?),
            Call::Chapters { item_id } => text(&self.chapters(item_id)?),
            Call::Capabilities => text(&self.capabilities()),
        }
    }
}
