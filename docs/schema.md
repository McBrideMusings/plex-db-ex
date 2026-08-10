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

**Version 1 is live.** Everything below it is the target for later slices.

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

Foreign keys are enforced (`PRAGMA foreign_keys = ON`) and the store runs in WAL mode, so a
consumer can read while the writer is mid-sweep.

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

## Watch history

```sql
plays(item_id, plex_account_id, client_identifier, platform, viewed_at,
      ip, percent_complete, paused_counter)
```

The first five columns come from Plex and are always present. The last three come only from the
Tautulli adapter and are null in a Plex-only deployment
([ADR-0004](./adr/0004-the-store-owns-watch-history)) — so a scorer must treat missing
completion data as normal, not as an error.

## Layer 2

No schema. A per-user weighted attribute vector is recomputed per generation pass and never
stored, because watch history changes constantly and invalidation is more work than
recomputation. Revisit only if profiling says otherwise.
