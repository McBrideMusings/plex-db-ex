# Roadmap

## Now

First slice — identity and enrichment, end to end. Nothing downstream is trustworthy until the
two `item_id` implementations agree on real data.

- [ ] Python package skeleton (`plexdb/`), `pyproject.toml` under uv, ruff + mypy + pytest
- [ ] SQLite schema v1: `items`, `external_ids`, `enrichment`, `plex_rating_key` map
- [ ] `entry_id` derivation in Python, first-hit-wins `imdb:` → `tmdb:` → `tvdb:` → `plex:` → `fs:`
- [ ] Shared GUID→id fixture, tested here and in `etv-station`, so the two cannot drift silently
- [ ] Plex library walk over all four sections, writing `items` + `external_ids`
- [ ] TMDB keywords fetch keyed by external id, with `fetched_at` and a staleness threshold
- [ ] Reconcile against `etv-station`'s `catalog.db`: how many titles derive the same id, and why any don't

## Next

- [ ] `edges` table and the replace-wholesale refresh rule
- [ ] TMDB recommendations + similar; Trakt related (needs a working `TRAKT_CLIENT_ID`)
- [ ] Local Plex collection co-membership, recomputed every sweep
- [ ] `plays` ingest from Plex history; Tautulli adapter for IP, completion, paused time
- [ ] Fingerprint clustering over `machine_id`, and whether it splits the shared accounts
- [ ] Rust reader crate + the `etv-station` capability grant, replacing `taste-engine.rhai`

## Later

- [ ] `collection_membership`: crowd lists and subreddit mention harvesting
- [ ] Wikidata awards
- [ ] Write-back projection to Plex labels, prefix-namespaced and booleanised
- [ ] Local embeddings over overviews
- [ ] Absorb `curator`'s acquisition code, retiring its `keyword_cache` and `resolution_cache`

## Deferred

- [ ] Time-of-day as a fingerprint tier — no device here serves two different people
- [ ] IMDb "more like this" — no free official API, scraping is against terms
- [ ] Persisting Layer 2 — recompute until profiling says otherwise
- [ ] Folding `etv-station`'s `catalog.db` into this store — rejected on posture: this augments
      Plex, so a consumer must be able to talk to Plex directly without it

## Open questions

- [ ] Where `plexdb.db` physically lives, and how each consumer reaches it. SQLite over a
      network share is unsafe, so readers and the writer share a host or readers get a copy.
- [ ] How a breaking schema change rolls out, given readers have no version negotiation.
- [ ] Who owns the Layer 2 policy knobs — recency half-life, exploration fraction,
      negative-signal weight.
