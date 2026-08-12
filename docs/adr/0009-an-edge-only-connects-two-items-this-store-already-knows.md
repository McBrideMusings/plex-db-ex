# An edge only connects two items this store already knows

`edges.from_id` and `edges.to_id` both `REFERENCES items(item_id)`. A source's recommendation or
similar-title result that points at a title this store has never walked is counted
(`edges_skipped_not_in_library`) and dropped — never inserted under an invented id, and never
stored in a form that bypasses the foreign key.

## Why this needs a ruling

TMDB's `/recommendations` and `/similar` endpoints return titles freely, with no notion of what a
given Plex library owns. A real request against a title with 20 recommendations routinely returns
several this library has never walked. `items` is populated exclusively by `plexdb walk`
([ADR-0005](./0005-the-store-walks-plex-itself-and-augments-never-replaces)) — nothing else may
insert a row there — so a recommended title with no walked counterpart has no `item_id` the schema
recognises. Something has to give: enforce the foreign key and drop the edge, or represent the
target some other way.

## Considered options

- **Enforce the foreign key on both columns; drop an edge whose target isn't walked yet.**
  Chosen. Costs real signal — a library that owns little of what TMDB recommends stores few edges
  — but every edge the store does hold points at something a consumer can actually look up, act
  on, or schedule. Nothing downstream has to guard against a target it cannot resolve.
- **Invent an id for the unwalked target, e.g. `tmdb:<id>`, and store the edge anyway.** Rejected.
  `item_id` is derived first-hit-wins over GUIDs in a fixed priority order
  ([ADR-0002](./0002-item-id-is-first-hit-wins-over-external-guids)): `imdb:` beats `tmdb:`. A recommendation
  response carries only a TMDB id, so an invented id would always land on the weaker `tmdb:` form.
  If that title is later walked and carries an IMDb GUID, the walk derives `imdb:…` for it — a
  *different* string from the edge already on file. The edge becomes an orphan under an id nothing
  else ever produces again, invisible to every table keyed the normal way. This is the exact
  identity-drift ADR-0002's ordering and ADR-0008's repoint-avoidance exist to prevent, reintroduced
  at the one join this store cannot see coming, since nothing walks in reaction to an edge sweep.
- **Add a stub-item or candidate-title table for something nobody owns yet.** Rejected for this
  slice: it is a second identity space with its own reconciliation problem — the moment a stub
  title is actually walked, something has to notice and merge the two rows, and nothing in this
  design owns that merge. A real want-to-acquire feature may need this, but issue #6 is proving the
  edge model, not designing acquisition tracking.

## Consequences

An edge sweep undercounts what a source actually offered whenever the library is a small slice of
what that source knows about — expected, not a bug, and worth surfacing in a sweep's summary
(`edges_skipped_not_in_library`) so an operator can see how much a run left on the table rather than
wondering why edge counts look thin. A title later walked into the library does not retroactively
gain the edges an earlier sweep skipped for it — the next staleness-driven re-fetch does, the same
way any other TMDB data here catches up on a schedule rather than by reacting to the walk.
