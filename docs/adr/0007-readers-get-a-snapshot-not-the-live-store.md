# Readers get a snapshot, not the live store

The writer keeps its store in WAL mode and, at the end of a sweep, publishes a consistent
single-file copy with `VACUUM INTO`. Consumers open **that copy**, read-only. Nothing but the
writer ever touches the live database file.

## Why

WAL keeps `-wal` and `-shm` sidecar files beside the database and needs to write them **even for
a read-only connection** — a directory at mode 555 fails an open with `attempt to write a
readonly database`, and the `mode=ro` URI does not help, because it constrains the connection
rather than WAL's need for its sidecars.

That collided with [ADR-0001](./0001-one-writer-many-readers-sqlite-file-is-the-interface.md):
its readers are separate processes in separate containers, and the safest way to deploy one —
mount the store read-only so a bug in a consumer *cannot* corrupt it — was the one arrangement
that did not work.

It also answers the question sitting next to it: **where the store lives and how each consumer
reaches it.** A live SQLite file cannot be safely opened across a network share; a snapshot is an
inert file that can be copied anywhere and opened by anyone. "Readers get a copy" stops being a
worry and becomes the mechanism.

## Considered options

- **Keep WAL and give every reader write access to the directory.** Preserves live reads. Rejected:
  every consumer would hold write access to the writer's file, so a bug in any of them can corrupt
  the store, and a read-only mount stays impossible. It guards the problem by handing out exactly
  the privilege the design says readers must not have.
- **`journal_mode = DELETE` with `mode=ro` readers.** Read-only mounts work. Rejected: a reader is
  locked out while the writer holds a write transaction, and enrichment sweeps hold them
  repeatedly for minutes. A channel generation that needs to schedule now would stall behind a
  sweep.

Both alternatives leave readers contending with the writer over one file. This one removes the
contention rather than scheduling it.

## Consequences

**Freshness is sweep-shaped.** A title enriched five minutes ago is invisible to consumers until
the next snapshot is published. Both known consumers are batch jobs — channel scheduling and
collection generation — so this is acceptable; it would not be if a consumer ever needed to see
enrichment the instant it landed.

**The snapshot path is part of the public interface**, alongside the schema. Consumers are pointed
at it, never at the live file, and that expectation has to be documented as firmly as the tables
are.

**A snapshot is a natural transport across hosts**, which the live file never was. It also gives a
cheap answer to "what did the store look like last week" if snapshots are ever retained.

**Contention largely stops being a concern.** Only the writer contends with itself, which makes the
busy-timeout work a robustness measure rather than a correctness one.
