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
watch history. Recomputed per pass, never stored.
_Avoid_: profile, model, recommendations

**item_id**:
A title's identity everywhere in the Store — the opaque string `etv-station` derives from
external GUIDs, first-hit-wins `imdb:` → `tmdb:` → `tvdb:` → `plex:`, else `fs:<hash>`.
_Avoid_: tmdb_id, rating key, GUID

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
