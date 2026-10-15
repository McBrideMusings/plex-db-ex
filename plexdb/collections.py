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

**Membership holds only titles this store already walked.**
`collection_membership.item_id` references `items`, populated exclusively by
`plexdb walk` (ADR-0005). An entry naming a title nobody walked has no `item_id`
to point at, so it is counted in `entries_not_in_library` and left out of
membership, never stored under an invented id — the same rule and the same
reasoning as `tmdb_edges.py`.

**Every entry is stored, owned or not, in `collection_entry`** (ADR-0027), under
the strongest external id the source gave and with the source's own title and
year. A list's entries and memberships are replaced together in one
transaction, so the two never disagree about what the list held at one fetch.
`collection.size` still records the list's full length.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import MDBListError
from .identity import PRIORITY
from .mdblist_client import MDBListSource

# Re-exported (redundant `as` alias) so tests can check that every sweep binds
# the exact same function/constant object rather than a drifted copy — see
# test_staleness.py.
from .staleness import DEFAULT_STALE_DAYS as DEFAULT_STALE_DAYS
from .staleness import is_stale as is_stale

#: The value written to `collection.source` by this module's MDBList harvest.
MDBLIST_SOURCE = "mdblist"

#: `collection.provenance` of a list that arrived from a source's own top lists.
FEED = "feed"


@dataclass
class HarvestStats:
    """What one harvest touched."""

    lists_seen: int = 0
    lists_fetched: int = 0
    lists_cached: int = 0
    #: Entries the source returned, and the distinct `(ns, value, kind)` rows
    #: kept in `collection_entry` (an id listed twice is stored once).
    entries_parsed: int = 0
    entries_stored: int = 0
    memberships_written: int = 0
    #: A list entry naming a title this store has never walked. Not an error;
    #: expected on any library that does not own everything a crowd list names.
    entries_not_in_library: int = 0
    #: A list entry naming a title already written for this same list — two
    #: different ids landing on one `item_id`. The first position wins.
    entries_duplicate: int = 0


def wipe_source(conn: sqlite3.Connection, source: str) -> tuple[int, int, int]:
    """Delete every collection, membership and entry belonging to one source,
    leaving every other source untouched.

    Returns `(memberships_removed, entries_removed, collections_removed)`.
    Memberships and entries go first and explicitly rather than by cascade:
    `PRAGMA foreign_keys` is not guaranteed on for this connection, so relying
    on the cascade would leave orphan rows on a connection where it happens to
    be off.
    """
    owned = "(SELECT collection_id FROM collection WHERE source = ?)"
    with conn:
        memberships = conn.execute(
            f"DELETE FROM collection_membership WHERE collection_id IN {owned}", (source,)
        ).rowcount
        entries = conn.execute(
            f"DELETE FROM collection_entry WHERE collection_id IN {owned}", (source,)
        ).rowcount
        collections = conn.execute("DELETE FROM collection WHERE source = ?", (source,)).rowcount
    return memberships, entries, collections


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
    """Refresh every MDBList top list into `collection`, `collection_entry` and
    `collection_membership`.

    A list whose `collection` row is inside the staleness window and already
    holds entries is not re-fetched, so a re-run costs one request rather than
    one per list. Unlike the TMDB edge sweep this needs no sentinel row for the
    cursor: a fetched list always leaves a `collection` row behind even when
    none of its entries resolved, so `observed_at` on that row is the cursor.
    A list stored before entries were kept has a fresh row and no entries, so it
    is fetched once more.

    Every list prints one log line: its URL, the HTTP status, the entries
    parsed and resolved to the library, and the outcome — `written`, `cached`,
    or `failed` with the reason, after which the error propagates.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    now_iso = now.isoformat(timespec="seconds")
    stats = HarvestStats()

    index = _resolve_index(conn)
    stocked = {
        row["collection_id"]
        for row in conn.execute("SELECT DISTINCT collection_id FROM collection_entry")
    }
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

        url = (
            f"https://mdblist.com/lists/{meta.user_name}/{meta.slug}"
            if meta.user_name and meta.slug
            else None
        )

        seen_at = observed.get(collection_id)
        if seen_at is not None and not is_stale(seen_at, cutoff) and collection_id in stocked:
            stats.lists_cached += 1
            print(f"mdblist: {url or collection_id} -> cached (fetched {seen_at})", flush=True)
            continue

        try:
            contents = source.list_entries(meta.list_id)
        except MDBListError as err:
            print(
                f"mdblist: {url or collection_id} -> HTTP {err.status or 'none'}, failed: {err}",
                flush=True,
            )
            raise
        entries = contents.entries

        # Array position is the rank the list presents, 1-indexed. MDBList's
        # own `rank` field is a different ordering entirely and is ignored —
        # see `mdblist_client`'s docstring. A title outside the library is
        # dropped and counted; a title already written for this list keeps its
        # first (stronger) position rather than crashing the harvest on the
        # table's primary key.
        memberships: list[tuple[str, int]] = []
        stored: list[tuple[int, str, str, str, str | None, int | None]] = []
        placed: set[str] = set()
        listed: set[tuple[str, str, str]] = set()
        for position, entry in enumerate(entries, start=1):
            ns, value = entry.external_ids[0]
            key = (ns, value, entry.media_type)
            if key not in listed:
                listed.add(key)
                stored.append((position, ns, value, entry.media_type, entry.title, entry.year))
            item_id = _resolve(index, entry.external_ids, entry.media_type)
            if item_id is None:
                stats.entries_not_in_library += 1
                continue
            if item_id in placed:
                stats.entries_duplicate += 1
                continue
            placed.add(item_id)
            memberships.append((item_id, position))

        with conn:
            conn.execute("DELETE FROM collection_entry WHERE collection_id = ?", (collection_id,))
            conn.execute(
                "DELETE FROM collection_membership WHERE collection_id = ?", (collection_id,)
            )
            conn.execute(
                "INSERT INTO collection "
                "(collection_id, source, name, url, size, likes, observed_at, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(collection_id) DO UPDATE SET "
                "source = excluded.source, name = excluded.name, url = excluded.url, "
                "size = excluded.size, likes = excluded.likes, "
                "observed_at = excluded.observed_at, provenance = excluded.provenance",
                (
                    collection_id,
                    MDBLIST_SOURCE,
                    meta.name,
                    url,
                    meta.size,
                    meta.likes,
                    now_iso,
                    FEED,
                ),
            )
            conn.executemany(
                "INSERT INTO collection_entry "
                "(collection_id, rank, ns, value, kind, title, year) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(collection_id, *row) for row in stored],
            )
            for item_id, rank in memberships:
                conn.execute(
                    "INSERT INTO collection_membership "
                    "(collection_id, item_id, rank, mentions, observed_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (collection_id, item_id, rank, now_iso),
                )

        stats.lists_fetched += 1
        stats.entries_parsed += len(entries)
        stats.entries_stored += len(stored)
        stats.memberships_written += len(memberships)
        print(
            f"mdblist: {url or collection_id} -> HTTP {contents.status}, "
            f"{len(entries)} entries parsed, {len(memberships)} resolved to the library, written",
            flush=True,
        )

    return stats
