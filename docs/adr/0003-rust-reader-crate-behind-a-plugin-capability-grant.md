# A typed Rust reader crate, exposed to Rhai plugins behind a capability grant

This repo ships a read-only Rust crate alongside the Python writer. `etv-station`'s core
links it and exposes its methods — `keywords_for(item_id)`, `edges_from(item_id, edge_type)`,
and so on — into the Rhai scripting environment only for plugins whose channel YAML granted
the datastore capability.

## Why this contradicts the spec's wording, and what the wording meant

The spec says "`etv-station` core never links this library." That is not implementable:
`deploy/appdata/plugins/` holds `.rhai` scripts, and a Rhai script cannot open a file or link
a crate. The core is the only thing that *can* open the database. What the sentence protects
is that a channel which did not ask for taste data cannot be affected by it — and the
capability grant is what enforces that, not the absence of a dependency line.

## Considered options

- **A generic `xdb_query(sql, params)` exposed to Rhai**, keeping the core ignorant of the
  schema. Rejected: it moves multi-table joins into a scripting language with no types and no
  test harness. The schema is a shared dependency either way; this variant only makes a
  breaking change fail silently at runtime instead of loudly at build time.
- **No Rust reader — project enrichment into `etv-station`'s own `catalog.db`.** Rejected:
  `catalog.db` has no edge model, and rank and `fetched_at` have nowhere to live, so the
  scorer would get tags but not the affinity graph the whole design rests on.

## Consequences

A schema change breaks `etv-station`'s build. That is the intended failure mode.

`etv-station`'s existing plugin contract is the precedent — its ADR-0002 already hands a
plugin a `query()` function over `catalog.db` rather than the catalog itself, for the same
reason: the plugin narrows the corpus, and only what it asks for is materialised.
