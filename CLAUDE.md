# plex-db-ex

A queryable metadata and affinity store layered over a Plex library. One process writes a
SQLite file; several projects read it. Plex stays authoritative for library contents,
playback, and users.

Read [`docs/CONTEXT.md`](docs/CONTEXT.md) for vocabulary before writing code, and
[`docs/adr/`](docs/adr/) for the decisions that shape the schema. `docs/PRD.md` is the full
spec.

## The rules that are easy to violate by accident

**Plex is the only required dependency.** Everything else — Tautulli, TMDB, Trakt, MDBList,
Reddit — is an optional source behind an adapter. Code that assumes Tautulli is present is a
bug (ADR-0004). A consumer talks to Plex directly for anything Plex can answer and reaches this
store only for what Plex cannot carry (ADR-0005).

**One writer.** The Python package here is the only thing that writes `plexdb.db`. Every other
consumer opens it read-only. The schema is the public API — a schema change breaks every
consumer at once, with no version negotiation (ADR-0001).

**`item_id` is not a TMDb id.** It is the opaque first-hit-wins string
`imdb:tt1375666` → `tmdb:…` → `tvdb:…` → `plex:…`, else `fs:<hash>` — byte-identical to what
`etv-station` derives in `crates/etv-station/src/catalog/identity.rs`. The two implementations
are tested against one shared fixture; if they drift, every join silently returns nothing
(ADR-0002).

**Enrichment is namespaced and opaque.** No first-class `mood` / `keywords` / `award` columns.
A writer may wipe and rewrite only rows in its own namespace, and the store never interprets a
value.

**Edges are snapshots, not facts.** On re-pull, replace the whole `(from_id, edge_type)` set
rather than appending. Store the source's rank; never collapse it to a boolean.

**Enrich once, keyed by external id, with `fetched_at`.** Never re-fetch a row that exists and
is inside its staleness threshold. This holds independently of rate limits.

**Bookkeeping never goes in `enrichment`.** That table holds facts about titles; a writer's own
fetch cursors go in `enrichment_cursor`. A cursor that sat in the facts table put the string `1`
at the top of the house's taste profile and shrank every real keyword at the same time (ADR-0013).

**The real store lives on the host, and you work on a copy.** `admin pull-baseline` fetches it;
`data/` is gitignored, so a working copy never follows a branch anywhere. Break the copy freely —
drop tables, run an unfinished migration, inject fake plays — and never point any of that at the
host. Twenty months of watch history and a rate-limited TMDB sweep are in there, and no re-scan
reproduces either. A deploy re-seeds nothing and re-fetches nothing; sweeps are incremental
because `enrichment_cursor` carries a `fetched_at` per title. See
[`docs/development.md`](docs/development.md) and [`docs/baseline.md`](docs/baseline.md).

## Layout

```
plexdb/            the Python package — the only writer
docs/              CONTEXT.md, adr/, PRD.md, ROADMAP.md
data/              plexdb.db lives here (gitignored)
```

## Documentation

VitePress site under `docs/`. Run `admin docs` to read it on `http://localhost:5193`.

Keep these in sync as you work:

| File | Update when |
|---|---|
| `docs/schema.md` | Any table, column, or refresh rule changes — this is the public API |
| `docs/CONTEXT.md` | A term resolves, or an existing one turns out to mean something else |
| `docs/adr/` | A decision is made that is hard to reverse and would surprise a future reader |
| `docs/PRD.md` | Scope or product behaviour changes |
| `docs/roadmap.md` | Direction shifts, something ships, or a question gets answered |
| `docs/file-map.md` | Major files or folders are added, removed, or moved |

Don't write new top-level planning or phase docs in `docs/` — file an issue on
[the tracker](https://github.com/McBrideMusings/plex-db-ex/issues) instead. `roadmap.md` is the
only forward-looking doc.

## Tasks

`admin.toml` is the source of truth for commands; the tool is installed on PATH.

```
admin build      uv sync
admin dev <...>  run the plexdb CLI, args forwarded
admin test       pytest
admin lint       ruff check
admin fmt        ruff format
admin vet        lint + typecheck + test
admin docs       serve the docs site
```

## Configuration

All connection details come from `.env` (gitignored). `.env.example` documents every variable
with placeholders. Never hardcode a URL, token, or hostname, and never put a real value in a
committed file.
