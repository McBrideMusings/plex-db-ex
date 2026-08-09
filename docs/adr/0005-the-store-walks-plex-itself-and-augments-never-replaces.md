# The store walks Plex itself, and augments Plex rather than replacing it

The store's only required dependency is a Plex server. It walks the library sections through
the Plex API and writes its own `items` rows, `external_ids`, and a `plex_rating_key → item_id`
map, so `plexdb.db` answers every question from one file without reaching into any consumer's
database.

The posture this sets for consumers: a tool talks to Plex directly for everything Plex can
answer, and reaches this store only for what Plex cannot carry. Where that is impossible, the
tool depends on both a Plex instance and a plex-db-ex instance — never on this store alone,
and never on this store as a stand-in for Plex.

## Why the store cannot skip its own walk

Watch history — from either source — identifies a title by Plex `ratingKey`, and its `guid`
field is the Plex-internal agent GUID (`plex://episode/697f42060df4b2030bb3fe74`), which is the
weakest tier in ADR-0002's derivation order. The external GUIDs an `item_id` is actually built
from only exist where somebody has walked the library. Today that is `etv-station`'s
`catalog.db`, written by `crates/etv-station/src/catalog/ingest/plex.rs`.

## Considered options

- **Resolve `rating_key` against `etv-station`'s `catalog.db` at read time.** Rejected: the
  store stops standing alone, the Python writer can compute nothing item-level, and `curator` —
  also Python — cannot use `plays` at all.
- **`etv-station` writes its map into `plexdb.db` after each ingest.** Rejected: it breaks
  ADR-0001's single-writer rule immediately.
- **The store becomes *the* library catalog, and `etv-station` drops its own ingest and reads
  `entries` from here.** This is the only option that removes the underlying condition — one
  walker, one identity derivation, one file — and it is rejected on posture, not cost: this
  store augments Plex, so a consumer must be able to talk to Plex directly without it.

## Consequences

Three independent walkers now cross the same library — `etv-station`'s ingest, `curator`'s
overlap gate, and this one. That duplication is accepted deliberately as the price of each
tool depending on Plex rather than on each other.

Because §7's write-back projection also needs a Plex API client, the dependency this ADR adds
was already required by the design.
