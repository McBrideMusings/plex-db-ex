# The walk never repoints an existing `item_id` when a title's GUID set changes

When `plexdb walk` sees a Plex rating key it has walked before, and this pass's GUID set for
that record derives a different `item_id` than the one already recorded against it, the walk
keeps the existing identity. The freshly-seen external ids are still recorded under it — so the
title stays reachable by every GUID it has ever carried — but `item_id` itself does not move.

## Why

`item_id` is meant to be stable for the lifetime of a title in the store: every table downstream
keys on it, and there is no migration path for a changed identity ([`docs/schema.md`](../schema)).
Plex re-matching a title — a fresh scrape correcting a wrong TVDB match, or a title gaining an
IMDb id it previously lacked — is ordinary behaviour on a real library, not a rare edge case.
Re-deriving `item_id` from scratch on every walk would leave the old `items`/`external_ids` rows
behind as an orphaned second row for the same physical title the moment a GUID set changes — the
silent fork issue [#3](https://github.com/McBrideMusings/plex-db-ex/issues/3) required the walk
not to produce.

## Considered options

- **Always re-derive, and let the id move.** Rejected: this is the silent fork issue #3 forbids —
  the old `item_id` row keeps existing, addressed by nothing, and anything recorded against it
  (enrichment, edges, once those tables exist) goes stale with no signal that it happened.
- **Refuse the walk and require a person to reconcile.** Rejected for this slice: Plex re-matching
  is routine enough that halting the whole walk over one title would make unattended runs
  impractical, and nothing about the store's single-writer design gives a person a place to make
  that call today.
- **Keep the existing identity; still record every GUID seen.** Chosen: no `item_id` ever moves
  once assigned, and every external id a title has ever carried through this store stays
  searchable — satisfying both halves of issue #3's requirement ("keeps its identity ... rather
  than silently forking"), and named in the walk's own output (`WalkStats.identities_kept`, each
  entry carrying the title, the id kept and the id that would have been derived) rather than
  silent.

## Amendment: an existing identity is found by external id first, then by rating key

The original rule looked up only the **rating key**. `etv-station` refuses to repoint too, but
looks up **each external id** in turn (`crates/etv-station/src/catalog/ingest/plex.rs`,
`resolve_existing`), falling back to a path match. Same intent, different key — and they disagree
in two real situations:

- **A re-match replaces the GUID set wholesale, rating key unchanged.** Keying on the rating key
  keeps the identity; keying on external ids does not, and mints a new one.
- **A remove-and-re-add changes the rating key, GUIDs intact.** Keying on external ids keeps the
  identity; keying on the rating key does not, and leaves no record that the two rating keys are
  the same title.

Neither raises. The symptom is a row addressed by an id nothing looks up any more — the failure
ADR-0002 and its fixture exist to prevent, one level up, in storage policy rather than
derivation. The fixture cannot catch it: every case in it still passes.

**Resolved: try both, external id first, then rating key.** External id is the stronger signal and
survives rating-key churn; the rating key catches the wholesale re-match that external ids miss.
This is a strict superset of `etv-station`'s rule, so the two agree everywhere that repo has an
opinion, and this store simply retains identity in one case it does not.

Matching on external id means two Plex rating keys sharing an external id resolve to one identity.
That is already this store's behaviour — a title with two copies in the library already maps two
rating keys to one `item_id` — so this extends an existing property rather than introducing one.

## Second amendment: an external id only matches within its own media kind

The paragraph directly above is where this went wrong. "Two Plex rating keys sharing an external
id" is only the same title twice when the shared id is unique across everything Plex holds. **TMDB
and TVDB ids are not.** Both sources number movies and TV shows in separate lists that start at 1,
and Plex reports each as a bare `tmdb://1678` with no media type attached. TMDB movie 1678 is
*Godzilla* (1954); TMDB show 1678 is *The Golden Girls* (1985). Two unrelated records, one number.

`external_ids` was keyed on `(ns, value)`, so both claimed the same row, `_resolve_existing` read
that as "seen before", and the rule above — correctly, given what it was told — kept the movie's
identity for the show. On the author's library 1,414 identities had fused that way, 1,326 of them
holding two different IMDb ids, with 553 plays landing on them
([issue #23](https://github.com/McBrideMusings/plex-db-ex/issues/23)).

**Resolved: the media kind is part of the external-id key.** Schema version 5 keys `external_ids`
on `(ns, value, kind)` and `walk._resolve_existing` matches within a kind only. Godzilla claims
`('tmdb', '1678', 'movie')`, The Golden Girls claims `('tmdb', '1678', 'show')`. All three kinds
are distinguished, not just movie-vs-show: TVDB numbers episodes in a list separate from series.

The never-repoint rule itself is untouched, and the merge #19 asked for still happens — two copies
of *the same* title in two library sections share a kind, so they still resolve to one identity.
What stops is merging across kinds, which was never the same title at all.

Identities already fused cannot be undone by re-walking: both rating keys are in `plex_items`
pointing at the fused id, so the walk finds it by rating key and this ADR's rule keeps it.
`plexdb repair-identities` deletes them and re-derives from Plex instead.

## Third amendment: a title reporting fewer GUIDs than before is now observable, and still keeps its identity

Every case above is about a title's GUID set *changing shape* — gaining an id, or a wholesale
re-match. None of them cover a title reporting **fewer** ids than a prior walk recorded, with the
id `item_id` was derived from untouched. That case reached this store on 12 August 2026: `plexdb
walk` printed 9 titles whose stored `item_id` disagreed with a fresh derivation
([issue #56](https://github.com/McBrideMusings/plex-db-ex/issues/56)), and five were not
explainable from anything `walk` recorded — each had its previously-strongest GUID vanish from
Plex's report, leaving only the tier below, with nothing to say whether Plex genuinely dropped the
match or one fetch simply came back short.

**Resolved: this store does not act on a dropped GUID, only makes it visible.** `external_ids`
gains `last_seen`, stamped by every walk that observes a row (schema v8, issue #57). An id a walk
no longer reports keeps its prior `last_seen`; every id it does report gets the current walk's
timestamp. Two walks now distinguish "Plex stopped publishing this id" from "Plex published it
once and we kept it" — a fact this store previously could not represent at all, since every id sat
in the table identically regardless of how recently Plex had actually reported it.

**The never-repoint rule itself is unaffected.** `_resolve_existing` still finds the title by
whichever of its ids it does have (identity.PRIORITY order), so a title that drops its strongest
GUID keeps the `item_id` already assigned — it does not fork, and it does not re-derive. `walk`'s
disagreement count (the trigger for issue #56) is still printed exactly as before; `last_seen`
gives a person investigating that count a way to tell a genuine drop from a transient miss without
guessing, but changes nothing about which `item_id` a title is assigned. Whether a store should
ever *act* on a stale id — expiring it, or re-deriving `item_id` once one is missing long enough —
is a separate decision, deliberately out of scope here.

## Consequences

An `item_id` can stop matching what `derive_item_id` would produce from a title's *current* GUID
set — it reflects the GUID set at first walk, not necessarily the latest one. `external_ids`
remains the right table to search a title by any GUID it has ever carried; `items.item_id` is not
guaranteed to be the "best" id a fresh derivation would produce after a re-match. **So an
`item_id` in this store is not always what ADR-0002's rule would return today**, and anything
checking the two against each other has to allow for it rather than treat a difference as
derivation drift.

An `item_id` can also cover more than one Plex rating key. After the second amendment those are
only genuine duplicates — one title present in two library sections — but the divergence is real,
and a reader that keys strictly per rating key will count rows differently. Both facts are
consequences of retaining identity, not defects, and both belong in the schema documentation so a
reader can plan for them.
