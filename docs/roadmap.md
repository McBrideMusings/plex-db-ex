# Roadmap

Tracked on [the issue backlog](https://github.com/McBrideMusings/plex-db-ex/issues); this page is
the shape, the issues are the work.

## Now

Milestone: **Identity and enrichment**. Nothing downstream is trustworthy until the two `item_id`
implementations agree on real data — [#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) is
the measurement that proves it, and the reason the rest of this milestone exists.

- [x] [#1](https://github.com/McBrideMusings/plex-db-ex/issues/1) The store opens: package skeleton and schema v1
- [x] [#2](https://github.com/McBrideMusings/plex-db-ex/issues/2) Derive `item_id`, and publish the identity fixture that specifies the rule
- [x] [#18](https://github.com/McBrideMusings/plex-db-ex/issues/18) Treat an empty GUID value as absent
- [x] [#15](https://github.com/McBrideMusings/plex-db-ex/issues/15) Publish a read-only snapshot for consumers
- [x] [#16](https://github.com/McBrideMusings/plex-db-ex/issues/16) Set the busy timeout explicitly instead of inheriting it
- [x] [#3](https://github.com/McBrideMusings/plex-db-ex/issues/3) Walk the Plex library into items and external ids
- [x] [#4](https://github.com/McBrideMusings/plex-db-ex/issues/4) Fetch TMDB keywords into namespaced enrichment, cached
- [x] [#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) Reconcile derived identities against another store's — built here, then removed: a writer opening a consumer's database runs ADR-0001's arrow backwards. The check belongs to whoever reads this store, and is refiled as [etv-station#269](https://github.com/McBrideMusings/etv-station/issues/269)
- [x] [#23](https://github.com/McBrideMusings/plex-db-ex/issues/23) A movie and a TV show sharing a TMDB number became one title — schema v5 puts the media kind in the external-id key, and `plexdb repair-identities` splits the 1,407 already fused

## Next

Milestone: **Edges, history, and the first consumer**. Deliberately sketchy — what
[#5](https://github.com/McBrideMusings/plex-db-ex/issues/5) finds changes several of their shapes.

- [x] [#6](https://github.com/McBrideMusings/plex-db-ex/issues/6) Affinity edges from TMDB recommendations and similar
- [x] [#7](https://github.com/McBrideMusings/plex-db-ex/issues/7) Local edges from Plex collection co-membership
- [x] [#8](https://github.com/McBrideMusings/plex-db-ex/issues/8) Ingest watch history from Plex into `plays`
- [x] [#9](https://github.com/McBrideMusings/plex-db-ex/issues/9) Tautulli history adapter: IP, completion, paused time — plus [#26](https://github.com/McBrideMusings/plex-db-ex/issues/26): the server owner's differing account id resolved so their plays match too
- [x] [#10](https://github.com/McBrideMusings/plex-db-ex/issues/10) Cluster shared accounts into latent users by machine id — plus [#25](https://github.com/McBrideMusings/plex-db-ex/issues/25): a keyword profile counts a show once, not once per episode
- [x] [#11](https://github.com/McBrideMusings/plex-db-ex/issues/11) Read-only Rust reader crate over the store — enrichment, edges, taste vector, read-only enforcement and the schema-version gate. The collection-membership accessor was split out as [#29](https://github.com/McBrideMusings/plex-db-ex/issues/29)
- [ ] [etv-station#181](https://github.com/McBrideMusings/etv-station/issues/181) `etv-station` exposes the crate to plugins behind a capability grant
- [x] ~~[#12](https://github.com/McBrideMusings/plex-db-ex/issues/12) Trakt related edges~~ — **dropped.** Trakt gated API application creation behind VIP in August 2026 and deleted existing applications without notice, so the credential cannot be obtained. A third opinion on affinity is not worth a subscription when `tmdb_recommendations` and `tmdb_similar` already ship
- [x] [#42](https://github.com/McBrideMusings/plex-db-ex/issues/42) `edges` had 0 rows — the sweep built by #6 had never been run. It has now: 12,961 titles fetched, 120,941 `tmdb_recommendations` and 113,855 `tmdb_similar` edges, 0 failures over 25,922 calls. **Only one of the two orderings means anything** — measured, not eyeballed: `tmdb_recommendations` decays threefold in keyword overlap from rank 1 to rank 20, while `tmdb_similar` is flat (0.0416 → 0.0450), so its twentieth answer is as good as its first. Both beat a random pair by 12–17×, so `similar` is an unordered bucket rather than noise. Tracked as [#52](https://github.com/McBrideMusings/plex-db-ex/issues/52), which blocks nothing but constrains [etv-station#176](https://github.com/McBrideMusings/etv-station/issues/176): a nearest-neighbour walk over `tmdb_similar` is a coin flip

## Open questions

Tracked as `question` issues — decisions, never implemented from.

- [x] [#13](https://github.com/McBrideMusings/plex-db-ex/issues/13) Who owns the Layer 2 ranking knobs? Answered against real vectors over 25,835 plays: the half-life and the negative weight are not knobs (decay concentrates rather than mixes; abandonment is no signal, not negative signal), and the exploration fraction belongs to a channel, not to this store. The rollup ships no knobs to own — [ADR-0011](./adr/0011-a-taste-vector-weights-a-season-not-an-episode), implemented by [#32](https://github.com/McBrideMusings/plex-db-ex/issues/32).
- [x] [#14](https://github.com/McBrideMusings/plex-db-ex/issues/14) Obtain a working Trakt client id — closed, not answerable. The inherited id was a real, formerly working one; Trakt deleted existing API applications without notice around 1–2 August 2026 and gated new ones behind VIP, calling it temporary with no date. Nothing is built from it now that #12 and #36 are dropped. (`403` from Trakt means "invalid API key or unapproved app" — not a VIP gate, which is `426`.)
- [ ] [#17](https://github.com/McBrideMusings/plex-db-ex/issues/17) Should an empty GUID value be treated as absent? Changes what an `item_id` is, so it must land in both repos at once.

## Later

**Running it unattended.** The store's sources and its consumer are both on the Unraid host; the
Mac is in the path only because that is where the code was written.

- [x] [#45](https://github.com/McBrideMusings/plex-db-ex/issues/45) `plexdb sweep` — one command runs the whole pass, each step declaring whether its failure ends the run ([ADR-0014](./adr/0014-the-sweep-is-a-command-not-a-shell-script))
- [x] [#43](https://github.com/McBrideMusings/plex-db-ex/issues/43) A container image whose entrypoint loops on a wall-clock schedule, so two sweeps cannot overlap
- [x] [#47](https://github.com/McBrideMusings/plex-db-ex/issues/47) Installed on the host and running unattended: `plays` 25,880 and `enrichment` 132,341 after the first container sweep, the snapshot published to the station's own directory, and no run-config drift between the live container and `[docker_run]`
- [x] [#48](https://github.com/McBrideMusings/plex-db-ex/issues/48) Plex collections are not harvested at all. The pairwise `local_collection` edge shape wrote 17.8M rows and a 3.7 GB snapshot; rather than reshape it, the source was dropped — a collection assembled by hand is a similarity its author already knows about, so feeding it back as a recommendation signal is circular

- [x] [#34](https://github.com/McBrideMusings/plex-db-ex/issues/34) MDBList crowd lists — schema v6's `collection` and `collection_membership`, filled by `plexdb harvest-mdblist`. Measured on the author's library: 50 lists, 11,896 memberships, 15,237 entries dropped as outside it
- [x] [#46](https://github.com/McBrideMusings/plex-db-ex/issues/46) The Gated Source contract — a source declares its client, its key and its write; the credential check, the derived staleness window and the two flags stop being copied per source
- [ ] The rest of the crowd-list sources, one at a time, each a new `source` value rather than a schema change: [#35](https://github.com/McBrideMusings/plex-db-ex/issues/35) Letterboxd, [#37](https://github.com/McBrideMusings/plex-db-ex/issues/37) editorial/RSS, [#38](https://github.com/McBrideMusings/plex-db-ex/issues/38) Reddit. [#36](https://github.com/McBrideMusings/plex-db-ex/issues/36) Trakt is dropped for the same reason as #12
- [x] [#29](https://github.com/McBrideMusings/plex-db-ex/issues/29) The reader crate's collection-membership accessor — `Reader::collections_for(item_id)` joins `collection_membership` with `collection`, every source-nullable field staying `Option<_>`
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
