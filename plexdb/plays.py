"""Ingesting Plex watch history into `plays`, and enriching it from Tautulli.

ADR-0004: the store owns watch history itself, with Plex as the source that
always works and Tautulli an optional adapter that fills in what Plex's
history cannot. `HistorySource` is the seam `ingest_plays` uses for the Plex
implementation; `TautulliSource` and `match_tautulli_history` below are
issue #9's adapter, layered on top of whatever `ingest_plays` already wrote —
Tautulli never creates a `plays` row, only fills columns on one that exists.

**Plex history is a watched-it ledger, not a play log.** Measured against a
live server: of 268 plays finished at 90% or more, 261 appear in Plex's
history; of 17 plays abandoned under 40%, only 3 do. So this ingest delivers
positive signal only — `ip`, `percent_complete`, and `paused_counter` land
null, which is the normal state of a Plex-only deployment, not a degraded
one.

**Resolution goes through the walk's rating-key map.** Plex's history
identifies a title only by `ratingKey`; `plex_items` (populated by
`plexdb walk`) is what turns that into an `item_id`. An event whose rating
key has no row there means the walk and the history have diverged — it is
counted (`unresolved_rating_key`) and reported, never dropped silently.

**Idempotent.** Each event carries its own `historyKey` — Plex's identity
for that one *viewing*, not to be confused with `ratingKey`, the *title's*
identity. `plays.history_key` is the primary key, so a re-ingest that
reaches an already-recorded event is a no-op (`ON CONFLICT ... DO NOTHING`).

**Incremental, without losing an unresolved event.** A naive watermark —
"the newest `viewed_at` already written" — breaks the "never dropped
silently" promise: an event skipped as unresolved is never written, so that
watermark would advance past it the moment any newer event succeeds, and it
would never be asked for again even after a later walk resolves it.
`plays_ingest_cursor` instead tracks the oldest event still outstanding: if
this run saw any unresolved event, the next cutoff is pinned at the oldest
one of those, so it — and everything at or after it — is asked for again
next time. Only once a run sees *no* unresolved events does the cutoff
advance, to the newest event that run actually saw.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol


class HistorySource(Protocol):
    """The read surface `ingest_plays` needs — real or recorded.

    Two methods, both read-only, mirroring the shape `PlexSource` uses for
    the library walk: an event list and a device list, so a recorded
    fixture and a live response are interchangeable here too.
    """

    def history(self, *, since_viewed_at: int | None = None) -> list[dict[str, Any]]:
        """Every history event at or after `since_viewed_at` (or all of
        them, if `None`), in any order — `ingest_plays` does not depend on
        it."""
        ...

    def devices(self) -> list[dict[str, Any]]:
        """Every device Plex has ever seen a client connect from."""
        ...


@dataclass
class PlaysStats:
    """What one ingest touched — the summary `plexdb ingest-plays` prints."""

    events_seen: int = 0
    plays_written: int = 0
    #: Already recorded by a prior ingest (same history_key). Expected on any
    #: run after the first; zero on a clean incremental fetch.
    already_recorded: int = 0
    #: An event whose rating_key has no row in plex_items — the walk and the
    #: history have diverged. Counted and reported, never dropped silently.
    unresolved_rating_key: int = 0
    #: An event whose deviceID has no row in Plex's device list. Still
    #: written, with a null client_identifier and platform.
    unresolved_device: int = 0


def _device_cache(source: HistorySource) -> dict[str, tuple[str | None, str | None]]:
    """`deviceID -> (clientIdentifier, platform)`, fetched once per ingest.

    An empty `clientIdentifier` (Plex reports one for at least one real
    device on the live server) is treated the same as an absent one — it
    identifies nothing.
    """
    cache: dict[str, tuple[str | None, str | None]] = {}
    for device in source.devices():
        device_id = device.get("id")
        if device_id is None:
            continue
        client_identifier = device.get("clientIdentifier") or None
        platform = device.get("platform") or None
        cache[str(device_id)] = (client_identifier, platform)
    return cache


def _cursor(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1").fetchone()
    return row[0] if row is not None else None


def _advance_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        """
        INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET since_viewed_at = excluded.since_viewed_at
        """,
        (value,),
    )


def ingest_plays(conn: sqlite3.Connection, source: HistorySource) -> PlaysStats:
    """Ingest every new history event from `source` into `plays`.

    One transaction for the whole pass, same reasoning as `walk_all`: a
    failure partway through leaves the store exactly as it was before the
    ingest started, never half-written.
    """
    stats = PlaysStats()

    since_viewed_at = _cursor(conn)
    devices = _device_cache(source)
    resolved_by_rating_key = {
        row["rating_key"]: row["item_id"]
        for row in conn.execute("SELECT rating_key, item_id FROM plex_items")
    }

    #: The oldest `viewed_at` this run could not resolve. As long as this
    #: stays set, the cursor must not move past it.
    oldest_unresolved: int | None = None
    #: The newest `viewed_at` this run saw at all. Only used to advance the
    #: cursor when nothing this run went unresolved.
    newest_seen: int | None = None

    with conn:
        for event in source.history(since_viewed_at=since_viewed_at):
            stats.events_seen += 1

            history_key = event.get("historyKey")
            raw_rating_key = event.get("ratingKey")
            viewed_at = event.get("viewedAt")
            account_id = event.get("accountID")
            if not history_key or not raw_rating_key or viewed_at is None or account_id is None:
                # Not a real, addressable viewing event — nothing to key a
                # row on.
                continue

            if newest_seen is None or viewed_at > newest_seen:
                newest_seen = viewed_at

            rating_key = str(raw_rating_key)
            item_id = resolved_by_rating_key.get(rating_key)
            if item_id is None:
                stats.unresolved_rating_key += 1
                if oldest_unresolved is None or viewed_at < oldest_unresolved:
                    oldest_unresolved = viewed_at
                continue

            client_identifier: str | None = None
            platform: str | None = None
            raw_device_id = event.get("deviceID")
            if raw_device_id is not None:
                device = devices.get(str(raw_device_id))
                if device is None:
                    stats.unresolved_device += 1
                else:
                    client_identifier, platform = device

            result = conn.execute(
                """
                INSERT INTO plays
                    (history_key, item_id, plex_account_id, client_identifier,
                     platform, viewed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(history_key) DO NOTHING
                """,
                (history_key, item_id, account_id, client_identifier, platform, viewed_at),
            )
            if result.rowcount:
                stats.plays_written += 1
            else:
                stats.already_recorded += 1

        if oldest_unresolved is not None:
            _advance_cursor(conn, oldest_unresolved)
        elif newest_seen is not None:
            _advance_cursor(conn, newest_seen)
        # Nothing seen at all: leave the cursor exactly where it was.

    return stats


# --- Tautulli adapter (issue #9) -------------------------------------------
#
# Plex's own history has no equivalent of `historyKey` in Tautulli's
# `get_history` response, so a Tautulli row cannot be resolved to a `plays`
# row by a shared primary key the way a re-ingested Plex event can. The match
# is composite instead, measured against the live server (see issue #9):
#
# - `plays.plex_account_id` and Tautulli's `user_id` are the same id space,
#   byte-identical.
# - `plays.client_identifier` and Tautulli's `machine_id` are the same id
#   space, byte-identical.
# - Only the timestamp is soft: Plex's `viewed_at` sits close to, but not
#   exactly on, Tautulli's `stopped` (~218s before, at the median).
#
# So: hard-match on (item_id via rating_key, plex_account_id, client_identifier),
# then take the play whose `viewed_at` is nearest to the row's `stopped`,
# within MATCH_WINDOW_SECONDS. Tautulli's own row `id` is stored on the play
# it lands on, which is what makes a re-run idempotent and stops one Tautulli
# row from enriching two different plays.

#: ±900 seconds. Measured against the live server over 197 completed rows:
#: this window matched 145 of the 151 that had any candidate play at all;
#: ±300s would have matched only 110. A named constant, not a magic number,
#: because the measurement behind it is the reason for the value, not taste.
MATCH_WINDOW_SECONDS = 900


class TautulliSource(Protocol):
    """The read surface `match_tautulli_history` needs from Tautulli — real
    or recorded."""

    def history(self) -> list[dict[str, Any]]:
        """Every history row Tautulli currently holds, completed and
        in-progress alike, in any order."""
        ...


@dataclass
class TautulliMatchStats:
    """What one Tautulli match sweep touched — the summary
    `plexdb enrich-tautulli-plays` prints."""

    rows_seen: int = 0
    #: `id` is null: an in-progress or paused session Tautulli has not yet
    #: written to its own history table. Not history yet; skipped, not
    #: counted as unmatched.
    rows_in_progress: int = 0
    rows_matched: int = 0
    #: Already carries this exact tautulli_id from a prior run. Expected on
    #: every run after the first that re-lists an already-matched session.
    rows_already_matched: int = 0
    #: A completed row this sweep could not attach to any play: its hard
    #: keys match no play, every hard-key match sits outside
    #: MATCH_WINDOW_SECONDS, or every candidate is already claimed by a
    #: different Tautulli row.
    rows_unmatched: int = 0
    #: `plays` rows carrying no Tautulli data at all, counted fresh from the
    #: store after this sweep — not just what this run left unmatched, so it
    #: also reflects plays from before the adapter was ever configured. The
    #: number a silently-empty match would otherwise hide.
    plays_without_tautulli_data: int = 0


def _candidate_plays(
    conn: sqlite3.Connection,
) -> dict[tuple[str, int, str | None], list[dict[str, Any]]]:
    """Every existing play, bucketed by the hard-key tuple a Tautulli row
    must share to be a candidate match: `(item_id, plex_account_id,
    client_identifier)`.

    Plain dicts, not `sqlite3.Row`, so a successful match can flip a
    candidate's `tautulli_id` in place — the in-memory claim a later row in
    the same sweep must see, since the DB write alone would not be visible
    without re-querying mid-loop.
    """
    buckets: dict[tuple[str, int, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT history_key, item_id, plex_account_id, client_identifier, viewed_at, "
        "tautulli_id FROM plays ORDER BY viewed_at"
    ):
        key = (row["item_id"], row["plex_account_id"], row["client_identifier"])
        buckets[key].append(
            {
                "history_key": row["history_key"],
                "viewed_at": row["viewed_at"],
                "tautulli_id": row["tautulli_id"],
            }
        )
    return buckets


def match_tautulli_history(conn: sqlite3.Connection, source: TautulliSource) -> TautulliMatchStats:
    """Enrich existing `plays` rows with Tautulli's `ip`, `percent_complete`,
    `paused_counter`, and `seconds_watched`.

    Never creates a `plays` row — only `ingest_plays` does that. One
    transaction for the whole pass: a failure partway through leaves the
    store exactly as it was before this sweep started.
    """
    stats = TautulliMatchStats()

    resolved_by_rating_key = {
        row["rating_key"]: row["item_id"]
        for row in conn.execute("SELECT rating_key, item_id FROM plex_items")
    }
    candidates = _candidate_plays(conn)

    with conn:
        for tautulli_row in source.history():
            stats.rows_seen += 1

            row_id = tautulli_row.get("id")
            if row_id is None:
                stats.rows_in_progress += 1
                continue

            raw_rating_key = tautulli_row.get("rating_key")
            user_id = tautulli_row.get("user_id")
            stopped = tautulli_row.get("stopped")
            if raw_rating_key in (None, "") or user_id is None or stopped is None:
                stats.rows_unmatched += 1
                continue

            item_id = resolved_by_rating_key.get(str(raw_rating_key))
            if item_id is None:
                stats.rows_unmatched += 1
                continue

            # Same "empty is absent" treatment ingest_plays gives an empty
            # Plex clientIdentifier.
            machine_id = tautulli_row.get("machine_id") or None
            key = (item_id, user_id, machine_id)

            best: tuple[int, dict[str, Any]] | None = None
            for play in candidates.get(key, ()):
                claimant = play["tautulli_id"]
                if claimant is not None and claimant != row_id:
                    # Already matched to a different Tautulli row — not a
                    # candidate, per issue #9's decision.
                    continue
                delta = abs(play["viewed_at"] - stopped)
                if delta > MATCH_WINDOW_SECONDS:
                    continue
                if best is None or delta < best[0]:
                    best = (delta, play)

            if best is None:
                stats.rows_unmatched += 1
                continue

            play = best[1]
            if play["tautulli_id"] == row_id:
                # Idempotent re-run: this row already landed on this play.
                stats.rows_already_matched += 1
                continue

            conn.execute(
                "UPDATE plays SET ip = ?, percent_complete = ?, paused_counter = ?, "
                "seconds_watched = ?, tautulli_id = ? WHERE history_key = ?",
                (
                    tautulli_row.get("ip_address"),
                    tautulli_row.get("percent_complete"),
                    tautulli_row.get("paused_counter"),
                    # seconds_watched is duration, copied verbatim: duration
                    # is already net of paused time, so subtracting
                    # paused_counter here would subtract it a second time.
                    tautulli_row.get("duration"),
                    row_id,
                    play["history_key"],
                ),
            )
            play["tautulli_id"] = row_id
            stats.rows_matched += 1

        stats.plays_without_tautulli_data = conn.execute(
            "SELECT count(*) FROM plays WHERE tautulli_id IS NULL"
        ).fetchone()[0]

    return stats
