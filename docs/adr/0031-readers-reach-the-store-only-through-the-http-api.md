# ADR-0031: Readers reach the store only through the HTTP API

The store's SQLite files — `plexdb.db` and every snapshot published from it — are private to this repo's writer and explorer. Every other program reads the store's facts through the explorer's HTTP API: it opens no store file, mounts no folder that holds one to read it, and links no crate that reads one. The API's routes and their JSON shapes are the public interface; the tables behind them may change without a reader noticing.

This supersedes [ADR-0001](./0001-one-writer-many-readers-sqlite-file-is-the-interface), [ADR-0003](./0003-rust-reader-crate-behind-a-plugin-capability-grant) and [ADR-0007](./0007-readers-get-a-snapshot-not-the-live-store) where they say a consumer opens the file or links `plexdb-reader`; the routes are listed in [The store API](../api).
