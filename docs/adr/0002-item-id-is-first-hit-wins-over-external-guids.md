# `item_id` is first-hit-wins over external GUIDs, with a path hash as the floor

Every table keys on `item_id TEXT`: an opaque, deterministic string derived from the ids Plex
already reports for a title, taken first-hit-wins in a fixed order — `imdb:` → `tmdb:` → `tvdb:`
→ `plex:` — falling back to `fs:<fnv1a hex of canonical path>` when a title carries none of them.
A separate `external_ids(item_id, ns, value)` table carries every other id a title is known by, so
a fetcher that needs a TMDb id to call the keywords endpoint looks it up rather than assuming the
primary key is one.

## Why

Identity has to be derivable from the title alone, by anything holding the same Plex record, with
no lookup and no shared state. That rules out an id this store invents — a serial, a UUID —
because nothing else could reproduce it, and any process that wanted to talk about a title would
first have to ask this store what it decided.

**Fixed priority, not "whichever id we saw first".** The order *is* the rule. A title carrying
both an IMDb and a TMDb id is `imdb:…`, always, however the ids arrived or in whatever order a
walk happened to read them. Ordering by arrival makes the id depend on the traversal, so the same
library walked twice can produce two different ids for one title.

**A path hash is the floor, not a tier anyone should reach.** A title with no external id has
nothing but its file to be named by. Hashing the canonical path gives it a stable name, at the
cost that the name is only as stable as the path — which is why the path is canonicalised against
the configured source roots before hashing, and why `walk` refuses to run when those roots are
empty (see the amendment below).

## Considered options

- **The TMDb id as `item_id`.** Rejected: TVDB-only shows and the anime mappings have no TMDb id
  at all, so they would be unaddressable, and every read would pay a resolution step.
- **No canonical id — key every table on `(id_ns, id_value)`.** Rejected: a reader holding
  `imdb:tt1375666` cannot find rows a writer filed under `tmdb:27205`, so one title silently
  becomes two.
- **A serial or UUID assigned by this store.** Rejected: not derivable, so nothing can name a
  title without first asking this store, and the id means nothing outside this file.

## Consequences

The derivation rule is a published part of the schema, not an implementation detail. It is
specified by `tests/fixtures/item_id.json` — the table of GUID sets, paths, and the `item_id` each
must produce — which this store's own suite runs against (ADR-0006). Anything that wants to derive
the same ids reads that fixture; nothing in this repository tracks who does.

`external_ids` is also where a Trakt slug, a Letterboxd URL, and a resolved free-text title land.

## Amendment: an empty or whitespace-only GUID value is absent

A GUID value that is empty or whitespace-only is not an identity — `imdb:` is the absence of one
wearing a prefix, not a value. Derivation skips such a value, considers the next namespace in
priority order, and reaches the path-hash fallback when nothing usable remains. A value that is
otherwise usable is still used verbatim: surrounding whitespace is not trimmed, so `" tt1375666 "`
and `"tt1375666"` remain distinct ids
([issue #17](https://github.com/McBrideMusings/plex-db-ex/issues/17)).

Before this amendment, a present-but-empty GUID formatted straight through (`imdb:`), so two
unrelated titles that both carried an empty IMDb GUID collided onto the same `item_id`.

## Amendment: an empty source root is a refusal, not a default

The path hash is taken over the canonical path — the playback path with the configured source
roots stripped. With no roots configured the whole absolute path is hashed, so the id encodes
where somebody mounted the disk, and two correct installs pointed at the same Plex server disagree
about what a title is called.

1,521 titles were affected before this was caught, every one of them a title with no external id
at all — exactly the set that has nothing but the path to identify it
([issue #24](https://github.com/McBrideMusings/plex-db-ex/issues/24)). `plexdb walk` now raises a
`ConfigError` naming `PLEX_SOURCE_ROOTS` rather than proceeding with an empty list. An absent root
is a startup error instead of ids nobody sees are wrong.
