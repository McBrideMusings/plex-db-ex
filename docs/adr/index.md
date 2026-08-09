# Decisions

Architecture decision records. Where one of these and [the spec](../PRD) disagree, the ADR wins.

| # | Decision |
|---|---|
| [0001](./0001-one-writer-many-readers-sqlite-file-is-the-interface) | One writer, many readers; the SQLite file is the interface |
| [0002](./0002-item-id-is-the-entry-id-string) | `item_id` is etv-station's `entry_id` string, with a side table of external ids |
| [0003](./0003-rust-reader-crate-behind-a-plugin-capability-grant) | A typed Rust reader crate, exposed to Rhai plugins behind a capability grant |
| [0004](./0004-the-store-owns-watch-history) | The store owns watch history: Plex required, Tautulli an optional adapter |
| [0005](./0005-the-store-walks-plex-itself-and-augments-never-replaces) | The store walks Plex itself, and augments Plex rather than replacing it |
