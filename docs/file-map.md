# File map

```
plex-db-ex/
├── CLAUDE.md              project rules — the ones easy to violate by accident
├── admin.toml             task runner manifest (admin build | dev | test | vet | …)
├── .env.example           every variable, with placeholders; real values live in .env
├── Dockerfile             the writer packaged for the Unraid host (#43): linux/amd64 pinned in the FROM because it is always built on an arm64 Mac, ENTRYPOINT ["plexdb", "schedule"], no shell wrapper, and nothing here naming a step or a failure policy
├── .dockerignore          keeps data/ out of the build context — an image layer holding the store would put twenty months of watch history in every registry it is pushed to
├── deploy/
│   └── my-plexdb.xml      reference copy of the Unraid container template, copied to /boot/config/plugins/dockerMan/templates-user/ on the host (#47). Read by nothing here; committed so the mounts and which variables are secret are reviewable. Every credential default is empty, deliberately
├── Cargo.toml             Rust workspace manifest — members: crates/plexdb-reader
├── crates/                the Rust workspace
│   └── plexdb-reader/     read-only typed Rust reader over plexdb.db (ADR-0003): enrichment (single-item and bulk — `enrichment_for_many` chunks an `IN` clause against the connection's own SQLITE_LIMIT_VARIABLE_NUMBER so a whole candidate pool is one query, not one per candidate, #40), edges, taste vector; opened SQLITE_OPEN_READ_ONLY and schema-version gated. Two taste accessors — `taste_vector_for(account)` and `pooled_taste_vector()` over every account's plays (#39) — sharing one private `rollup()` so ADR-0011's rules exist once: `sqrt(seasons watched)` split across a title's attributes, nothing under half a season. Every enrichment read and the rollup return each `(key, value)` once per title however many sources carry it, and `source` never reaches a caller (ADR-0016). `keyword_for_surface` lowercases, trims and whitespace-collapses a spelling and looks it up in `keyword_forms` — `None` when unknown, and never any stemming, which only the Python writer does — and `surfaces_for_keyword` lists the spellings recorded for a stored keyword. It needs no bookkeeping filter, because writers' cursors live in `enrichment_cursor` rather than in `enrichment` (ADR-0013). `examples/taste_vector.rs` prints one account's vector, so the rollup can be driven and read back without a consumer. `collections_for(item_id)` joins `collection_membership` with `collection`, and every field a source may not record — `rank`, `mentions`, `name`, `url`, `size`, `likes` — stays `Option<_>` rather than collapsing to `0`, so "unordered" and "ranked first" stay distinguishable (#29). Tested against a fixture store built by `plexdb migrate`; no live Plex, no network, no consumer repo
├── plexdb/                the Python package — the only writer of plexdb.db
│   ├── cli.py             the `plexdb` command line; discovers and registers each module under commands/, holds no command itself
│   ├── commands/          one module per subcommand, each declaring NAME, ORDER, and optionally SWEEP (and SOURCE, for a Gated Source); a new command is one new file here, no edit to cli.py, sweep.py or config.py
│   │   ├── check.py
│   │   ├── enrich_tautulli.py
│   │   ├── enrich_tmdb_edges.py
│   │   ├── enrich_tmdb_keywords.py
│   │   ├── explore.py
│   │   ├── harvest_mdblist.py
│   │   ├── ingest_plays.py
│   │   ├── migrate.py
│   │   ├── latent_users.py
│   │   ├── publish.py
│   │   ├── refresh_map.py
│   │   ├── repair_fs_identities.py
│   │   ├── repair_identities.py
│   │   ├── schedule.py
│   │   ├── sweep.py
│   │   └── walk.py
│   ├── config.py          settings from .env; never a default for a URL or a token
│   ├── errors.py          the errors a user is meant to see, as one `error: …` line
│   ├── identity.py        item_id derivation — first-hit-wins over external GUIDs, path hash as the floor; specified by tests/fixtures/item_id.json
│   ├── enrich_tmdb.py     TMDB keyword sweep: writes `source='tmdb'` rows into the shared `keywords` namespace (ADR-0016), cached, staleness-gated, tolerant of a failing title (aborts after 3 in a row)
│   ├── keywords.py        `normalize_keyword` — lowercase, `-`/`_` as word breaks, whitespace-collapsed, Snowball-stemmed — and `upsert_keyword_form`, which every keyword writer calls so `keyword_forms(surface, keyword)` can still show a reader the raw spelling a source used (ADR-0016)
│   ├── plays.py           watch-history ingest: Plex history into plays, incrementally, plus the Tautulli match that enriches them
│   ├── clusters.py        clusters a *configured* shared account's plays into latent users by fingerprint, joining devices that share a recurring single-account IP (issue #27), and scores keyword overlap between them over show/movie units — a show counts once however many episodes were watched (issue #25); a cluster under 20 plays is folded into one unattributed bucket rather than called a person (issue #28); every other account is one named person with a single profile, no device or cluster numbers; read-only, persists nothing
│   ├── explore.py         the tag explorer behind `plexdb explore`: every `keywords`-namespace value per media type with its title count, IDF (`1 + ln(N / df)`, as taste-cosine.rhai computes it) and top co-occurring tags, plus the titles carrying one tag and the neighbourhood graph around one (`neighbourhood`: its top co-tags, each keeping its 4 strongest links, noise-marked tags excluded before ranking) and the 2D map (`title_map`: IDF-weighted keyword vectors, truncated SVD to 50 dimensions, UMAP to two, via numpy, scikit-learn and umap-learn imported only when a map is drawn; `stored_map` serves the writer's precomputed default map when its fingerprint matches the keyword rows, and a map with noise excluded is drawn live and cached per kind and noise list — tens of seconds for the movies); a stdlib `http.server` that opens the store read-only per request and caches the index until the file changes. `serve_in_background` is how the container runs it: a daemon thread inside `plexdb schedule`, reading the snapshot, started when PLEXDB_EXPLORE_PORT is set, answering 503 until the first publish. It also serves the Query tab: `run_query` runs one SELECT on a fresh read-only connection with `PRAGMA query_only`, an authorizer that allows only SELECT, READ, FUNCTION and RECURSIVE, a 5 s progress-handler timeout and a 500-row cap, then one lookup in `items` that returns `labels` (a title, year or episode code for each item id among the returned cells); `SavedQueries` keeps named queries in `explore-queries.json` beside the store (`data/` locally, the snapshot's directory in the container), replaced by rename; `RECIPES` is the SQL behind each `plexdb-reader` accessor plus orientation queries
│   ├── titlemap.py        the writer's half of the explorer's default title map: draws it once per kind into `title_map` / `title_map_state`, skipping a kind whose keyword fingerprint is unchanged
│   ├── explore.html       the explorer's one page — table, drill-down, a co-occurrence graph around one tag (cytoscape.js 3.34.3 from cdnjs, pinned with its SRI hash), a canvas map of every title with the selected tag's titles highlighted, a noise list kept in the browser's localStorage, never on disk, and the Query tab (SQL box, result table, saved queries, recipes)
│   ├── tautulli_client.py read-only Tautulli client (get_history, get_users) behind TautulliSource/TautulliUserSource protocols
│   ├── plex_client.py     read-only Plex HTTP client behind PlexSource/PlexAccountSource protocols
│   ├── tmdb_client.py     read-only TMDB client (keywords, recommendations, similar) behind a TMDbSource protocol
│   ├── tmdb_common.py     media_type_for / MAX_CONSECUTIVE_FAILURES shared by enrich_tmdb.py and tmdb_edges.py
│   ├── staleness.py       is_stale / DEFAULT_STALE_DAYS — the one "is this row due a re-fetch" rule, shared by both TMDB sweeps and the crowd-list harvest
│   ├── mdblist_client.py  read-only MDBList client (top lists, list entries) behind an MDBListSource protocol; pages until has_more clears and sends an explicit User-Agent, without which the service 403s a valid key
│   ├── collections.py     crowd-list harvest into collection/collection_membership: rank is array position, replace-wholesale per collection_id, no computed weight (ADR-0012), an entry outside the library dropped rather than invented
│   ├── tmdb_edges.py      TMDB recommendations/similar sweep: two edge types, replace-wholesale per (from_id, edge_type), cached via enrichment_cursor rows under the tmdb_edges namespace
│   ├── repair.py          splits identities that fused two unrelated titles sharing a TMDB/TVDB number (issue #23): deletes them, re-walks Plex, rewinds the play cursor over what went with them
│   ├── repair_fs_identities.py  moves an fs: item_id that names a disk mount point onto what the rule derives today, carrying every referencing row in one transaction (issue #55). The walk cannot do this — ADR-0008 keeps an assigned item_id put, because a walk cannot know the move is safe; a person pointing this at a known problem can. Backs up first, handles two ids merging onto one corrected id, and rolls back on any row loss it did not expect
│   ├── walk.py            the library walk: Plex sections into items, external_ids, plex_items; an external id only ever matches within its own media kind. Every title that kept an id differing from the one derived this pass is named in the summary — show, season, episode, both ids (issue #56) — because whether the keep was right depends on which title it was
│   ├── health.py          what `plexdb check` reads: schema version against this build, PRAGMA quick_check, row counts, the newest play/enrichment/id-seen and how old each is, the identities more than one rating key points at, the fs: count, keyword row counts per source and the `keyword_forms` count, and what the backups directory holds (#59). Any row still left under the pre-v10 `tmdb_keywords` namespace fails the check outright (ADR-0016). Read-only and defensive about shape — a table or column the store has not reached yet is reported as absent rather than raising, because a store this build disagrees with is the case it exists for
│   ├── schema.py          the DDL and the append-only migration list
│   ├── sources.py         what a Gated Source is — an external source whose units carry a fetched_at and are re-fetched only once stale; holds the credential check, the derived <NAME>_STALE_DAYS window, the client build, the store handle and the --stale-days/--rewipe flags, so a source declares only what varies. Not harvest-plex-collections (replaces wholesale, never stales — its input is Plex, not a rate-limited API) and not enrich-tautulli-plays (matches rows)
│   ├── sweep.py           one scheduled run: the steps a command opts into by declaring SWEEP, ordered by ORDER, a required step's failure ending the run and a best-effort one's only reported (ADR-0014); ConfigError reads as "not configured", anything else as "not reachable"
│   ├── schedule.py        the container entrypoint's clock, plus the one thing that must happen before any clock matters (ADR-0015): parse PLEXDB_SCHEDULE as HH:MM, migrate the store to the schema this build expects, then sleep to the next occurrence, run one sweep, repeat. The migration is here rather than only in the sweep because a freshly deployed image otherwise ran for up to a day against a store at the old schema (#58); it is idempotent, so a restart loop cannot fill the disk with copies. In the package rather than the Dockerfile so `next_fire` — midnight rollover, the sweep that ends inside its own minute — is a pure function with tests instead of a `sleep` loop nobody can check
│   ├── backup.py          copies of the store taken before a migration and never pruned — one per schema version is the only route back past that migration: VACUUM INTO so a copy taken mid-sweep is consistent, guarded row counts for items/plays/enrichment, and the restore that puts one back. The transaction in schema.apply covers a crash; this covers SQL that runs perfectly and does the wrong thing
│   └── store.py           opening the store, publishing the read-only snapshot consumers open, and `migrate` — back up, apply, verify version + quick_check + row counts, roll back on any of them. `quick_check` is exposed rather than inlined so `plexdb check` asks the same question of a live store that `migrate` asks of a just-migrated one
├── tools/
│   ├── baseline.sh        copies of the live store over ssh: `pull` to ./data, `backup` left on the host (also the first step of `admin deploy`, so there is a snapshot from before the new code existed) and pruned to the newest MANUAL_KEEP=10, `list`. VACUUM INTO on the host rather than scp, because the store is in WAL mode and a plain copy leaves the sidecar's rows behind. Only manual copies are pruned — a pre-migration one never is
│   └── host-exec.sh       one plexdb command inside the deployed container, now rather than at the scheduler's next sweep — the container's own CLI, so the store is still written by the one writer. Refuses while a sweep is running, except for `check`, which writes nothing and instead waits out a migration in progress — which is what lets it be the last step of `admin deploy`
├── tests/                 pytest; no test reaches the network
│   └── fixtures/
│       ├── item_id.json   the published specification of the item_id rule (ADR-0006), run by test_identity.py
│       └── mdblist/       trimmed recordings of MDBList's live responses, including a two-page list that proves paging
├── data/                  plexdb.db lives here (gitignored)
└── docs/
    ├── index.md           docs home
    ├── PRD.md             the spec to build from — supersedes spec.md
    ├── spec.md            the original spec: research, prior art, reasoning
    ├── schema.md          the SQLite schema, which is the public API
    ├── CONTEXT.md         vocabulary — read before writing code
    ├── roadmap.md         Now / Next / Later / Deferred, plus open questions
    ├── baseline.md        where the real store lives and why it is never edited in place
    ├── development.md     what is safe to break, what a deploy must never re-acquire, where each change belongs
    ├── file-map.md        this file
    └── adr/               architecture decision records
```

## Related repos on this machine

Neither is a dependency; both are consumers, and both already contain code this project either
mirrors or will absorb.

| Repo | Language | What matters here |
|---|---|---|
| `~/Projects/etv-station` | Rust | A consumer, not a dependency — nothing here is owed to it. `docs/adr/0002` there is the Rhai plugin contract the reader crate plugs into. |
| `~/Projects/curator` | Python | `curator/clients/`, `harvest/`, `resolve/`, `enrich/` — the acquisition code this store absorbs. `curator/curator/schema.sql` — `keyword_cache` and `resolution_cache`, the caching discipline already in production. |
