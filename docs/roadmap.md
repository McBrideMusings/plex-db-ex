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
- [x] [#16](https://github.com/McBrideMusings/plex-db-ex/issues/16) Set the busy timeout explicitly instead of inheriting it
- [x] [#3](https://github.com/McBrideMusings/plex-db-ex/issues/3) Walk the Plex library into items and external ids
- [x] [#4](https://github.com/McBrideMusings/plex-db-ex/issues/4) Fetch TMDB keywords into namespaced enrichment, cached
- [x] [#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) Reconcile derived identities against `etv-station`'s catalog
- [x] [#23](https://github.com/McBrideMusings/plex-db-ex/issues/23) A movie and a TV show sharing a TMDB number became one title — schema v5 puts the media kind in the external-id key, and `plexdb repair-identities` splits the 1,407 already fused

## Next

Milestone: **Edges, history, and the first consumer**. Deliberately sketchy — what
[#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) finds changes several of their shapes.

- [x] [#6](https://github.com/McBrideMusings/plex-db-ex/issues/6) Affinity edges from TMDB recommendations and similar
- [x] [#7](https://github.com/McBrideMusings/plex-db-ex/issues/7) Local edges from Plex collection co-membership
- [x] [#8](https://github.com/McBrideMusings/plex-db-ex/issues/8) Ingest watch history from Plex into `plays`
- [x] [#9](https://github.com/McBrideMusings/plex-db-ex/issues/9) Tautulli history adapter: IP, completion, paused time — plus [#26](https://github.com/McBrideMusings/plex-db-ex/issues/26): the server owner's differing account id resolved so their plays match too
- [x] [#10](https://github.com/McBrideMusings/plex-db-ex/issues/10) Cluster shared accounts into latent users by machine id — plus [#25](https://github.com/McBrideMusings/plex-db-ex/issues/25): a keyword profile counts a show once, not once per episode
- [x] [#11](https://github.com/McBrideMusings/plex-db-ex/issues/11) Read-only Rust reader crate over the store — enrichment, edges, taste vector, read-only enforcement and the schema-version gate. The weighted collection-membership accessor is split out as [#29](https://github.com/McBrideMusings/plex-db-ex/issues/29), which waits on a `collection_membership` table that does not exist yet
- [ ] [etv-station#181](https://github.com/McBrideMusings/etv-station/issues/181) `etv-station` exposes the crate to plugins behind a capability grant
- [ ] [#12](https://github.com/McBrideMusings/plex-db-ex/issues/12) Trakt related edges

## Open questions

Tracked as `question` issues — decisions, never implemented from.

- [x] [#13](https://github.com/McBrideMusings/plex-db-ex/issues/13) Who owns the Layer 2 ranking knobs? Answered against real vectors over 25,835 plays: the half-life and the negative weight are not knobs (decay concentrates rather than mixes; abandonment is no signal, not negative signal), and the exploration fraction belongs to a channel, not to this store. The rollup ships no knobs to own — [ADR-0011](./adr/0011-a-taste-vector-weights-a-season-not-an-episode), implemented by [#32](https://github.com/McBrideMusings/plex-db-ex/issues/32).
- [ ] [#14](https://github.com/McBrideMusings/plex-db-ex/issues/14) Obtain a working Trakt client id — the inherited one is 43 characters and returns 403.
- [ ] [#17](https://github.com/McBrideMusings/plex-db-ex/issues/17) Should an empty GUID value be treated as absent? Changes what an `item_id` is, so it must land in both repos at once.

## Later

- [ ] `collection_membership`: crowd lists and subreddit mention harvesting — unblocks [#29](https://github.com/McBrideMusings/plex-db-ex/issues/29)
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
