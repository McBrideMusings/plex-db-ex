"""Harvesting crowd lists into `collection` and `collection_membership`.

MDBList is the first source; Letterboxd, Trakt, editorial articles and
subreddit mentions land on top as further values in `collection.source`, never
as a schema change (issues #35–#38).

**Membership is a snapshot, not a fact** — the same discipline `tmdb_edges.py`
applies to edges. On re-pull, a list's whole membership set is deleted and
rewritten inside one transaction, so a title the list dropped disappears rather
than going stale. MDBList marks some of its lists `dynamic`, meaning the source
regenerates them; appending to one of those would accumulate every title that
had ever passed through it.

**No score is computed here** (ADR-0012). `rank` is array position, `size` and
`likes` are what MDBList reported, and nothing multiplies, normalises or
combines them. A consumer wanting one number per membership computes it from
the four raw columns, which is why all four are stored.

**Only titles this store already walked are representable.**
`collection_membership.item_id` references `items`, populated exclusively by
`plexdb walk` (ADR-0005). An entry naming a title nobody walked has ids and
nothing else, so it is counted in `entries_not_in_library` and dropped, never
stored under an invented id — the same rule and the same reasoning as
`tmdb_edges.py`. `collection.size` still records the list's full length, so a
consumer can tell it is looking at a twelfth of a list rather than all of it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .identity import PRIORITY
from .mdblist_client import MDBListSource
from .staleness import DEFAULT_STALE_DAYS, is_stale

#: The value written to `collection.source` by this module's MDBList harvest.
MDBLIST_SOURCE = "mdblist"


@dataclass
class HarvestStats:
    """What one harvest touched."""

    lists_seen: int = 0
    lists_fetched: int = 0
    lists_cached: int = 0
    memberships_written: int = 0
    #: A list entry naming a title this store has never walked. Not an error;
    #: expected on any library that does not own everything a crowd list names.
    entries_not_in_library: int = 0
    #: A list entry naming a title already written for this same list — two
    #: different ids landing on one `item_id`. The first position wins.
    entries_duplicate: int = 0


def wipe_source(conn: sqlite3.Connection, source: str) -> tuple[int, int]:
    """Delete every collection and membership belonging to one source, leaving
    every other source untouched.

    Returns `(memberships_removed, collections_removed)`. Memberships go first
    and explicitly rather than by cascade: `PRAGMA foreign_keys` is not
    guaranteed on for this connection, so relying on the cascade would leave
    orphan rows on a connection where it happens to be off.
    """
    with conn:
        memberships = conn.execute(
            "DELETE FROM collection_membership WHERE collection_id IN "
            "(SELECT collection_id FROM collection WHERE source = ?)",
            (source,),
        ).rowcount
        collections = conn.execute("DELETE FROM collection WHERE source = ?", (source,)).rowcount
    return memberships, collections


def _resolve_index(conn: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    """`(ns, value, kind)` -> `item_id`, prefetched once for the whole harvest.

    Keyed by all three, not by `(ns, value)`, because that is the table's own
    primary key since schema v5: TMDB and TVDB number movies and shows in two
    separate lists that both start at 1, so movie 1678 and show 1678 are
    unrelated titles. Resolving a list entry without its media type would hand
    a show the movie's identity — the fusion #23 was filed for.
    """
    return {
        (row["ns"], row["value"], row["kind"]): row["item_id"]
        for row in conn.execute("SELECT item_id, ns, value, kind FROM external_ids")
    }


def _resolve(
    index: dict[tuple[str, str, str], str],
    external_ids: tuple[tuple[str, str], ...],
    media_type: str,
) -> str | None:
    """The `item_id` for one list entry, or `None` if this store never walked it.

    Tries namespaces in `identity.PRIORITY` order — the same order
    `derive_item_id` uses — so a title resolves here the way it was named at
    walk time rather than by whichever id the source happened to list first.
    """
    by_ns = {ns: value for ns, value in external_ids}
    for ns in PRIORITY:
        value = by_ns.get(ns)
        if value is None:
            continue
        item_id = index.get((ns, value, media_type))
        if item_id is not None:
            return item_id
    return None


def refresh_mdblist(
    conn: sqlite3.Connection,
    source: MDBListSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> HarvestStats:
    """Refresh every MDBList top list into `collection` and `collection_membership`.

    A list whose `collection` row is inside the staleness window is not
    re-fetched, so a re-run costs one request rather than one per list. Unlike
    the TMDB edge sweep this needs no sentinel row for the cursor: a fetched
    list always leaves a `collection` row behind even when none of its entries
    resolved, so `observed_at` on that row is the cursor.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    now_iso = now.isoformat(timespec="seconds")
    stats = HarvestStats()

    index = _resolve_index(conn)
    observed: dict[str, str] = {
        row["collection_id"]: row["observed_at"]
        for row in conn.execute(
            "SELECT collection_id, observed_at FROM collection WHERE source = ?",
            (MDBLIST_SOURCE,),
        )
    }

    for meta in source.top_lists():
        stats.lists_seen += 1
        collection_id = f"{MDBLIST_SOURCE}:{meta.list_id}"

        seen_at = observed.get(collection_id)
        if seen_at is not None and not is_stale(seen_at, cutoff):
            stats.lists_cached += 1
            continue

        entries = source.list_entries(meta.list_id)

        # Array position is the rank the list presents, 1-indexed. MDBList's
        # own `rank` field is a different ordering entirely and is ignored —
        # see `mdblist_client`'s docstring. A title outside the library is
        # dropped and counted; a title already written for this list keeps its
        # first (stronger) position rather than crashing the harvest on the
        # table's primary key.
        memberships: list[tuple[str, int]] = []
        placed: set[str] = set()
        for position, entry in enumerate(entries, start=1):
            item_id = _resolve(index, entry.external_ids, entry.media_type)
            if item_id is None:
                stats.entries_not_in_library += 1
                continue
            if item_id in placed:
                stats.entries_duplicate += 1
                continue
            placed.add(item_id)
            memberships.append((item_id, position))

        url = (
            f"https://mdblist.com/lists/{meta.user_name}/{meta.slug}"
            if meta.user_name and meta.slug
            else None
        )

        with conn:
            conn.execute(
                "DELETE FROM collection_membership WHERE collection_id = ?", (collection_id,)
            )
            conn.execute(
                "INSERT INTO collection "
                "(collection_id, source, name, url, size, likes, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(collection_id) DO UPDATE SET "
                "source = excluded.source, name = excluded.name, url = excluded.url, "
                "size = excluded.size, likes = excluded.likes, "
                "observed_at = excluded.observed_at",
                (collection_id, MDBLIST_SOURCE, meta.name, url, meta.size, meta.likes, now_iso),
            )
            for item_id, rank in memberships:
                conn.execute(
                    "INSERT INTO collection_membership "
                    "(collection_id, item_id, rank, mentions, observed_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (collection_id, item_id, rank, now_iso),
                )

        stats.lists_fetched += 1
        stats.memberships_written += len(memberships)

    return stats
