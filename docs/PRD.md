---
outline: deep
---

# plex-db-ex — Extended Plex Metadata & Affinity Store

::: info Where this document stands
This is the original spec, kept as written. Five things in it have since been decided or
corrected against the running systems — see [Decisions](./adr/) — and each is flagged inline
below. Where this document and an ADR disagree, the ADR wins.
:::

Separate repo. A queryable metadata/affinity graph layered over a Plex library, built to be consumed by more than one project.

**Known consumers:** `etv-station` (channel scheduling, via plugins only) and the collection-generation project. Neither should reimplement this.

**Consumption rule:** `etv-station` core never links this library. Any channel that needs it reaches it through a `PoolProvider` or `Sequencer` plugin with an explicitly granted datastore capability. See ETV-000.

::: warning Superseded by [ADR-0003](./adr/0003-rust-reader-crate-behind-a-plugin-capability-grant)
"Core never links this library" is not implementable — plugins are Rhai scripts, which cannot
open a file or link a crate, so the core is the only thing that *can* open the database. The
core links a typed reader crate and exposes it to plugins behind the capability grant. The
grant, not the absence of a dependency, is what protects a channel that didn't ask for taste data.
:::

---

## 1. Non-goals

- Not a Plex replacement or mirror. Plex remains authoritative for library contents, playback, and users.
- Not a recommender *product*. It stores signal and exposes queries; ranking policy lives in consumers.
- Not a live service in the request path. Batch enrichment, cached results, offline scoring.

---

## 2. Two-layer model

This split is the core design decision.

### Layer 1 — cached item graph (user-agnostic, scraped, durable)

Same for every user. Expensive to acquire, cheap to reuse. Cache hard.

- **Enrichment tags** — TMDB keywords, networks, awards, and anything else per-title.
- **Affinity edges** — item-to-item relationships from multiple sources.
- **Weighted collections** — membership sets with per-item weight.
- **Embeddings** — vectors over plot overviews (stretch tier).

### Layer 2 — derived per-user taste profile (computed, not cached)

Built per generation pass from a user's fingerprint-resolved watch history, rolled up through Layer 1 into a weighted attribute vector. Candidate scoring is vector overlap plus edge bonuses.

Recompute rather than store-and-invalidate: watch history changes constantly, and invalidation is more work than recomputation. Revisit only if profiling says otherwise.

---

## 3. Schema sketch

::: warning `item_id` resolved by [ADR-0002](./adr/0002-item-id-is-the-entry-id-string) and [ADR-0005](./adr/0005-the-store-walks-plex-itself-and-augments-never-replaces)
`item_id` is the opaque first-hit-wins string `imdb:` → `tmdb:` → `tvdb:` → `plex:`, else
`fs:<hash>` — byte-identical to `etv-station`'s `entry_id`. An `external_ids(item_id, ns, value)`
table carries every other id a title is known by. The store walks Plex itself to build both,
because watch history identifies titles only by `ratingKey` and a `plex://` agent GUID.
:::

### 3.1 Generic namespaced enrichment (key-value sidecar)

Do **not** add first-class `mood` / `keywords` / `award` columns. The core store must not carry fields defined by one consumer's needs — the next plugin author wants different fields.

```
enrichment(item_id, namespace, key, value, fetched_at)
```

- Namespaces are hard-partitioned. A writer can wipe and rewrite only its own rows; two consumers cannot collide.
- Values are opaque to the store. It indexes and serves; it does not interpret.
- `fetched_at` drives staleness, not correctness.

### 3.2 Affinity edges

```
edges(from_id, to_id, edge_type, rank, fetched_at)
```

- `from_id` and `to_id` as separate columns, never a concatenated key — the scorer must query in both directions and filter by type without string parsing. Uniqueness is (`from_id`, `to_id`, `edge_type`).
- **Store the source's rank; do not collapse to a boolean.** A #2 recommendation is not a #20 recommendation, and consumers want the gradient.
- **Edges are snapshots, not facts.** They drift as new titles appear and old ones fall off. Handle by refresh, not by modeling truth: on re-pull, *replace* that (`from_id`, `edge_type`) set wholesale rather than appending. Additions and removals then fall out naturally.
- Staleness threshold per source, roughly 30–60 days for external sources. Local edges recompute free on every sweep and never expire.

**Edge sources:**

| Source | Type | Notes |
|---|---|---|
| Local Plex collection co-membership | local | Human-curated, highest weight, no API, recompute each sweep |
| TMDB recommendations | external | Behavioral; closest to "people who watched this also watched" |
| TMDB similar | external | Content-based; different signal from recommendations |
| Trakt related | external | Genuinely behavioral, large user base, real free API, already crawled in the curator pipeline |
| IMDb "more like this" | — | **Skip.** No free official API; scraping is fragile and against terms |

### 3.3 Weighted collection membership

A collection is not a set. It is a set of (item, weight, source).

```
collection_membership(collection_id, item_id, weight, source, observed_at)
```

Drives two things that turned out to be the same mechanism:

- **Crowd-derived semantic tagging.** Public user lists named "Christmas movies" are human-authored classifications. When many independent users file a title under such a list, that co-membership becomes a weighted thematic tag — including for titles no official source calls a Christmas movie. Medium-weight signal, harvested by list-name semantics.
- **Subreddit mention harvesting.** Treat a mention in r/TubiTreasures, r/badMovies etc. as membership; weight by mention count.

Same table serves seed-set bootstrapping for ETV-003 categories: a crowd list *is* a seed set.

### 3.4 Identity (shared-account disentanglement)

~10 users, of which most map to one person or couple. Two are shared: `bboy` and the core `McBrideMusings`/general account.

::: tip Verified against the live server — see [ADR-0004](./adr/0004-the-store-owns-watch-history)
The whole tuple survives batch ingest. A Tautulli `get_history` row carries `machine_id`,
`ip_address`, `platform`, `player`, `product`. Plex's own history carries `deviceID`, which
joins to `/devices` for `clientIdentifier` (the same machine id) and `platform` — but no IP.
No live poller is needed either way.
:::

**Fingerprint tuple, in priority order:**

1. **Plex client machine ID** — primary. Stable per client install, survives device renames. Closest available substitute for a MAC address, which is unobtainable: MACs do not survive the first network hop, so a remote client only ever presents an IP.
2. **IP** — secondary. Coarse household bucket.
3. **Platform / product** — weak tiebreak. Distinguishes an iPad from a tvOS box from a browser on one IP; a soft fingerprint standing in for hardware identity.
4. **Device display name** — display label only, not a clustering key. Collides on generic strings, changes on rename.

**Explicitly deferred: time-of-day.** It only earns a place when one device genuinely serves two different people (kids vs. adults on a shared TV), which does not describe this user base. Revisit only if a spike shows a single client ID with a bimodal pattern worth splitting.

**Caveats to encode, not solve:** client machine ID is stable per *install*, so reinstalls fork identity and two people on one physical client still collapse. Transient IPs (hotels, cellular) are accepted noise. Prior art warns explicitly against merging users who merely shared a transient/corporate IP — down-weight or exclude such IPs rather than modeling them.

**Prior art — the subfield is "shared-account user disentanglement" / "latent user identification":**
- *User Identification within a Shared Account: Improving IP-TV Recommender Performance* — https://link.springer.com/chapter/10.1007/978-3-319-10933-6_17 (same domain: set-top-box logs, detect sharing, then split users)
- *Gesture Clustering for Real-Time User Disentanglement in Shared-Account Recommendation*, SIGIR 2025 — https://doi.org/10.1145/3805712.3808393
- Recurring finding across the literature: a shared account is mostly occupied by one user at a time, so cluster the interaction stream into latent users rather than tagging each play live.

### 3.5 Layer-2 taste profile (in-memory)

Per-user weighted attribute vector over Layer-1 attributes. No persisted schema unless profiling demands it.

---

## 4. Signals

| Signal | Source | Weight | Status |
|---|---|---|---|
| Watch history | Tautulli | High | Committed |
| Per-user star rating (`userRating`) | Plex | Highest confidence, sparsest coverage | Committed |
| Recency weighting | derived | — | Committed |
| Exploration slice | policy | — | Committed |
| Household co-occurrence | Tautulli, ~10 users | Minority partner | Spike |
| Abandonment / low completion | Tautulli | Low confidence | Spike, gated |
| Letterboxd ratings & reviews | external, per-user | — | Committed (movies only) |
| Live-channel viewing attribution | Plex/Tautulli | — | Spike |

**Star ratings.** Per-user ratings are real: managed users each keep independent ratings on the same file, with no merging. Field is `userRating`, readable at `GET /library/metadata/<ratingKey>` and writable via the rate endpoint. This is the highest-quality signal available — explicit and intentional — but expect thin coverage. Note the asymmetry: it is clean for the ~9 near-unique users and near-useless for the shared accounts, because ratings carry no device metadata to cluster on, unlike plays.

::: warning Requires the Tautulli adapter — [ADR-0004](./adr/0004-the-store-owns-watch-history)
Plex's own history is a *watched-it* ledger. Measured over the last 300 plays: 261 of 268 plays
finished at 90%+ appear in Plex history, but only 3 of 17 plays abandoned under 40% do. So a
Plex-only deployment has no weak negative signal — it has none. The floor should measure
`duration − paused_counter`, both Tautulli fields.
:::

**Negative signal — gated and skeptical.** Watch-completion percentage is the only dislike channel available (no likes, no skips). Two guardrails, both required:
- **Hard floor:** no play under ~30 seconds of *actual footage* counts at all. Scrubbing and playback quality-checks must not register. Segment-hopping inside the floor does not accumulate.
- Even above the floor, treat as low confidence. A ten-minute bail can be mood rather than taste.

Implement, measure whether it is predictive, and only then weight it in.

**Watch-history source asymmetry.** History comes from Plex, not from the channel. On-demand playback is rich and trackable, and Plex *can* register a stop, which is a soft skip. Channel viewing is thin to invisible — someone parked on a channel for hours may not be tracked at all. Whether live-channel viewing can be attributed to a user (get current viewers, correlate to what was airing at that timestamp) is its own spike.

**No-skip design consequence.** On a feed, the skip is the primary signal because scrolling is free. On a channel there is no skip, so there is no fast negative feedback and a bad pick costs the viewer real minutes. That argues for exploitation-heavy ranking with a small deliberate discovery slice — a design constraint, not merely a limitation.

**Exploration is the point.** A pure taste vector is an echo chamber. Keep an explicit knob: mostly on-taste, a deliberate fraction adjacent-but-unseen.

**Per-user external taste sources.** Define a pluggable interface, with Letterboxd as the first implementation. Do not hardcode to Letterboxd.

---

## 5. What the existing For You sample is not

The shipped `taste-engine.rhai` ranks on: recently-aired suppression (`replay_ttl_days` 30, hard zero), watch affinity `(1 − age/14d) × 3`, freshness `+0.5` for year ≥ 2024, easy-entry `+0.25` for season 1, deterministic `entry_id` tiebreak.

It uses **no** genre, cast, director, studio, or thematic signal. It is a replay-affinity ranker, not a For You algorithm — it can never make the grilling-video jump because it has no notion of what a title is *about*, only whether it has been touched. Replacing it with a content-based taste model is the headline item this repo exists to enable.

**Positioning against feed algorithms:** TikTok/Reels are primarily *collaborative* filtering plus rich behavioral telemetry (watch time, rewatches, skips, scroll velocity) fed to a constantly-retrained ranker; content tags are secondary. That world requires millions of users. With ~10 users and no scroll telemetry, collaborative filtering barely functions — content-based filtering is the correct choice here, not a consolation prize. Household co-occurrence is a legitimate but minor second-order enhancement.

---

## 6. Enrichment sources

| Source | Provides | Notes |
|---|---|---|
| TMDB | Keywords (concept-level tags), recommendations, similar, networks, overviews | Free for non-commercial. **The keywords endpoint is the core acquisition** — the concept tags Plex lacks. Resolve via the TMDB/IMDb GUIDs Plex agents already attach |
| Trakt | Related, trending | Real free API; already crawled by the curator pipeline |
| Wikidata / Wikipedia | Awards: award, category, year, result | Structured and queryable. Near-permanent facts, no staleness churn. Prefer over the hand-built "Oscar nominees" Plex collection, which carries neither category nor year |
| MDBList | Trending, list membership | Cross-source aggregation |
| AniList / AniDB / TheXEM / anibridge-mappings | Anime arc and season splits | See ETV-005 |
| Reddit | Weighted list membership | Rate-limited, OAuth'd; free-text title resolution is its own sub-problem |
| TVmaze | Schedules | Only if network-mirror channels pursue real schedule data. Weak on themes |
| Local embeddings | Overview vectors | Stretch tier; local GPU already available |

**What Plex natively provides** (the floor this exists to raise): genres, directors, writers, cast, studio, content rating, year, country, collections, plus patchy mood/style depending on agent. No thematic or concept-level tags, no tone, no micro-genre. Category-level similarity is achievable natively; concept-level is not.

**Caching discipline.** Enrich once per title, keyed by external ID, with `fetched_at`. Never re-fetch unless the row is missing or past threshold. This matters independently of rate limits.

---

## 7. Write-back projection to Plex (design now, build later)

Goal: let tools that only speak Plex benefit from enrichment without linking this library.

**Feasible, and more durable than first assumed, because of field locking.**

- Tags are editable via the API: genres, collections, labels, moods, styles, plus scalars. python-plexapi exposes `addLabel` / `addGenre` / `addMood` / `addStyle` / `addCollection`; `editTags` defaults to `locked=True`. Raw HTTP is a PUT against `/library/sections/{id}/all`.
- **Locked fields do not update on metadata refreshes.** Locking is the documented mechanism for preventing an agent from overwriting a field, and it is explicitly recommended practice for anything set via the API.
  - https://support.plex.tv/articles/201272763-edit-details/
  - https://www.plexopedia.com/plex-media-server/api/library/movie-update/
  - https://python-plexapi.readthedocs.io/en/latest/modules/mixins.html
- **Locking is sticky — plan the unlock path from day one.** A user who manually edited tags and collections found those fields locked and could not benefit from a new scanner's automatic collection assignment without unlocking every item: https://forums.plex.tv/t/how-to-unlock-locked-metadata-fields-en-masse/673826 — lock only the specific field written, never the item wholesale.

**Use `label`, not `genre`.** Labels are intended for fine-grained filtering and access restrictions. Genres feed Plex's own browse UI and its agents reason about them; writing inferred genres there corrupts a field the rest of Plex depends on.

**Representational limits.** Labels are flat strings — no namespace, no numeric weight, no timestamp. Fake namespacing by prefix (`xdb:kw:martial-arts`, `xdb:season:christmas`) for collision avoidance and cheap wipe-by-prefix. Scores cannot round-trip: `xdb:christmas:0.83` is a string Plex cannot range-query, so thresholding must happen at write time and only the boolean outcome survives.

**Cannot be projected at all:**
- Item-to-item edges — Plex has no edge model; collections are the only adjacency primitive and they are heavyweight objects, not a graph.
- Rank and `fetched_at` — nowhere to store them, so refresh/staleness logic cannot round-trip.
- Per-user taste vectors — derived and user-scoped, no home in Plex.

**Therefore:** this store stays authoritative. Write-back is a one-way, booleanized, prefix-namespaced *projection* for the benefit of Plex-only tools. Anything consuming the graph reads here directly, because Plex physically cannot carry what it needs. Structurally this is a publishing concern — an optional output adapter, not the storage layer.

---

## 8. Spikes

1. **Household co-occurrence** — build the pairwise co-watch matrix over ~10 users and look at whether it surfaces anything content similarity misses. Small enough to be a lookup, not a model.
2. **Shared-account clustering** — cluster `bboy` and the general account by client machine ID; measure whether clusters separate meaningfully or just fragment one person.
3. **Negative signal predictiveness** — with the 30-second floor applied, does abandonment predict anything, or is it noise?
4. **Genre discrimination** — does gating coarse Plex genre on TMDB keyword corroboration actually fix the "Guardians of the Galaxy in the Comedy channel" problem? Stretch: infer a *primary* genre from keyword density rather than treating all listed genres as equal.
5. **Episode-grain seasonal signal** — can Halloween/Christmas episodes of otherwise-unthemed shows be identified at all (TMDB episode keywords, episode-title matching), and is coverage dense enough to use? Hardest tier of the seasonal work.
6. **Live-channel viewing attribution** — can a current viewer be resolved to a user and correlated to what was airing?
7. **Seed-based category quality** — do inferred keyword sets from a handful of seed titles actually produce a usable pool, and do negative seeds sharpen the boundary as expected?
