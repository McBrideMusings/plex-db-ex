# The store owns watch history: Plex is the required source, Tautulli an optional adapter

The store ingests watch history itself, into a `plays` table keyed by `item_id`. Plex's own
history is the required source and works alone. A pluggable history-source interface — the
same seam the spec already specifies for per-user external taste sources — has a Tautulli
adapter that, when configured, fills the columns Plex cannot supply. Without it the store
still runs.

## Why the store owns it at all

Nothing persisted per-user watch history before this. `etv-station` reads Tautulli live per
generation and narrows `get_history` to four fields
(`crates/etv-station/src/tautulli.rs:162` keeps `rating_key`, `stopped`, `friendly_name`,
`user`), and its `.history` sidecar (`crates/etv-station/src/history.rs:41`) is a ledger of
what a *channel aired*, not what a person watched. Neither is usable as taste input.

## What each source actually provides — measured, not assumed

**Plex** (`/status/sessions/history/all`): `accountID`, `deviceID`, `ratingKey`, `viewedAt`,
`type`, `title`. No `viewOffset`, no `duration`, no completion percentage, no IP. `deviceID`
joins to `/devices`, which supplies `clientIdentifier` — the client machine ID the fingerprint
tuple's first tier needs — plus `platform` and `name`.

**Tautulli** (`get_history`): all of the above plus `ip_address`, `percent_complete`,
`paused_counter`, `duration`, `player`, `product`.

**Plex history is a watched-it ledger, not a play log.** Over the most recent 300 Tautulli
plays: of 268 plays finished at 90% or more, 261 appear in Plex history. Of 17 plays abandoned
below 40%, 3 appear. So a Plex-only deployment does not get a weak negative signal — it gets
none, because the abandoned play leaves no row.

## Consequences

Plex-only: fingerprint tiers 1 (client machine ID), 3 (platform), 4 (display name), and watch
events. No tier 2 (IP), no abandonment channel, so the negative-signal work and its spike
require the Tautulli adapter.

`plays` therefore carries columns that are populated or null depending on deployment, and any
Layer 2 scorer must treat missing abandonment data as a normal state rather than an error.

Two details from the sampled rows, for the schema:

- Tautulli's `machine_id` matches Plex's `clientIdentifier`, and the Android form already
  embeds the product (`4c2dc76c60b6ca82-com-plexapp-android`), so the fingerprint's third tier
  is partly redundant with its first.
- The negative-signal floor should measure `duration - paused_counter`. `paused_counter`
  reached 182s on a sampled row while `duration` and `play_duration` were identical on every
  row sampled.

## Considered options

- **Tautulli required.** Rejected: the store is meant to depend on Plex and nothing else.
- **Plex-only, hard — no adapter.** Rejected: it permanently deletes the only dislike channel
  available, in a deployment where Tautulli is present anyway.
- **`etv-station` persists history and the reader `ATTACH`es two files.** Rejected: it moves
  fingerprint clustering into Rust, away from the crawlers and away from Python's clustering
  libraries, for no gain.
