# Decisions

Architecture decision records. Where one of these and [the original spec](../spec) disagree, the
ADR wins; [the PRD](../PRD) already reflects them.

| # | Decision |
|---|---|
| [0001](./0001-one-writer-many-readers-sqlite-file-is-the-interface) | One writer, many readers; the SQLite file is the interface |
| [0002](./0002-item-id-is-first-hit-wins-over-external-guids) | `item_id` is first-hit-wins over external GUIDs, with a path hash as the floor and a side table of external ids |
| [0003](./0003-rust-reader-crate-behind-a-plugin-capability-grant) | A typed Rust reader crate, exposed to Rhai plugins behind a capability grant |
| [0004](./0004-the-store-owns-watch-history) | The store owns watch history: Plex required, Tautulli an optional adapter |
| [0005](./0005-the-store-walks-plex-itself-and-augments-never-replaces) | The store walks Plex itself, and augments Plex rather than replacing it |
| [0006](./0006-the-identity-fixture-is-the-published-spec-of-the-rule) | The identity fixture is the published spec of the derivation rule |
| [0007](./0007-readers-get-a-snapshot-not-the-live-store) | Readers get a snapshot, not the live store |
| [0008](./0008-the-walk-never-repoints-an-existing-item-id) | The walk never repoints an existing `item_id` when a title's GUID set changes |
| [0009](./0009-an-edge-only-connects-two-items-this-store-already-knows) | An edge only connects two items this store already knows |
| [0010](./0010-the-owner-account-id-is-resolved-by-a-runtime-name-join-not-a-hardcoded-id) | The owner's account id is resolved by a runtime name join, not a hardcoded id |
| [0011](./0011-a-taste-vector-weights-a-season-not-an-episode) | A taste vector weighs one season, damped, and carries no ranking knobs |
| [0012](./0012-the-store-records-what-a-source-said-never-a-score-it-computed) | The store records what a source said, never a score it computed |
| [0013](./0013-bookkeeping-never-shares-a-table-with-facts) | Bookkeeping never shares a table with facts |
| [0014](./0014-the-sweep-is-a-command-not-a-shell-script) | The sweep is a command, not a shell script |
| [0015](./0015-the-schedule-is-a-command-too) | The schedule is a command too |
| [0016](./0016-keywords-are-one-source-agnostic-namespace-with-a-source-column) | Keywords are one source-agnostic namespace with a source column |
| [0017](./0017-the-explorers-client-is-a-thin-credential-less-visualizer) | The explorer's client is a thin, credential-less visualizer |
| [0018](./0018-synonym-keywords-are-a-judges-verdicts-beside-enrichment) | Synonym keywords are a judge's verdicts beside `enrichment` |
