# File map

```
plex-db-ex/
├── CLAUDE.md              project rules — the ones easy to violate by accident
├── admin.toml             task runner manifest (admin build | dev | test | vet | …)
├── .env.example           every variable, with placeholders; real values live in .env
├── plexdb/                the Python package — the only writer of plexdb.db
├── data/                  plexdb.db lives here (gitignored)
└── docs/
    ├── index.md           docs home
    ├── PRD.md             the spec, with inline flags where an ADR supersedes it
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
