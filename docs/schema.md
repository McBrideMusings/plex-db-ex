---
outline: deep
---

# Schema

The SQLite schema **is** the public API of this project. One process writes it; every consumer
opens the same file read-only. There is no version negotiation, so a schema change is a
breaking change for every consumer at once ([ADR-0001](./adr/0001-one-writer-many-readers-sqlite-file-is-the-interface)).

## Versioning

The store carries its version in a `schema_version` table. Migrations are an append-only list
in `plexdb/schema.py`: index 0 takes an empty database to version 1, index 1 takes it to
version 2, and so on. A shipped migration is never edited — another is added.

Opening a store whose version is **higher** than the running build understands is refused
outright, because a newer writer may have added rows this build cannot see. A store carrying a
`schema_version` table with no row is reported as damaged rather than treated as empty.

**Versions 1 and 2 are live.** Everything under "Not yet built" is the target for later
slices.

## Version 1 — identity and enrichment

Every table keys on `item_id` — the opaque, deterministic string described in
[ADR-0002](./adr/0002-item-id-is-the-entry-id-string), derived first-hit-wins from external
GUIDs: `imdb:tt1375666`, else `tmdb:…`, else `tvdb:…`, else `plex:…`, else `fs:<hash>`.

```sql
CREATE TABLE items (
    item_id        TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    title          TEXT NOT NULL,
    title_sort     TEXT,
    show_title     TEXT,
    show_item_id   TEXT,
    season         INTEGER,
    episode        INTEGER,
    year           INTEGER,
    duration_ms    INTEGER,
    content_rating TEXT,
    studio         TEXT
);

-- Every other id a title is known by. Primary key is (ns, value), so one id
-- resolves to exactly one item and a collision is a write-time error rather
-- than a silent duplicate.
CREATE TABLE external_ids (
    item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns      TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (ns, value)
);

-- Where Plex keeps a title. Watch history identifies a title only by rating
-- key, so this is the join that makes a play resolvable to an item.
CREATE TABLE plex_items (
    rating_key TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    section_id TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE enrichment (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, namespace, key, value)
);
```

`external_ids` is what lets an enrichment fetcher that needs a TMDb id find one without
assuming the primary key is one. It is also where a Trakt slug, a Letterboxd URL, and a
resolved free-text Reddit title land.

Foreign keys are enforced (`PRAGMA foreign_keys = ON`) and the live store runs in WAL mode for
the writer's own benefit. Consumers never open that file. `plexdb publish` writes a consistent,
single-file copy with `VACUUM INTO` — no `-wal` or `-shm` sidecars — and every consumer opens
*that* file, read-only, including from a directory with no write permission
([ADR-0007](./adr/0007-readers-get-a-snapshot-not-the-live-store)).

## Version 2 — watch history

One row per Plex watch-history event ([ADR-0004](./adr/0004-the-store-owns-watch-history)),
written by `plexdb ingest-plays`.

```sql
CREATE TABLE plays (
    history_key       TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    plex_account_id   INTEGER NOT NULL,
    client_identifier TEXT,
    platform          TEXT,
    viewed_at         INTEGER NOT NULL,
    ip                TEXT,
    percent_complete  INTEGER,
    paused_counter    INTEGER
);

-- The incremental-fetch watermark, one row. See below for why it is not
-- simply MAX(plays.viewed_at).
CREATE TABLE plays_ingest_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    since_viewed_at INTEGER NOT NULL
);
```

`history_key` is Plex's own identity for the *viewing* (its `historyKey`, e.g.
`/status/sessions/history/58536`) — not to be confused with `plex_items.rating_key`, which is
the *title's* identity. It is what makes ingest idempotent: re-recording an event already on
file is `ON CONFLICT(history_key) DO NOTHING`, a no-op.

`item_id` resolves through `plex_items`, populated by `plexdb walk`. An event whose rating key
has no row there means the walk and the history have diverged — the ingest counts and reports
it, never drops it silently.

**Staying reachable until a walk catches up is why `plays_ingest_cursor` exists.** The obvious
watermark — the newest `viewed_at` already written to `plays` — breaks that promise: an
unresolved event is never written, so that watermark would advance past it the moment any
*newer* event resolved, and it would never be asked for again even after a later walk added its
rating key. `plays_ingest_cursor` instead tracks the oldest event a run could not resolve. As
long as anything is outstanding, the next run's cutoff stays pinned there, so that event — and
everything at or after it — is asked for again. Only once a run resolves everything does the
cursor advance, to the newest event that run saw. Plex's own `viewedAt>` history filter (used to
narrow the request server-side) is **inclusive** in practice despite its name — the boundary
event comes back on every run — which `ON CONFLICT` absorbs for free.

`plex_account_id` and `client_identifier`+`platform` come from Plex directly, the latter via a
join against `/devices` (cached per ingest — a play references a device far more often than the
device list changes). `ip`, `percent_complete`, and `paused_counter` are filled only by the
Tautulli adapter (issue #9) and are null on a Plex-only deployment — the normal state, not a
degraded one. Measured against a live server: of 268 plays finished at 90% or more, 261 appear
in Plex's own history; of 17 plays abandoned under 40%, only 3 do — so a Plex-only ingest
delivers positive signal only.

## Not yet built

```sql
edges(from_id, to_id, edge_type, rank, fetched_at)
collection_membership(collection_id, item_id, weight, source, observed_at)
```

Three rules that are easy to break by accident:

- **Namespaces are hard partitions.** A writer may wipe and rewrite only its own rows. Values
  are opaque — the store indexes and serves, it never interprets.
- **Edges are snapshots.** On re-pull, replace the whole `(from_id, edge_type)` set rather than
  appending; additions and removals then fall out on their own. Uniqueness is
  `(from_id, to_id, edge_type)`. Keep the source's rank — a #2 recommendation is not a #20.
- **Enrich once**, keyed by external id, with `fetched_at`. Never re-fetch a row that exists
  and is inside its threshold. Roughly 30–60 days for external sources; local edges recompute
  free on every sweep and never expire.

## Layer 2

No schema. A per-user weighted attribute vector is recomputed per generation pass and never
stored, because watch history changes constantly and invalidation is more work than
recomputation. Revisit only if profiling says otherwise.
