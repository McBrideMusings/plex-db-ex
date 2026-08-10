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

**Versions 1, 2, 3 and 4 are live.** Everything under "Not yet built" is the target for later
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

### Namespaces in use

| Namespace | Writer | Keys |
|---|---|---|
| `tmdb_keywords` | `plexdb enrich-tmdb-keywords` | `keyword` (one row per keyword, `value` is the keyword text) and a sentinel `_fetched` row (`value` = `"1"`) so a title with zero keywords still has a `fetched_at` to check staleness against. Only `items.type` `movie` and `show` are enriched — TMDB has no keywords endpoint for an episode. |
| `tmdb_edges` | `plexdb enrich-tmdb-edges` | Bookkeeping only, not relationship data — that lives in `edges` (below). `_fetched_recommendations` and `_fetched_similar` sentinel rows (`value` = `"1"`), one per title per edge type, so a title whose result was empty or entirely outside the library still has a `fetched_at` to check staleness against even though it left no `edges` row behind. |

`enrich-tmdb-keywords` re-fetches a title only once its row is older than `TMDB_KEYWORDS_STALE_DAYS`
(default 45 days). `--rewipe` deletes every `tmdb_keywords` row before a sweep, forcing a full
re-fetch, without touching any other namespace.

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
device list changes). `ip`, `percent_complete`, and `paused_counter` are filled only by
`plexdb enrich-tautulli-plays` (issue #9, Version 4 below) and are null on a Plex-only
deployment — the normal state, not a degraded one. Measured against a live server: of 268 plays
finished at 90% or more, 261 appear in Plex's own history; of 17 plays abandoned under 40%, only
3 do — so a Plex-only ingest delivers positive signal only.

## Version 3 — affinity edges

Directed, typed, ranked item-to-item relationships
([issue #6](https://github.com/McBrideMusings/plex-db-ex/issues/6)), written by
`plexdb enrich-tmdb-edges`.

```sql
CREATE TABLE edges (
    from_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    to_id      TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    edge_type  TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, edge_type)
);
```

**Edges are snapshots, not facts.** On re-pull, the whole `(from_id, edge_type)` set is deleted
and rewritten inside one transaction — a run interrupted partway through never leaves a
half-replaced set. Additions and removals fall out on their own: a title the source stopped
recommending simply isn't in the new set. `rank` is the source's own ordering, stored verbatim —
never collapsed to a boolean — so a #2 recommendation stays distinguishable from a #20.

**Both `from_id` and `to_id` reference `items`.** `items` is populated only by `plexdb walk`
([ADR-0005](./adr/0005-the-store-walks-plex-itself-and-augments-never-replaces)), so a
recommendation pointing at a title this library has never walked has no `item_id` to land on. Such
a result is dropped and counted, never stored under an invented id
([ADR-0009](./adr/0009-an-edge-only-connects-two-items-this-store-already-knows)).

### Edge types in use

| Edge type | Writer | Signal |
|---|---|---|
| `tmdb_recommendations` | `plexdb enrich-tmdb-edges` | TMDB's `/recommendations` endpoint — behavioural: "people who engaged with this also engaged with that". |
| `tmdb_similar` | `plexdb enrich-tmdb-edges` | TMDB's `/similar` endpoint — content-derived, not behavioural. Kept as a distinct type from `tmdb_recommendations` because the two measure different things. |

`enrich-tmdb-edges` re-fetches a title's edge set only once it is older than
`TMDB_EDGES_STALE_DAYS` (default 45 days, tracked separately from `TMDB_KEYWORDS_STALE_DAYS`).
`--rewipe` deletes every row of one run's edge types before a sweep, forcing a full re-fetch,
without touching any other edge type. The per-title fetch cursor this staleness check reads lives
in the `tmdb_edges` `enrichment` namespace, not in `edges` itself — a title whose result is empty,
or entirely outside the library, still needs a `fetched_at` to check next sweep, and `edges` alone
cannot carry one for a title with no rows.

## Version 4 — the Tautulli history adapter

Two columns added to the existing `plays` table ([issue #9](https://github.com/McBrideMusings/plex-db-ex/issues/9)),
written by `plexdb enrich-tautulli-plays` — an optional adapter, same as `ip` and
`percent_complete` above; the store still runs with neither Tautulli column populated.

```sql
ALTER TABLE plays ADD COLUMN seconds_watched INTEGER;
ALTER TABLE plays ADD COLUMN tautulli_id INTEGER;
CREATE UNIQUE INDEX idx_plays_tautulli_id ON plays(tautulli_id) WHERE tautulli_id IS NOT NULL;
```

**`seconds_watched` is Tautulli's `duration` copied verbatim.** `duration` is already net of
paused time — measured over 151 completed history rows, `stopped - started - duration -
paused_counter` lands within 0-2 seconds of zero, rounding only — so computing `duration -
paused_counter` would subtract pause a second time and roughly halve the footage on exactly the
paused-heavy rows this column exists to measure. There is no stored floor and no stored
completion flag: **the floor a play must clear to "count" is the reader's choice, made at query
time** (`WHERE seconds_watched >= 30`, or whatever threshold that reader wants), never baked into
this schema — a stored `counts_as_signal` boolean would freeze today's threshold into every row,
and changing it later would mean rewriting history. Do not re-add one.

**A Tautulli row finds its play by composite match, not a shared key.** Plex's `historyKey` has
no equivalent in Tautulli's `get_history` response, so `enrich-tautulli-plays` matches on
`rating_key` (resolved through `plex_items`, same as `ingest_plays`) + `plex_account_id` +
`client_identifier` as hard keys, then takes the play whose `viewed_at` is nearest the Tautulli
row's `stopped`, within ±900 seconds (measured against the live server: this window matched 145
of 151 matchable rows that had any candidate play at all; ±300s would have matched only 110). The
Tautulli row's own `id` is stored in `tautulli_id`, guarded by the partial unique index above — a
re-run is idempotent, and a play already carrying a different `tautulli_id` is never re-matched.
A Tautulli row with a null `id` is an in-progress session, not history, and is skipped.

## Not yet built

```sql
collection_membership(collection_id, item_id, weight, source, observed_at)
```

Two rules that are easy to break by accident:

- **Namespaces are hard partitions.** A writer may wipe and rewrite only its own rows. Values
  are opaque — the store indexes and serves, it never interprets.
- **Enrich once**, keyed by external id, with `fetched_at`. Never re-fetch a row that exists
  and is inside its threshold. Roughly 30–60 days for external sources; local edges recompute
  free on every sweep and never expire.

## Layer 2

No schema. A per-user weighted attribute vector is recomputed per generation pass and never
stored, because watch history changes constantly and invalidation is more work than
recomputation. Revisit only if profiling says otherwise.
