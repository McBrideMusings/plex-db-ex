//! Typed rows returned by [`crate::Reader`]'s accessors.
//!
//! Deliberately concrete structs rather than a dynamic row type: a schema
//! change that drops a field a consumer reads fails that consumer's
//! **build**, not a runtime query built from a string (ADR-0003).

/// One namespaced, opaque fact about a title — a row of `enrichment`.
///
/// The store never interprets `key`/`value`; neither does this crate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnrichmentFact {
    pub namespace: String,
    pub key: String,
    pub value: String,
    pub fetched_at: String,
}

/// One directed, typed, ranked relationship between two titles — a row of
/// `edges`. A snapshot of what a source said at one moment, not a fact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edge {
    pub from_id: String,
    pub to_id: String,
    pub edge_type: String,
    pub rank: i64,
    pub fetched_at: String,
}

/// One attribute in a user's Layer 2 taste vector: how many distinct titles
/// in their watch history carry it.
///
/// Deliberately the plain, unweighted count — no recency half-life, no
/// negative-signal discount for an abandoned play, no exploration fraction.
/// Those are ranking policy, owned by the consumer and not yet decided
/// (plex-db-ex#13). This is the rollup a policy gets layered on top of.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TasteAttribute {
    pub namespace: String,
    pub key: String,
    pub value: String,
    pub weight: i64,
}
