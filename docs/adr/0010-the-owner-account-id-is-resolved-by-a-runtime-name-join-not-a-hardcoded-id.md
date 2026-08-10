# The owner's account id is resolved by a runtime name join, not a hardcoded id

`enrich-tautulli-plays` fetches Plex's `/accounts` and Tautulli's `get_users` every run and joins
them on account name into a `tautulli_user_id -> plex_account_id` map
(`plays.build_account_id_map`). A Tautulli history row's `user_id` is translated through that map
before it becomes part of the hard match key `plays.match_tautulli_history` already used
(`item_id`, `plex_account_id`, `client_identifier`) — see `docs/schema.md`'s Version 4 section.

## Why this needs a ruling

[Issue #26](https://github.com/McBrideMusings/plex-db-ex/issues/26): the match rule required
`plays.plex_account_id == user_id` as a hard key, on the assumption — true for every other account,
confirmed against the live server — that Plex and Tautulli agree on account ids. They don't, for
exactly one account: Plex's history stores whoever *owns the server* under the local account id
`1`, while Tautulli reports that same person under their plex.tv account id (`22831969` on the live
server) instead. The server owner's 2,563 plays never matched anything, silently: 0 enriched, 0
IPs, every one of them counted in the adapter's own unmatched total with no signal pointing at why.

## Considered options

- **A runtime join on account name.** Chosen. Plex's `/accounts` and Tautulli's `get_users` both
  report every account by name as well as by id, and the name is the one field confirmed identical
  between the two systems for the account whose id differs. Joining on it needs no server-specific
  configuration: a different server has a different owner id, and the join resolves it the same way
  regardless. Every non-owner account's name join lands on the same id it already had — a same-id
  no-op, not a special case — so nothing about the owner is hardcoded or singled out in the match
  logic itself.
- **A hardcoded config knob (e.g. `TAUTULLI_OWNER_USER_ID`) mapping one Plex id to one Tautulli
  id.** Rejected. Issue #26's acceptance criteria rule this out explicitly: the id must be derived
  at runtime, not configured, because a different server has a different owner id and a config
  value silently drifts stale (a server migration, a re-created Tautulli user) with no signal that
  it has.
- **Match on account name directly instead of id**, dropping `plex_account_id`/`user_id` from the
  key entirely. Rejected: name is not guaranteed unique or stable the way an id is (a Plex home
  user can rename their profile; Tautulli's `username` and `friendly_name` already diverge on the
  live server for the shared accounts). Using name only as a one-time id-resolution key, and
  keeping id as the actual match key, keeps the existing match rule's stability guarantees intact.

## Consequences

`enrich-tautulli-plays` now requires `PLEX_URL`/`PLEX_TOKEN` in addition to
`TAUTULLI_URL`/`TAUTULLI_API_KEY` — previously it read only existing `plays` rows from the store
and never talked to Plex directly. This adds no new external dependency (ADR-0004): Plex is the
store's one required source everywhere else already, and `/accounts` is a Plex endpoint.

An account name present on one system's account list and absent on the other's is not silently
dropped from the join — `AccountIdMap.tautulli_only_names` / `.plex_only_names` carry it, and
`enrich-tautulli-plays` prints it. A name join, unlike an id join, can go quietly wrong in a way an
operator needs the chance to notice: a renamed account or a Plex/Tautulli account pair that never
matched by name at all would otherwise resolve to nothing, with no signal that it should have
resolved to something.

Account id `0` is excluded from the join on both sides before it runs, by id rather than by name —
Plex's own placeholder account (empty `name` on the live server) and Tautulli's `"Local"` row for
unauthenticated sessions are both not a person, and an id-0-to-id-0 join would otherwise pair the
two placeholders as though they were the same account.
