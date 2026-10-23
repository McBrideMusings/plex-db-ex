# A typed Rust reader crate, exposed to Rhai plugins behind a capability grant

This repo ships `plexdb-reader`, a read-only Rust crate. `etv-station`'s core links it and
exposes its methods — `keywords_for(item_id)`, `edges_from(item_id, edge_type)` and the
rest — to Rhai plugins only when the plugin's channel YAML grants the datastore capability;
a plugin without the grant cannot reach the store. A schema change breaks `etv-station`'s
build.
