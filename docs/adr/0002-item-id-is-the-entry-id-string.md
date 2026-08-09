# `item_id` is etv-station's `entry_id` string, with a side table of external ids

Every table keys on `item_id TEXT` — the opaque, deterministic id
`etv-station` already derives in `crates/etv-station/src/catalog/identity.rs`:
first-hit-wins over external GUIDs (`imdb:` → `tmdb:` → `tvdb:` → `plex:`), falling back
to `fs:<fnv1a hex of canonical path>`. A separate `external_ids(item_id, ns, value)` table
carries every other id a title is known by, so a fetcher that needs a TMDb id to call the
keywords endpoint looks it up rather than assuming the primary key is one.

## Considered options

- **The TMDb id as `item_id`.** This is what `curator` does today — `keyword_cache` is
  keyed `(tmdb_id, media_type)` and `candidate_members` is documented "TMDb IDs only".
  Rejected: TVDB-only shows and the anime mappings have no TMDb id at all, so they would
  be unaddressable, and `etv-station` would resolve on every read.
- **No canonical id — key every table on `(id_ns, id_value)`.** Rejected: a reader holding
  `imdb:tt1375666` cannot find rows a writer filed under `tmdb:27205`, so one title
  silently becomes two.

## Consequences

The derivation rule now exists in two languages. Python must reproduce the Rust
first-hit-wins order exactly, or the two processes disagree about what a title is called
and the join silently returns nothing. Both repos test against one shared fixture — a
table of GUID sets and their expected id — so a change to the priority order fails on both
sides instead of one.

`external_ids` is also where a Trakt slug, a Letterboxd URL, and a resolved Reddit free-text
title land, which is the mapping `curator/resolve/` maintains today.
