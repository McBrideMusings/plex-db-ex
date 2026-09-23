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
`imdb:tt1375666` → `tmdb:…` → `tvdb:…` → `plex:…`, else `fs:<hash>` (ADR-0002). The rule is
published, so changing it is a schema change: its specification is
`tests/fixtures/item_id.json`, and the code and that file move together or the spec stops
describing the store. Who else derives these ids, and how they keep up, is their business — this
store keeps no register of its readers.

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
admin deploy     copy the live store, build the image, recreate the container on the host
admin test       pytest
admin lint       ruff check
admin fmt        ruff format
admin vet        lint + typecheck + test
admin pull-baseline  copy the host store down into ./data
admin host-exec <...> run one plexdb command in the deployed container, now
admin dev check  report the store's health read-only; non-zero if it is behind or damaged
admin logs live  tail the container on the host
admin diff       show run-config drift between the container and the last deploy
admin docs       serve the docs site
admin explore    serve the read-only tag explorer on http://localhost:5194
```

`admin deploy` takes a copy of the live store on the host first, then builds for `linux/amd64`
here, ships the image over ssh with `docker save | docker load`, recreates the container
from the `[docker_run]` table — nothing pulls from a registry — and finishes by running
`plexdb check` inside it, so a startup migration that rolled itself back ends the deploy. **The migration runs as the
container starts** (`plexdb/schedule.py`), so the store matches the code that was just deployed
rather than waiting for the next sweep; it takes its own pre-migration copy and rolls itself back
on any row loss. After it, the container serves the read-only tag explorer over the snapshot on
port 5194, published only on the host address `PLEXDB_EXPLORE_BIND` names — it has no login.
The container's mounts and credentials are read from
`/boot/config/plexdb.env` **on the host**, never from a checkout's `.env`: those mount paths
name directories on the Unraid box, and resolving them from a laptop would mount a laptop
path onto the server.

## Configuration

All connection details come from `.env` (gitignored). `.env.example` documents every variable
with placeholders. Never hardcode a URL, token, or hostname, and never put a real value in a
committed file.
