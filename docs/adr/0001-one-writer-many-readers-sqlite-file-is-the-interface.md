# One writer, many readers; the SQLite file is the interface

Superseded by [ADR-0031](./0031-readers-reach-the-store-only-through-the-http-api) where it says a consumer opens the file and the schema is the public API: consumers call the explorer's HTTP API, and its routes are the public API.

The store is one SQLite file, `plexdb.db`, written by exactly one process: the `plexdb`
Rust binary in this repo's Cargo workspace, which compiles against the same schema crate
as the read-only `plexdb-reader`. Until every sweep step has moved to Rust, the Python
package `plexdb` runs the steps that have not, and both take the same writer lock, so one
process writes at a time. Every other consumer opens the file read-only. The schema is the
public API: there is no server and no IPC, and a schema change breaks every consumer at
once with no version negotiation. Readers and the writer share a host, or each reader gets
its own copy, because SQLite over a network share is not safe.
