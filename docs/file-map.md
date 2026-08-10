# File map

```
plex-db-ex/
├── CLAUDE.md              project rules — the ones easy to violate by accident
├── admin.toml             task runner manifest (admin build | dev | test | vet | …)
├── .env.example           every variable, with placeholders; real values live in .env
├── Cargo.toml             Rust workspace manifest — members: crates/plexdb-reader
├── crates/                the Rust workspace
│   └── plexdb-reader/     read-only typed Rust reader over plexdb.db (ADR-0003): enrichment, edges, taste vector; opened SQLITE_OPEN_READ_ONLY and schema-version gated. No collection-membership accessor yet — that table is not built. Tested against a fixture store built by `plexdb init`; no live Plex, no network, no consumer repo
├── plexdb/                the Python package — the only writer of plexdb.db
│   ├── cli.py             the `plexdb` command line; discovers and registers each module under commands/, holds no command itself
│   ├── commands/          one module per subcommand; a new command is one new file here, no edit to cli.py
│   │   ├── enrich_tautulli.py
│   │   ├── enrich_tmdb_edges.py
│   │   ├── enrich_tmdb_keywords.py
│   │   ├── ingest_plays.py
│   │   ├── init.py
│   │   ├── latent_users.py
│   │   ├── local_edges.py
│   │   ├── publish.py
│   │   ├── reconcile_etv.py
│   │   └── walk.py
│   ├── config.py          settings from .env; never a default for a URL or a token
│   ├── errors.py          the errors a user is meant to see, as one `error: …` line
│   ├── identity.py        item_id derivation — mirrored in etv-station, guarded by a fixture
│   ├── enrich_tmdb.py     TMDB keyword sweep: namespaced, cached, staleness-gated, tolerant of a failing title (aborts after 3 in a row)
│   ├── local_edges.py     collection co-membership sweep: replace-wholesale, no staleness, Plex's smart collections excluded
│   ├── plays.py           watch-history ingest: Plex history into plays, incrementally, plus the Tautulli match that enriches them
│   ├── clusters.py        clusters a *configured* shared account's plays into latent users by fingerprint, joining devices that share a recurring single-account IP (issue #27), and scores keyword overlap between them over show/movie units — a show counts once however many episodes were watched (issue #25); every other account is one named person with a single profile, no device or cluster numbers; read-only, persists nothing
│   ├── tautulli_client.py read-only Tautulli client (get_history, get_users) behind TautulliSource/TautulliUserSource protocols
│   ├── plex_client.py     read-only Plex HTTP client behind PlexSource/PlexAccountSource protocols
│   ├── reconcile_etv.py   compares item_id against etv-station's entry_id, joined by Plex rating key; read-only on both stores, reports and never fixes
│   ├── tmdb_client.py     read-only TMDB client (keywords, recommendations, similar) behind a TMDbSource protocol
│   ├── tmdb_common.py     media_type_for / is_stale / MAX_CONSECUTIVE_FAILURES / DEFAULT_STALE_DAYS shared by enrich_tmdb.py and tmdb_edges.py
│   ├── tmdb_edges.py      TMDB recommendations/similar sweep: two edge types, replace-wholesale per (from_id, edge_type), cached via a tmdb_edges enrichment cursor
│   ├── walk.py            the library walk: Plex sections into items, external_ids, plex_items
│   ├── schema.py          the DDL and the append-only migration list
│   └── store.py           opening the store, and publishing the read-only snapshot consumers open
├── tests/                 pytest; no test reaches the network
│   └── fixtures/
│       └── entry_id.json  SHARED WITH etv-station — copied there, hash-pinned in both
├── data/                  plexdb.db lives here (gitignored)
└── docs/
    ├── index.md           docs home
    ├── PRD.md             the spec to build from — supersedes spec.md
    ├── spec.md            the original spec: research, prior art, reasoning
    ├── schema.md          the SQLite schema, which is the public API
    ├── CONTEXT.md         vocabulary — read before writing code
    ├── roadmap.md         Now / Next / Later / Deferred, plus open questions
    ├── file-map.md        this file
    └── adr/               architecture decision records
```

## Related repos on this machine

Neither is a dependency; both are consumers, and both already contain code this project either
mirrors or will absorb.

| Repo | Language | What matters here |
|---|---|---|
| `~/Projects/etv-station` | Rust | `crates/etv-station/src/catalog/identity.rs` — the `entry_id` derivation this store must match byte for byte. `docs/adr/0002` — the Rhai plugin contract the reader crate plugs into. |
| `~/Projects/curator` | Python | `curator/clients/`, `harvest/`, `resolve/`, `enrich/` — the acquisition code this store absorbs. `curator/curator/schema.sql` — `keyword_cache` and `resolution_cache`, the caching discipline already in production. |
