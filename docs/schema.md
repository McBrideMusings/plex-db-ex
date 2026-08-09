---
outline: deep
---

# Schema

The SQLite schema **is** the public API of this project. One process writes it; every consumer
opens the same file read-only. There is no version negotiation, so a schema change is a
breaking change for every consumer at once ([ADR-0001](./adr/0001-one-writer-many-readers-sqlite-file-is-the-interface)).

Nothing below is built yet. This page is the target for the first slice; it gets replaced with
the real DDL as tables land.

## Identity

Every table keys on `item_id` — the opaque, deterministic string described in
[ADR-0002](./adr/0002-item-id-is-the-entry-id-string), derived first-hit-wins from external
GUIDs: `imdb:tt1375666`, else `tmdb:…`, else `tvdb:…`, else `plex:…`, else `fs:<hash>`.

```sql
items(item_id, type, title, year, …)
external_ids(item_id, ns, value)          -- imdb, tmdb, tvdb, trakt, letterboxd, …
plex_items(item_id, rating_key, section_id, last_seen)
```

`external_ids` is what lets an enrichment fetcher that needs a TMDb id find one without
assuming the primary key is one. It is also where a Trakt slug, a Letterboxd URL, and a
resolved free-text Reddit title land.

## Layer 1 — the cached item graph

```sql
enrichment(item_id, namespace, key, value, fetched_at)
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
