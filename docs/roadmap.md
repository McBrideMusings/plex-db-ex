# Roadmap

Tracked on [the issue backlog](https://github.com/McBrideMusings/plex-db-ex/issues); this page is
the shape, the issues are the work.

## Now

Milestone: **Identity and enrichment**. Nothing downstream is trustworthy until the two `item_id`
implementations agree on real data — [#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) is
the measurement that proves it, and the reason the rest of this milestone exists.

- [x] [#1](https://github.com/McBrideMusings/plex-db-ex/issues/1) The store opens: package skeleton and schema v1
- [x] [#2](https://github.com/McBrideMusings/plex-db-ex/issues/2) Derive `item_id`, and author the shared identity fixture
- [ ] [etv-station#180](https://github.com/McBrideMusings/etv-station/issues/180) `etv-station` adopts that fixture
- [x] [#18](https://github.com/McBrideMusings/plex-db-ex/issues/18) Treat an empty GUID value as absent — Python half; [etv-station#184](https://github.com/McBrideMusings/etv-station/issues/184) is the Rust half and is still open
- [x] [#15](https://github.com/McBrideMusings/plex-db-ex/issues/15) Publish a read-only snapshot for consumers
- [ ] [#16](https://github.com/McBrideMusings/plex-db-ex/issues/16) Give connections a busy timeout
- [x] [#3](https://github.com/McBrideMusings/plex-db-ex/issues/3) Walk the Plex library into items and external ids
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
- [ ] [#11](https://github.com/McBrideMusings/plex-db-ex/issues/11) Read-only Rust reader crate over the store
- [ ] [etv-station#181](https://github.com/McBrideMusings/etv-station/issues/181) `etv-station` exposes the crate to plugins behind a capability grant
- [ ] [#12](https://github.com/McBrideMusings/plex-db-ex/issues/12) Trakt related edges

## Open questions

Tracked as `question` issues — decisions, never implemented from.

- [ ] [#13](https://github.com/McBrideMusings/plex-db-ex/issues/13) Who owns the Layer 2 ranking knobs — recency half-life, exploration fraction, negative-signal weight? Not answerable until a real taste vector exists.
- [ ] [#14](https://github.com/McBrideMusings/plex-db-ex/issues/14) Obtain a working Trakt client id — the inherited one is 43 characters and returns 403.
- [ ] [#17](https://github.com/McBrideMusings/plex-db-ex/issues/17) Should an empty GUID value be treated as absent? Changes what an `item_id` is, so it must land in both repos at once.

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

## Untracked questions

Not yet sharp enough to file.

- [ ] How a breaking schema change rolls out, given readers have no version negotiation.

Settled since: *where `plexdb.db` lives and how consumers reach it* — readers open a published
snapshot, never the live file ([ADR-0007](./adr/0007-readers-get-a-snapshot-not-the-live-store.md)),
which makes a copy to another host safe.
