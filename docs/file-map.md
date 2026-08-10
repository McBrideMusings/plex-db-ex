# File map

```
plex-db-ex/
├── CLAUDE.md              project rules — the ones easy to violate by accident
├── admin.toml             task runner manifest (admin build | dev | test | vet | …)
├── .env.example           every variable, with placeholders; real values live in .env
├── plexdb/                the Python package — the only writer of plexdb.db
│   ├── cli.py             the `plexdb` command line; subcommands land here as slices ship
│   ├── config.py          settings from .env; never a default for a URL or a token
│   ├── errors.py          the errors a user is meant to see, as one `error: …` line
│   ├── identity.py        item_id derivation — mirrored in etv-station, guarded by a fixture
│   ├── enrich_tmdb.py     TMDB keyword sweep: namespaced, cached, staleness-gated
│   ├── plays.py           watch-history ingest: Plex history into plays, incrementally
│   ├── plex_client.py     read-only Plex HTTP client behind a PlexSource protocol
│   ├── tmdb_client.py     read-only TMDB client (keywords) behind a TMDbSource protocol
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
