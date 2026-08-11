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

**Plays are the expensive part.** `plays` rows on a fused identity point at an
`item_id` that is about to stop existing, and until version 5 they carried no
`rating_key`, so nothing records which Plex item each one actually came from
and they cannot be re-pointed. They are deleted with the identity and re-read
from history: `plays_ingest_cursor` is rewound to the oldest affected play so
the next `plexdb ingest-plays` asks Plex for that window again. Rows Plex's
history no longer covers are genuinely lost — which is why this pass is scoped
to the fused identities rather than rebuilding the whole store.
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

    #: Identities holding two or more different values in a single namespace.
    fused_found: int = 0
    #: `plays` rows deleted with those identities, to be re-read from history.
    plays_dropped: int = 0
    #: The `viewed_at` the ingest cursor was rewound to, or `None` when no
    #: fused identity had any plays and the cursor was left alone.
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


def _oldest_play(conn: sqlite3.Connection, item_ids: Sequence[str]) -> int | None:
    """The earliest `viewed_at` recorded against any of `item_ids`."""
    oldest: int | None = None
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT MIN(viewed_at) AS oldest FROM plays WHERE item_id IN ({placeholders})",
            tuple(chunk),
        ).fetchone()
        if row["oldest"] is not None and (oldest is None or row["oldest"] < oldest):
            oldest = int(row["oldest"])
    return oldest


def _count_plays(conn: sqlite3.Connection, item_ids: Sequence[str]) -> int:
    total = 0
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM plays WHERE item_id IN ({placeholders})",
            tuple(chunk),
        ).fetchone()
        total += int(row["n"])
    return total


def repair(
    conn: sqlite3.Connection,
    source: PlexSource,
    source_roots: Sequence[str] = (),
) -> RepairStats:
    """Delete every fused identity, re-walk Plex, and rewind the play cursor.

    The delete and the cursor rewind share one transaction, so a failure
    between them cannot leave the store missing rows it will never ask for
    again. The walk runs afterwards in its own transaction — it is idempotent,
    so re-running the whole pass after an interrupted walk is safe.
    """
    stats = RepairStats()
    item_ids = fused_item_ids(conn, kinds_by_rating_key(source))
    stats.fused_found = len(item_ids)
    if not item_ids:
        return stats

    stats.plays_dropped = _count_plays(conn, item_ids)
    oldest = _oldest_play(conn, item_ids)

    with conn:
        for start in range(0, len(item_ids), _CHUNK):
            chunk = item_ids[start : start + _CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            # `items` is the parent of external_ids, plex_items, plays, edges
            # and enrichment, every one of them ON DELETE CASCADE, so this one
            # statement clears the fused identity everywhere it appears.
            conn.execute(f"DELETE FROM items WHERE item_id IN ({placeholders})", tuple(chunk))

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
    return stats
