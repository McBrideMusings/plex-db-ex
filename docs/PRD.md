---
outline: deep
---

# plex-db-ex PRD

Vocabulary is defined in [CONTEXT](./CONTEXT); the tables are in [Schema](./schema); the
decisions and their rejected alternatives are in [Decisions](./adr/). The reasoning and prior
art behind all of it is in [the original spec](./spec), which this document supersedes.

## Problem Statement

Plex knows a title's genre, cast, director, studio, year, and content rating. It does not know
what a title is *about*. There is no concept-level tag, no tone, no micro-genre, and no
item-to-item relationship of any kind — collections are the only adjacency Plex has, and they
are heavyweight objects rather than a graph.

That gap has produced a concrete failure in a shipped tool. `etv-station`'s "For You" channel
ranks on recently-aired suppression, watch recency, release year, and season number, with a
deterministic tiebreak. It uses no genre, cast, director, studio, or thematic signal at all, so
it is a replay-affinity ranker wearing a recommender's name: it can only tell you whether you
have touched something, never whether you would like it. It cannot make the jump from one
grilling video to another, because it has no representation of "grilling".

Two separate projects need the same missing layer. `etv-station` needs it to schedule channels
by taste, and the collection-generation pipeline needs it to decide what belongs in a
collection. Neither should build it twice, and today the second one has built half of it
privately.

## Solution

A single SQLite file holding the metadata Plex cannot carry: namespaced enrichment tags,
ranked item-to-item affinity edges, weighted collection membership, and a per-user watch-history
ledger. One process writes it; every consumer opens it read-only, and the schema is the contract
between them.

The store's only hard dependency is a Plex server. Every other source — TMDB, Trakt, Tautulli,
MDBList, Wikidata, Reddit — is an optional adapter, and the store degrades rather than fails when
one is absent. A consumer talks to Plex directly for everything Plex can answer and reaches this
store only for what Plex physically cannot hold; where that is impossible, the consumer depends
on both a Plex instance and a plex-db-ex instance, never on this store as a stand-in for Plex.

Taste is derived, not stored. A per-user weighted attribute vector is rolled up from the cached
item graph through that user's watch history at the moment a consumer asks for it, then thrown
away — watch history changes constantly, and invalidating a cached profile is more work than
rebuilding it.

## User Stories

1. As a **channel author**, I want a pool that ranks on what a title is about rather than
   whether I have watched it recently, so that a "For You" channel can surface something I have
   never touched and would actually like.
2. As a **plugin author**, I want the store reachable from a Rhai scorer through a granted
   capability, so that a channel that never asked for taste data cannot be affected by it.
3. As a **plugin author**, I want typed accessors rather than hand-written SQL in a scripting
   language, so that a schema change breaks the build instead of failing silently at runtime.
4. As the **collection pipeline**, I want to read enrichment and edges that some other process
   already paid to fetch, so that I stop maintaining my own TMDB cache against the same rate
   limits.
5. As a **consumer author**, I want a title to have one identity across every tool, so that a
   join between my data and the store's returns rows rather than nothing.
6. As an **enrichment writer**, I want a namespace only I can wipe and rewrite, so that adding
   my source cannot corrupt or collide with another writer's rows.
7. As a **scorer**, I want an edge to carry the source's rank rather than a boolean, so that a
   #2 recommendation and a #20 recommendation are distinguishable.
8. As a **scorer**, I want the store to answer "what is adjacent to this title, by this kind of
   relationship, in either direction" without parsing strings.
9. As an **operator**, I want a title enriched exactly once and refreshed only past a staleness
   threshold, so that a full sweep does not re-fetch what it already has.
10. As an **operator**, I want the store to run against Plex alone, so that standing it up does
    not require Tautulli, a TMDB key, or any account I do not already have.
11. As an **operator**, I want the fingerprint tuple captured per play, so that the two shared
    accounts can later be split into the people actually behind them.
12. As a **viewer on a shared account**, I want my own watch history separated from the other
    person on that account, so that a taste profile built from it reflects me.
13. As a **viewer**, I want a deliberate fraction of adjacent-but-unseen material, so that the
    channel does not collapse into an echo of what I already watch.
14. As a **Plex-only tool**, I want inferred tags projected into Plex labels, so that I benefit
    from enrichment without linking anything.
15. As a **maintainer**, I want a title I abandoned after ten minutes treated as low-confidence
    evidence rather than a verdict, so that one bad mood does not poison a taste profile.

## Implementation Decisions

**One Writer, many Readers, and the SQLite schema is the interface.** The Writer is a Python
package; every Consumer opens the file read-only. No server, no IPC, no shared library across
languages. Python because the acquisition code — TMDB, Trakt, and MDBList clients, resolution,
and enrichment — already exists and runs weekly in the collection pipeline, and because
duplicating it in Rust would mean two crawlers against the same rate limits drifting apart
immediately. ([ADR-0001](./adr/0001-one-writer-many-readers-sqlite-file-is-the-interface))

**`item_id` is an opaque first-hit-wins string over external GUIDs** — `imdb:` before `tmdb:`
before `tvdb:` before `plex:`, falling back to a hash of the canonical path — identical to what
`etv-station` already derives, so the two agree with no coordination. A separate `external_ids`
table carries every other id a title is known by, because the enrichment fetcher needs a TMDb id
that the primary key is not guaranteed to be, and because TVDB-only shows and anime have no TMDb
id at all. ([ADR-0002](./adr/0002-item-id-is-the-entry-id-string))

**The derivation rule now exists in two languages**, which is a real duplication across a
language seam. Both implementations are pinned to one shared fixture — a table of GUID sets and
their expected ids — so a change to the priority order fails on both sides rather than one.

**A typed Rust Reader crate, exposed to Rhai plugins behind a capability grant.** The spec's
"core never links this library" is not implementable: plugins are Rhai scripts, which cannot open
a file or link a crate, so the core is the only thing that can. What the rule protects — a
channel that did not ask for taste data cannot be affected by it — is enforced by the grant, not
by the absence of a dependency. ([ADR-0003](./adr/0003-rust-reader-crate-behind-a-plugin-capability-grant))

**Enrichment is namespaced and opaque.** No first-class `mood` / `keywords` / `award` columns —
the store indexes and serves values it never interprets, and a writer may wipe and rewrite only
its own namespace. `fetched_at` drives staleness, not correctness.

**Edges are snapshots, not facts.** On re-pull, the whole `(from_id, edge_type)` set is replaced
rather than appended, so additions and removals fall out on their own. `from_id` and `to_id` are
separate columns so both directions are queryable without string parsing, and the source's rank
is stored rather than collapsed to a boolean.

**The store owns watch history, with Plex required and Tautulli optional.** Plex's history
supplies account, device, timestamp, and rating key; the device joins to Plex's device list for
the client machine id and platform. The Tautulli adapter adds IP, completion percentage, and
paused time when configured. Measured against the live server: of 268 plays finished at 90% or
more, 261 appear in Plex history — but of 17 plays abandoned under 40%, only 3 do. **Plex history
is a watched-it ledger, so a Plex-only deployment has no negative signal at all, not a weak
one.** ([ADR-0004](./adr/0004-the-store-owns-watch-history))

**The store walks Plex itself.** Watch history from either source identifies a title only by
rating key and a `plex://` agent GUID — the weakest tier in the derivation order — so the
external GUIDs an `item_id` is built from only exist where somebody has walked the library. The
store does its own walk rather than reading a consumer's database, which is what lets each tool
depend on Plex instead of on each other.
([ADR-0005](./adr/0005-the-store-walks-plex-itself-and-augments-never-replaces))

**Layer 2 has no schema.** The taste vector is computed by the Reader crate at request time and
never persisted. Revisit only if profiling says otherwise.

**Write-back to Plex is a one-way projection, designed now and built later.** Labels only, never
genres — genres feed Plex's own browse UI and its agents reason about them. Prefixed for
namespacing and cheap wipe-by-prefix, booleanised at write time because a label is a flat string
Plex cannot range-query. Lock only the specific field written, never the item wholesale, because
locking is sticky and the unlock path has to exist from day one. Edges, ranks, `fetched_at`, and
per-user vectors cannot be projected at all, which is why this store stays authoritative.

**Fingerprint tiers, in order:** client machine id, then IP, then platform/product, with device
display name excluded as a clustering key. Time-of-day is deferred — it earns a place only when
one device genuinely serves two different people, which does not describe this user base. Note
that the Android machine id already embeds the product, so the third tier is partly redundant
with the first.

**The negative-signal floor measures footage actually watched net of pause** — a sampled play
showed 182 seconds of paused time against an otherwise identical duration. No play under roughly
30 seconds of real footage counts at all, and even above the floor it stays low-confidence until
measurement says it predicts something.

## Testing Decisions

Two seams, and no more.

**The store's public API** carries almost everything. A test writes through the Python interface
and asserts by querying the database — never by reaching into a private function. The behaviours
worth pinning:

- An `item_id` derives correctly from each GUID combination, including the fallback and the
  ordering between competing GUIDs.
- A library walk populates items and external ids from a recorded Plex response, and a second
  walk over the same response changes nothing.
- An enrichment fetch writes rows under its namespace with a timestamp, and a second run inside
  the staleness threshold performs no fetch — asserted by a fake source that fails the test if
  called twice.
- A namespace wipe removes only its own rows.
- Re-pulling an edge set replaces it: an edge the source dropped is gone, not merely stale.
- A play with completion data and a play without both round-trip, since the second is the normal
  Plex-only case rather than an error.

**The shared identity fixture** is the second seam, and it exists precisely because it crosses a
language boundary. A file of GUID sets and expected ids is read by the Python tests here and by
the Rust tests in `etv-station`. Neither side may edit it unilaterally; a change that breaks the
other is the failure this seam is for.

External sources are never called in tests — every adapter is exercised against recorded
responses, which is also what makes the "did it re-fetch?" assertions possible.

## Out of Scope

Not a Plex replacement or mirror; Plex stays authoritative for library contents, playback, and
users. Not a recommender product — the store holds signal and answers queries, and ranking policy
stays in consumers. Not a live service in the request path.

Collaborative filtering is out, and not as a concession: it needs millions of users and rich
behavioural telemetry, and this is roughly ten users with no scroll signal. Content-based
filtering is the correct choice at this scale. Household co-occurrence stays a legitimate but
minor second-order enhancement.

IMDb "more like this" is out — no free official API, and scraping it is against terms.

Folding `etv-station`'s catalog into this store is out. It would remove the duplicate library
walk, and it is still rejected: this store augments Plex, so a consumer must be able to talk to
Plex directly without it.

Backward compatibility is out. Readers have no version negotiation, so a schema change is a
breaking change for every consumer at once, handled by updating them rather than by layering
compatibility shims.

## Further Notes

**Build order.** The first slice is identity and enrichment end to end: walk Plex, derive ids,
fetch TMDB keywords, cache them. It comes first because the duplicated identity rule is the one
failure that makes everything downstream join to nothing, and this is the cheapest thing that
puts both implementations against the real library and shows whether they agree.

**No skip means no fast negative feedback.** On a feed, skipping is free, so a bad pick costs
nothing and the ranker learns immediately. On a channel there is no skip, so a bad pick costs the
viewer real minutes and produces no signal. That argues for exploitation-heavy ranking with a
small deliberate discovery slice — a design constraint, not a limitation to apologise for.

**Star ratings are the highest-quality signal and the thinnest.** Managed users keep independent
ratings on the same file with no merging, which makes them clean and explicit — but they are
near-useless for the shared accounts, because a rating carries no device metadata to cluster on
the way a play does.

**Open questions**, all downstream of the first slice: where the database file physically lives
and how each consumer reaches it, given SQLite over a network share is unsafe; how a breaking
schema change is rolled out; and who owns the Layer 2 policy knobs — recency half-life,
exploration fraction, negative-signal weight.

**One known-bad credential.** The Trakt client id inherited from the collection pipeline is 43
characters where Trakt's is 64, and a live call with it returns 403. Trakt work needs a fresh
credential first.
