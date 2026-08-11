# plex-db-ex

A queryable metadata and affinity store layered over a Plex library, written by one
process and read by several. Plex stays authoritative for library contents and playback.

## Language

### Domain

**Store**:
The SQLite database file this repo defines, owns, and writes.
_Avoid_: the graph, the cache, the DB

**Layer 1**:
User-agnostic scraped facts about titles — enrichment tags, affinity edges, weighted
collection membership, embeddings. Cached hard, expensive to acquire.

**Layer 2**:
A single user's weighted attribute vector, rolled up from Layer 1 through that user's
watch history. Recomputed per pass, never stored. Carries no ranking policy — no recency
half-life, no tunable damping constant (ADR-0011).
_Avoid_: profile, model, recommendations

**Season unit**:
The amount of watching that counts as 1 in a taste vector — one full season of a show, or
one film. `r = plays / median season length`; below `r = 0.5` a title contributes nothing.
Chosen because every other unit measures something that is not taste: per-episode measures
time occupied, per-title equates a nine-minute special with a seven-season run (ADR-0011).
_Avoid_: play count, watch count, completion

**item_id**:
A title's identity everywhere in the Store — the opaque string `etv-station` derives from
external GUIDs, first-hit-wins `imdb:` → `tmdb:` → `tvdb:` → `plex:`, else `fs:<hash>`.
_Avoid_: tmdb_id, rating key, GUID

**External id**:
One `(source, number)` pair Plex reports for a title — an IMDb, TMDB, TVDB or Plex id.
Only IMDb numbers movies and shows in a single shared list; **TMDB and TVDB number each
media type separately**, so `tmdb 1678` is a movie *and* an unrelated show, and an external
id identifies a title only together with its media type.
_Avoid_: GUID (Plex's word for the whole set), external key

**Enrichment**:
A namespaced key-value fact about one title, opaque to the Store.

**Namespace**:
A hard partition of enrichment rows owned by exactly one writer, which may wipe and
rewrite only its own rows.

**Edge**:
A directed item-to-item relationship carrying a source rank. A snapshot of what a
source said at one moment, not a fact.
_Avoid_: link, relation, similarity

**Weighted collection membership**:
A (title, weight, source) triple recording that some list, subreddit, or local Plex
collection contained a title.

**Play**:
One recorded watch event — a title, a Plex account, a client, a timestamp, and however much
completion data the configured history source could supply.

**History source**:
An adapter supplying Plays. Plex is required and always present; Tautulli is optional and
adds IP, completion percentage, and paused time.

**Fingerprint**:
The tuple used to split a shared Plex account into latent users — client machine ID,
then IP, then platform/product, with device display name excluded as a clustering key.
IP does double duty: it fills in only when a play has no client machine ID (the fallback
chain above), and — issue #27 — it also *joins* two different client machine IDs into one
cluster when both recur at the same IP within one account and that IP is not seen under any
other Plex account; see `plexdb.clusters` for the exact recurrence and cross-account
thresholds.

**Shared account**:
A Plex account genuinely used by more than one person, so its plays are worth splitting
by Fingerprint. Configuration the server owner states (`PLEXDB_SHARED_ACCOUNT_IDS`), never
inferred from device or IP counts — a personal account can show more devices than a shared
one. Every account not listed is one named person; `plexdb latent-users` reports it as a
single user with no device or cluster numbers at all.

**Unattributed**:
The one bucket per shared account that `plexdb latent-users` folds every cluster under
`LATENT_USER_FLOOR` (20) plays into, instead of listing each as its own latent user or
dropping its plays — issue #28. Not a latent user: no keyword profile, no pairwise overlap.
The report names both the play count and the device count it covers.

**Projection**:
The one-way, booleanized, prefix-namespaced write-back of enrichment into Plex labels,
for the benefit of tools that only speak Plex. Never a round trip.
_Avoid_: sync, export, write-back

### Architecture

**Writer**:
The single process permitted to write the Store.

**Snapshot**:
A consistent single-file copy of the Store, published by the Writer and opened read-only by
every Reader. Readers never touch the live Store.
_Avoid_: replica, backup, export

**Reader**:
Any process that opens the Snapshot read-only. Readers never write.

**Consumer**:
A project that uses the Store — today `etv-station` (via plugin only) and `curator`.

**Sweep**:
One scheduled run of the Writer: every Step in order, ending in a published Snapshot. Runs
as `plexdb sweep`; the container's entrypoint invokes it and nothing else.
_Avoid_: job, pipeline, cron run

**Step**:
One command taking part in a Sweep. Declared `REQUIRED` — its failure ends the Sweep — or
`BEST_EFFORT` — its failure is reported and the Sweep continues. A command that declares
neither is not in the Sweep at all.

**Gated Source**:
An external source whose units carry a `fetched_at` and are re-fetched only once stale. The
unit is not always a title: TMDB keywords and edges gate per title, MDBList per list.
`enrich-tautulli-plays` reads an external thing and is *not* this — it
gates on staleness.
_Avoid_: cache, provider

## Relationships

- A **Consumer** is a **Reader**, a **Writer**, or both.
- **Layer 2** is derived from **Layer 1** plus watch history; it is never persisted.
- The Store's SQLite schema is the interface between **Writer** and **Reader** — there is
  no server, no IPC, and no language coupling.

## Decisions so far

- [ADR-0001](./adr/0001-one-writer-many-readers-sqlite-file-is-the-interface.md) — one
  writer, many readers; the SQLite file is the interface.
- [ADR-0002](./adr/0002-item-id-is-the-entry-id-string.md) — `item_id` is the `entry_id`
  string, with `external_ids` alongside it.
- [ADR-0003](./adr/0003-rust-reader-crate-behind-a-plugin-capability-grant.md) — a typed
  Rust reader crate, reached by Rhai plugins through a capability grant.
- [ADR-0004](./adr/0004-the-store-owns-watch-history.md) — the Store owns watch history;
  Plex is the required source, Tautulli an optional adapter.
- [ADR-0005](./adr/0005-the-store-walks-plex-itself-and-augments-never-replaces.md) — the
  Store walks Plex itself and augments Plex rather than replacing it.
- [ADR-0006](./adr/0006-the-identity-fixture-is-duplicated-and-guarded-by-a-hash.md) — the
  identity fixture is duplicated in both repos and guarded by a hash.
- [ADR-0007](./adr/0007-readers-get-a-snapshot-not-the-live-store.md) — Readers get a
  Snapshot, not the live Store.
- [ADR-0008](./adr/0008-the-walk-never-repoints-an-existing-item-id.md) — the walk never
  repoints an existing `item_id` when a title's GUID set changes; it keeps the identity and
  records the new GUID alongside it.
