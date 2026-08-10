# Roadmap

Tracked on [the issue backlog](https://github.com/McBrideMusings/plex-db-ex/issues); this page is
the shape, the issues are the work.

## Now

Milestone: **Identity and enrichment**. Nothing downstream is trustworthy until the two `item_id`
implementations agree on real data — [#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) is
the measurement that proves it, and the reason the rest of this milestone exists.

- [ ] [#1](https://github.com/McBrideMusings/plex-db-ex/issues/1) The store opens: package skeleton and schema v1
- [ ] [#2](https://github.com/McBrideMusings/plex-db-ex/issues/2) Derive `item_id`, and author the shared identity fixture
- [ ] [etv-station#180](https://github.com/McBrideMusings/etv-station/issues/180) `etv-station` adopts that fixture
- [ ] [#3](https://github.com/McBrideMusings/plex-db-ex/issues/3) Walk the Plex library into items and external ids
- [ ] [#4](https://github.com/McBrideMusings/plex-db-ex/issues/4) Fetch TMDB keywords into namespaced enrichment, cached
- [ ] [#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) Reconcile derived identities against `etv-station`'s catalog

## Next

Milestone: **Edges, history, and the first consumer**. Deliberately sketchy — what
[#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) finds changes several of their shapes.

- [ ] [#6](https://github.com/McBrideMusings/plex-db-ex/issues/6) Affinity edges from TMDB recommendations and similar
- [ ] [#7](https://github.com/McBrideMusings/plex-db-ex/issues/7) Local edges from Plex collection co-membership
- [ ] [#8](https://github.com/McBrideMusings/plex-db-ex/issues/8) Ingest watch history from Plex into `plays`
- [ ] [#9](https://github.com/McBrideMusings/plex-db-ex/issues/9) Tautulli history adapter: IP, completion, paused time
- [ ] [#10](https://github.com/McBrideMusings/plex-db-ex/issues/10) Cluster shared accounts into latent users by machine id
- [ ] [#11](https://github.com/McBrideMusings/plex-db-ex/issues/11) Rust reader crate and the `etv-station` capability grant
- [ ] [#12](https://github.com/McBrideMusings/plex-db-ex/issues/12) Trakt related edges — blocked on a credential only you can get

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
