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

## Amendment: an empty or whitespace-only GUID value is absent

A GUID value that is empty or whitespace-only is not an identity — `imdb:` is the absence of
one wearing a prefix, not a value. Derivation skips such a value, considers the next namespace
in priority order, and reaches the path-hash fallback when nothing usable remains. A value that
is otherwise usable is still used verbatim: surrounding whitespace is not trimmed, so
`" tt1375666 "` and `"tt1375666"` remain distinct ids
([issue #17](https://github.com/McBrideMusings/plex-db-ex/issues/17)).

Before this amendment, a present-but-empty GUID formatted straight through (`imdb:`), so two
unrelated titles that both carried an empty IMDb GUID collided onto the same `item_id`.

**The two implementations are knowingly divergent until `etv-station` catches up.** This repo
applies the rule as of `plexdb/identity.py`; the Rust half is
[etv-station#184](https://github.com/McBrideMusings/etv-station/issues/184) and has not landed.
The shared fixture (ADR-0006) has moved here, so that repo's recorded hash is now stale by
design — the guard going red is the signal to land the other half, not a fault. Nothing has
walked a real library yet, so no persisted id is affected by the gap.
