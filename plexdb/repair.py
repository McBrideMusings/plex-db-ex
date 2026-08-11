"""Splitting identities that fused two unrelated titles into one row (issue #23).

Before schema version 5, `external_ids` was keyed on `(ns, value)` alone. TMDB
and TVDB number movies, shows and episodes in separate lists that all start at
1, so a bare `tmdb://1678` claimed one row for *Godzilla* (1954) and *The
Golden Girls* (1985) alike, and `walk._resolve_existing` handed the second one
the first one's identity.

Version 5 puts the media kind in the key, so no *new* fusion can happen. It
cannot undo the ones already recorded: both rating keys are already in
`plex_items` pointing at the fused id, so a plain re-walk finds that identity
by rating key and ADR-0008 correctly keeps it. Undoing them takes an explicit
pass that deletes the fused rows and lets the walk derive them again from Plex.

**A fused identity is one whose Plex records are not all the same media kind.**
That is the only thing version 5 stopped happening, so it is the only thing
worth undoing. Answering it needs Plex: `plex_items` records where Plex keeps
a title but not what Plex says it is, and `items.type` describes the identity,
which is the very thing a fusion corrupted. `kinds_by_rating_key` enumerates
the library to get it.

The tempting local test — an identity holding two ids in one namespace — is
wrong, and using it churns the store forever. See `fused_item_ids`.

**Plays split into two populations, and only one of them costs anything.**
From schema version 5 a `plays` row carries its own `rating_key`, which is
enough to place it again after a split: the fused `items` row is left standing
while its Plex mapping is removed, so the play is never deleted, only *moved*
onto whatever identity its rating key resolves to once the walk has run. No
history re-read, nothing held in memory across a walk that takes minutes, and
no instant at which the play exists nowhere.

A row written *before* version 5 has no `rating_key` — it recorded the
conclusion it reached and not the evidence — so once its identity splits there
is no way to say which half it belongs to. Those are deleted and re-read from
history: `plays_ingest_cursor` is rewound to the oldest of *them* specifically,
never to reach a row that is about to be moved anyway. Rows Plex's history no
longer covers are genuinely lost, which is why this pass is scoped to the fused
identities rather than rebuilding the whole store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from .plex_client import PlexSource
from .walk import _SECTION_PASSES, WalkStats, walk_all

#: How many ids to name in one `IN (...)` clause. SQLite caps the number of
#: bound parameters per statement, and a repair on a large library can find
#: thousands of fused identities.
_CHUNK = 500


@dataclass
class RepairStats:
    """What one repair pass found and touched."""

    #: Identities whose Plex records span more than one media kind.
    fused_found: int = 0
    #: Plays carried across the split by their own `rating_key` — written
    #: back against whichever identity that rating key resolves to after the
    #: re-walk. No history re-read, nothing lost.
    plays_repointed: int = 0
    #: Plays that went with their identity and must be re-read from history:
    #: rows written before schema v5, which carry no `rating_key`, so nothing
    #: records which Plex item each one came from.
    plays_dropped: int = 0
    #: Plays whose `rating_key` no longer resolves after the re-walk — the
    #: title left the library between the snapshot and the walk. Neither
    #: repointed nor recoverable by re-ingest, so counted separately rather
    #: than folded into either.
    plays_orphaned: int = 0
    #: The `viewed_at` the ingest cursor was rewound to, or `None` when
    #: nothing had to be dropped and the cursor was left alone.
    cursor_rewound_to: int | None = None
    #: The walk that re-derived the deleted titles.
    walk: WalkStats | None = None


def kinds_by_rating_key(source: PlexSource) -> dict[str, str]:
    """Every rating key Plex currently holds, and the media kind it holds it as.

    Enumerated the same way `walk_all` enumerates — section by section, one
    pass per kind — because that is the only place the kind of a *rating key*
    exists. The store cannot answer it: `plex_items` records where Plex keeps
    a title, and `items.type` is a property of the identity, which is exactly
    what a fusion corrupts.
    """
    kinds: dict[str, str] = {}
    for section in source.sections():
        passes = _SECTION_PASSES.get(section.type)
        if passes is None:
            continue
        for kind, plex_type in passes:
            for record in source.items(section.key, plex_type):
                rating_key = record.get("ratingKey")
                if rating_key:
                    kinds[str(rating_key)] = kind
    return kinds


def fused_item_ids(conn: sqlite3.Connection, kinds: dict[str, str]) -> list[str]:
    """Every identity whose Plex records are not all the same media kind.

    That — and only that — is what schema version 5 stopped happening. A
    movie and a TV show sharing a TMDB number were merged into one identity
    because the external-id key had no media type in it; putting the kind in
    the key means it cannot recur.

    **Two ids in one namespace is the wrong test**, and using it does real
    damage. Plex reports every match it holds, so one record can legitimately
    carry several ids in a namespace: *The Animatrix* carries nine TMDB ids,
    one per segment, on a single rating key. Two records of the same kind can
    also legitimately merge and disagree — South Park S28E1 exists twice in
    the library, the two copies share a TVDB id (so version 5 merges them, as
    issue #19 asked) but were matched to different IMDb entries. Neither is a
    fusion, and deleting either one re-walks it into byte-identical rows, so a
    detector built on that test never reaches a fixed point: it reports the
    same titles on every run and drops their plays every time. Measured on the
    author's library, it churned 25 identities forever.

    A rating key Plex no longer holds has no kind to compare, so it cannot
    prove a disagreement and is skipped rather than guessed at.

    Sorted, so two runs over unchanged data do the same thing in the same
    order and the report is stable.
    """
    by_item: dict[str, set[str]] = {}
    for row in conn.execute("SELECT rating_key, item_id FROM plex_items"):
        kind = kinds.get(row["rating_key"])
        if kind is not None:
            by_item.setdefault(row["item_id"], set()).add(kind)
    return sorted(item_id for item_id, seen in by_item.items() if len(seen) > 1)


def _oldest_play_without_rating_key(
    conn: sqlite3.Connection, item_ids: Sequence[str]
) -> int | None:
    """The earliest `viewed_at` among plays that cannot be carried across.

    Deliberately ignores plays that *do* carry a rating key: those are written
    back after the walk, so rewinding the cursor to reach them would re-read
    history the store is about to hold again anyway.
    """
    oldest: int | None = None
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT MIN(viewed_at) AS oldest FROM plays "
            f"WHERE rating_key IS NULL AND item_id IN ({placeholders})",
            tuple(chunk),
        ).fetchone()
        if row["oldest"] is not None and (oldest is None or row["oldest"] < oldest):
            oldest = int(row["oldest"])
    return oldest


def _count_plays_without_rating_key(conn: sqlite3.Connection, item_ids: Sequence[str]) -> int:
    """Plays on `item_ids` that record no rating key, so cannot be placed.

    Written before schema version 5: they hold the conclusion the ingest
    reached and no evidence of where it came from, so once their identity
    splits in two there is no way to say which half they belong to.
    """
    total = 0
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM plays "
            f"WHERE rating_key IS NULL AND item_id IN ({placeholders})",
            tuple(chunk),
        ).fetchone()
        total += int(row["n"])
    return total


def repoint_plays(conn: sqlite3.Connection) -> int:
    """Move every play onto whatever identity its own rating key now holds.

    One statement over the whole table, and the reason the repair never has
    to hold a play in memory: a play is only ever *moved*, never deleted and
    re-inserted, so there is no window in which it exists nowhere. Returns how
    many moved.

    Idempotent and unconditional, which is what makes the repair crash-safe.
    A pass that dies after deleting a fused identity's `plex_items` rows but
    before the walk has rebuilt them leaves its plays sitting on an identity
    that is no longer anywhere in `plex_items`; the next run's call to this
    puts them right, because the question it asks — "does this play's rating
    key resolve somewhere other than where the play is?" — needs no memory of
    what the failed run intended.

    A rating key with no `plex_items` row is a title that has left the
    library. Its play is left exactly where it is rather than being moved to a
    guess or deleted; `orphaned_plays` counts those.
    """
    cursor = conn.execute(
        "UPDATE plays SET item_id = ("
        "    SELECT p.item_id FROM plex_items p WHERE p.rating_key = plays.rating_key"
        ") "
        "WHERE rating_key IS NOT NULL "
        "  AND EXISTS (SELECT 1 FROM plex_items p WHERE p.rating_key = plays.rating_key) "
        "  AND item_id <> ("
        "    SELECT p.item_id FROM plex_items p WHERE p.rating_key = plays.rating_key"
        ")"
    )
    return cursor.rowcount


def orphaned_plays(conn: sqlite3.Connection, item_ids: Sequence[str]) -> int:
    """Plays still on `item_ids` whose rating key resolves nowhere.

    The title left Plex between one pass and the next, so nothing can say
    which identity the play belongs to now. Counted, never guessed at.
    """
    total = 0
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM plays "
            f"WHERE rating_key IS NOT NULL AND item_id IN ({placeholders}) "
            f"  AND NOT EXISTS (SELECT 1 FROM plex_items p WHERE p.rating_key = plays.rating_key)",
            tuple(chunk),
        ).fetchone()
        total += int(row["n"])
    return total


def repair(
    conn: sqlite3.Connection,
    source: PlexSource,
    source_roots: Sequence[str] = (),
) -> RepairStats:
    """Split every fused identity, carrying its plays across where it can.

    **A play is never deleted and re-inserted, only moved.** The fused
    identity's `items` row is left standing while its `plex_items` and
    `external_ids` rows are deleted — enough for the walk to derive each rating
    key afresh, and not enough to cascade its plays away. After the walk,
    `repoint_plays` moves each play onto whatever identity its own rating key
    now holds. At no instant does a play exist nowhere, so no failure between
    the steps can lose one, and no snapshot has to survive in memory across a
    walk that takes minutes.

    Once its plays have moved off it, the emptied fused row is deleted. A row
    still holding plays is kept: those are plays whose rating key has left
    Plex, and dropping the row would take real viewing history with it.

    A play written before schema v5 carries no `rating_key`, so it records the
    conclusion the ingest reached and no evidence of where it came from. Those
    genuinely cannot be placed once the identity splits; they are deleted and
    `plays_ingest_cursor` is rewound far enough to read them back from Plex.

    **Re-running after a failure repairs it.** `repoint_plays` asks only "does
    this play's rating key resolve somewhere other than where the play is",
    which needs no memory of what the interrupted run intended, and the walk
    is idempotent.
    """
    stats = RepairStats()
    item_ids = fused_item_ids(conn, kinds_by_rating_key(source))
    stats.fused_found = len(item_ids)
    if not item_ids:
        # Still worth a pass: an earlier run may have died between deleting a
        # identity's plex_items rows and moving its plays off it, which leaves
        # nothing looking fused but plays sitting on a stale identity.
        with conn:
            stats.plays_repointed = repoint_plays(conn)
        return stats

    stats.plays_dropped = _count_plays_without_rating_key(conn, item_ids)
    # Only the plays that cannot be carried across justify a rewind. Rewinding
    # to reach a play that is about to be moved would re-read history the store
    # already holds.
    oldest = _oldest_play_without_rating_key(conn, item_ids) if stats.plays_dropped else None

    with conn:
        for start in range(0, len(item_ids), _CHUNK):
            chunk = item_ids[start : start + _CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            # Deliberately NOT `DELETE FROM items`: that cascades to `plays`,
            # and a play deleted here would have to survive in memory across
            # the walk to come back. Removing the identity's Plex mapping and
            # its external ids is all the walk needs to derive each rating key
            # afresh — `enrichment` and `edges` are keyed on the item and are
            # rebuilt by their own sweeps.
            conn.execute(f"DELETE FROM plex_items WHERE item_id IN ({placeholders})", tuple(chunk))
            conn.execute(
                f"DELETE FROM external_ids WHERE item_id IN ({placeholders})", tuple(chunk)
            )
            # These cannot be moved by any later pass, so they go now, in the
            # same transaction as the rewind that will read them back.
            conn.execute(
                f"DELETE FROM plays WHERE rating_key IS NULL AND item_id IN ({placeholders})",
                tuple(chunk),
            )

        if oldest is not None:
            # Only ever moves the cursor backwards. Advancing it here would
            # skip events the store still has no row for.
            row = conn.execute(
                "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, ?)",
                    (oldest,),
                )
                stats.cursor_rewound_to = oldest
            elif oldest < int(row["since_viewed_at"]):
                conn.execute(
                    "UPDATE plays_ingest_cursor SET since_viewed_at = ? WHERE id = 1",
                    (oldest,),
                )
                stats.cursor_rewound_to = oldest
            else:
                stats.cursor_rewound_to = int(row["since_viewed_at"])

    stats.walk = walk_all(conn, source, source_roots)

    # After the walk, and only then: the identity a rating key resolves to did
    # not exist until it ran.
    with conn:
        stats.plays_repointed = repoint_plays(conn)
        stats.plays_orphaned = orphaned_plays(conn, item_ids)
        for start in range(0, len(item_ids), _CHUNK):
            chunk = item_ids[start : start + _CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            # The fused rows have served their purpose — they held the plays
            # while the walk ran. Delete only the ones nothing points at any
            # more; a row still holding plays is one whose rating key left
            # Plex, and taking it would take that viewing history with it.
            conn.execute(
                f"DELETE FROM items WHERE item_id IN ({placeholders}) "
                f"  AND NOT EXISTS (SELECT 1 FROM plays WHERE plays.item_id = items.item_id) "
                f"  AND NOT EXISTS "
                f"      (SELECT 1 FROM plex_items WHERE plex_items.item_id = items.item_id)",
                tuple(chunk),
            )
    return stats
