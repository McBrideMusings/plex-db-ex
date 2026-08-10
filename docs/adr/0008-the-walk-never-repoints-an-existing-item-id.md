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
  than silently forking"), and counted (`identity_kept_on_guid_change`) rather than silent.

## Consequences

An `item_id` can stop matching what `derive_item_id` would produce from a title's *current* GUID
set — it reflects the GUID set at first walk, not necessarily the latest one. `external_ids`
remains the right table to search a title by any GUID it has ever carried; `items.item_id` is not
guaranteed to be the "best" id a fresh derivation would produce after a re-match. Reconciling this
store's ids against another source of truth (`etv-station`'s catalog,
[#5](https://github.com/McBrideMusings/plex-db-ex/issues/5)) will need to account for that rather
than assume `item_id` always equals a fresh derivation.
