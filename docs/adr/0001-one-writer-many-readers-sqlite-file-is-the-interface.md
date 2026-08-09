# One writer, many readers; the SQLite file is the interface

The store is a single SQLite database file. Exactly one process writes it — a Python
package that absorbs the acquisition code already running in `curator`
(`curator/clients/`, `curator/harvest/`, `curator/resolve/`, `curator/enrich/`, and the
`keyword_cache` / `resolution_cache` tables in `curator/curator/schema.sql`). Every other
consumer opens the same file read-only. The schema is the public API; there is no server,
no IPC, and no shared library across languages.

## Considered options

- **A Rust crate instead**, native to `etv-station` (the first consumer, already a Rust
  workspace using `rusqlite` for its own `catalog.db`). Rejected: it leaves `curator`'s
  TMDB/Trakt/MDBList crawlers in place, so the same titles get fetched twice against the
  same rate limits, into two caches that immediately drift.
- **A service with an HTTP API.** Rejected: §1 of the spec puts this explicitly out of the
  request path — batch enrichment, cached results, offline scoring. A server buys nothing
  a file read doesn't already give, and adds a process to keep alive.

## Consequences

Both consumers must reach the same file, which constrains deployment: SQLite over a
network share is not safe, so readers and the writer either share a host or the readers
get their own copy.

Readers see the schema, so a schema change is a breaking change for every consumer at
once. There is no version negotiation.
