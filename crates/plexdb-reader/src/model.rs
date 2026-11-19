//! Typed rows returned by [`crate::Reader`]'s accessors.
//!
//! Deliberately concrete structs rather than a dynamic row type: a schema
//! change that drops a field a consumer reads fails that consumer's
//! **build**, not a runtime query built from a string (ADR-0003).

use serde::Serialize;

/// One namespaced, opaque fact about a title — a row of `enrichment`.
///
/// The store never interprets `key`/`value`; neither does this crate.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EnrichmentFact {
    pub namespace: String,
    pub key: String,
    pub value: String,
    pub fetched_at: String,
}

/// One directed, typed, ranked relationship between two titles — a row of
/// `edges`. A snapshot of what a source said at one moment, not a fact.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Edge {
    pub from_id: String,
    pub to_id: String,
    pub edge_type: String,
    pub rank: i64,
    pub fetched_at: String,
}

/// One tag that co-occurs with another on the tag network — a row of
/// `tag_network_edge` seen from one of its two ends.
///
/// `shared` is how many titles carry both tags; `df` is how many titles carry
/// `tag` alone, so a caller can tell a strong tie to a rare tag from one to a
/// ubiquitous tag.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TagNeighbour {
    pub tag: String,
    pub shared: i64,
    pub df: i64,
}

/// One attribute in a user's Layer 2 taste vector, and how strongly their
/// watch history carries it.
///
/// `weight` is the sum, over every title the account watched, of
/// `sqrt(r) / attributes_on_that_title`, where `r` is that account's
/// consumption of the title measured in **seasons** — a film, or one full
/// season of a show, is `r = 1`. Titles below `r = 0.5` contribute nothing.
/// Dividing by the title's attribute count stops a heavily-tagged title
/// outvoting a sparsely-tagged one (plex-db-ex ADR-0011).
///
/// **Still no ranking policy here**, and now on evidence rather than by
/// deferral: measured against 25,835 real plays, a recency half-life
/// concentrated the vector onto one recent show rather than mixing it, and an
/// abandoned watch is no signal rather than negative signal. The exploration
/// fraction describes how a *channel* is assembled, not what a person likes,
/// so it stays with the consumer. There is no constant in here to tune.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TasteAttribute {
    pub namespace: String,
    pub key: String,
    pub value: String,
    pub weight: f64,
}

/// One crowd list a title appears on, and where in it — a join of one
/// `collection_membership` row with the `collection` row it belongs to.
///
/// `collection_id`, `source`, and `observed_at` (the membership's own, not
/// the list's) are always present; every other field is nullable at the
/// source and stays `Option<_>` here rather than collapsing to `0` or `""`,
/// because a missing `rank` must stay distinguishable from rank 1 and a
/// missing `name`/`url`/`size`/`likes` from a source that genuinely recorded
/// none. There is deliberately no `weight` field — `rank`, `size`, `likes`
/// and `mentions` are the facts a source gave; a consumer wanting one number
/// computes it from those (plex-db-ex#33, ADR-0012).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CollectionMembership {
    pub collection_id: String,
    pub source: String,
    pub name: Option<String>,
    pub url: Option<String>,
    pub size: Option<i64>,
    pub likes: Option<i64>,
    pub rank: Option<i64>,
    pub mentions: Option<i64>,
    pub observed_at: String,
}

/// A user's Layer 2 taste vector, plus what the rollup could not weigh
/// properly.
///
/// `shows_without_seasons` names every show whose episodes Plex files with no
/// season number. There is no season length to divide by, so each play counts
/// as one unit — which over-weights a long show. Reported rather than
/// silently folded in, because the alternative is a vector that is quietly
/// wrong in a way nothing downstream can detect.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TasteVector {
    pub attributes: Vec<TasteAttribute>,
    pub shows_without_seasons: Vec<String>,
}

/// One title — a row of `items` — with the related facts a request's `include` asked for.
///
/// Each included field is absent unless asked for; asked for, a list is `[]` and audio
/// facts are `null` when the store holds none.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Item {
    pub item_id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub title: String,
    pub title_sort: Option<String>,
    pub show_title: Option<String>,
    pub show_item_id: Option<String>,
    pub season: Option<i64>,
    pub episode: Option<i64>,
    pub year: Option<i64>,
    pub duration_ms: Option<i64>,
    pub content_rating: Option<String>,
    pub studio: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub enrichment: Option<Vec<EnrichmentFact>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub external_ids: Option<Vec<ExternalId>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_facts: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub plex: Option<Vec<PlexEntry>>,
}

/// One id a source gives a title — a row of `external_ids`. `kind` scopes the id to a
/// media type (schema v5); `last_seen` is when a scan last observed it (schema v8).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ExternalId {
    pub ns: String,
    pub value: String,
    pub kind: String,
    pub last_seen: String,
}

/// One Plex library entry that resolves to a title — a row of `plex_items`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlexEntry {
    pub rating_key: String,
    pub section_id: String,
    pub last_seen: String,
}

/// One file Plex lists for a title — a row of `media_part`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MediaPart {
    pub part_id: i64,
    pub item_id: String,
    pub rating_key: String,
    pub file: String,
    pub size: Option<i64>,
    pub container: Option<String>,
    pub video_codec: Option<String>,
    pub audio_codec: Option<String>,
    pub height: Option<i64>,
    pub bitrate: Option<i64>,
    pub duration_ms: Option<i64>,
    pub plex_updated_at: Option<i64>,
    pub fetched_at: String,
}

/// One play — a row of `plays`, without the client's IP address and device identifier,
/// which describe the household's network rather than what was watched, and without the
/// ingest's own keys (`tautulli_id`, `rating_key`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Play {
    pub history_key: String,
    pub item_id: String,
    pub plex_account_id: i64,
    pub platform: Option<String>,
    /// Unix seconds.
    pub viewed_at: i64,
    pub percent_complete: Option<i64>,
    pub paused_counter: Option<i64>,
    pub seconds_watched: Option<i64>,
}

/// One Plex account the store has plays for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Account {
    pub account_id: i64,
    pub plays: i64,
    /// Unix seconds of the newest play.
    pub last_viewed_at: i64,
}

/// One stored keyword: every raw spelling recorded for it (`keyword_forms`) and the
/// display name a person gave it (`keyword_names`), for display only.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Keyword {
    pub keyword: String,
    pub name: Option<String>,
    pub surfaces: Vec<String>,
}

/// The judge's verdict and a person's decision on whether two keywords mean the same —
/// a row of `keyword_pairs`, `keyword_a < keyword_b`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct KeywordPair {
    pub keyword_a: String,
    pub keyword_b: String,
    pub jev_score: Option<f64>,
    pub jev_model: Option<String>,
    pub judged_at: Option<String>,
    pub jev_error: Option<String>,
    pub decision: Option<String>,
    pub decided_at: Option<String>,
}

/// What the store records about the roles one keyword plays.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct KeywordRoles {
    pub keyword: String,
    /// A source's score for each role — rows of `keyword_roles`.
    pub verdicts: Vec<RoleVerdict>,
    /// A person's decision on each role — rows of `keyword_role_decisions`.
    pub decisions: Vec<RoleDecision>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RoleVerdict {
    pub role: String,
    pub source: String,
    pub score: Option<f64>,
    pub model: Option<String>,
    pub error: Option<String>,
    pub stated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RoleDecision {
    pub role: String,
    pub decision: String,
    pub decided_at: String,
}

/// A source stating that a title carries a keyword in a role — a row of
/// `keyword_role_statements`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RoleStatement {
    pub keyword: String,
    pub role: String,
    pub source: String,
    pub stated_at: String,
}

/// One page of a list: `next` is the cursor to pass as `after` for the page after it,
/// `null` on the last page.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Page<T> {
    pub data: Vec<T>,
    pub has_more: bool,
    pub next: Option<String>,
}
