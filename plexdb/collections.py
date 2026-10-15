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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import MDBListError, MDBListQuotaError, MDBListUnreachable
from .identity import PRIORITY
from .list_decisions import (
    LETTERBOXD,
    MDBLIST,
    canonical_list_url,
    is_before,
    list_source,
    list_url_parts,
)
from .mdblist_client import MDBListList, MDBListSource

# Re-exported (redundant `as` alias) so tests can check that every sweep binds
# the exact same function/constant object rather than a drifted copy — see
# test_staleness.py.
from .staleness import DEFAULT_STALE_DAYS as DEFAULT_STALE_DAYS
from .staleness import is_stale as is_stale

#: The value written to `collection.source` by this module's MDBList harvest.
MDBLIST_SOURCE = MDBLIST

#: The value written to `collection.source` by `letterboxd_lists.py`.
LETTERBOXD_SOURCE = LETTERBOXD

#: `collection.provenance` of a list that arrived from a source's own top lists.
FEED = "feed"

#: `collection.provenance` of a list a person added by URL (`list_decisions.json`).
ADDED = "added"

#: `collection.provenance` of a list found by searching outward from watched titles.
SEARCH = "search"

#: Failures in a row after which a list is not fetched again until a `retry` decision.
MAX_FAILURES = 3

#: The longest failure reason kept in `list_attempt`.
REASON_MAX = 300


@dataclass
class HarvestStats:
    """What one harvest touched."""

    lists_seen: int = 0
    lists_fetched: int = 0
    lists_cached: int = 0
    #: Lists whose fetch failed this run (recorded in `list_attempt`), lists left alone
    #: because they have failed `MAX_FAILURES` times in a row, and lists a person
    #: excluded or removed.
    lists_failed: int = 0
    lists_stopped: int = 0
    lists_excluded: int = 0
    lists_removed: int = 0
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


def resolve_index(conn: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
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


def drop_lists(conn: sqlite3.Connection, collection_ids: list[str]) -> None:
    """Delete these lists' entries, memberships and collection rows (explicitly, not by
    cascade: `PRAGMA foreign_keys` is not guaranteed on)."""
    with conn:
        for collection_id in collection_ids:
            for table in ("collection_membership", "collection_entry", "collection"):
                conn.execute(f"DELETE FROM {table} WHERE collection_id = ?", (collection_id,))  # noqa: S608


def decisions_for(conn: sqlite3.Connection, source: str) -> dict[str, sqlite3.Row]:
    """The decision rows naming a list of this source, keyed by canonical URL."""
    return {
        row["url"]: row
        for row in conn.execute("SELECT * FROM list_decision")
        if list_source(row["url"]) == source
    }


def apply_removals(
    conn: sqlite3.Connection,
    decisions: dict[str, sqlite3.Row],
    stats: HarvestStats,
    source: str,
) -> set[str]:
    """Act on the `excluded` and `removed` decisions: wipe an excluded list whatever its
    provenance, drop a removed list only if a person added it, and forget either's failed
    fetches. Returns the URLs that must not be fetched."""
    kept: set[str] = set()
    stored = conn.execute(
        "SELECT collection_id, url, provenance FROM collection WHERE source = ?", (source,)
    ).fetchall()
    for url, decision in decisions.items():
        verdict = decision["decision"]
        if verdict not in ("excluded", "removed"):
            continue
        if verdict == "excluded":
            kept.add(url)
        doomed = [
            row["collection_id"]
            for row in stored
            if canonical_list_url(row["url"] or "") == url
            and (verdict == "excluded" or row["provenance"] == ADDED)
        ]
        if doomed:
            drop_lists(conn, doomed)
            stats.lists_removed += len(doomed)
            print(f"{source}: {url} -> {verdict}, dropped {len(doomed)} stored list(s)", flush=True)
        with conn:
            conn.execute("DELETE FROM list_attempt WHERE list_key = ?", (url,))
    return kept


def is_systemic(err: MDBListError) -> bool:
    """A failure that says nothing about one list: the quota is spent, the key is refused,
    or MDBList never answered. The run stops instead of charging it to every list."""
    return isinstance(err, MDBListQuotaError | MDBListUnreachable) or err.status in (401, 403)


def record_failure(
    conn: sqlite3.Connection, key: str, held: sqlite3.Row | None, now_iso: str, reason: str
) -> int:
    attempts = 1 if held is None else held["attempts"] + 1
    with conn:
        conn.execute(
            "INSERT INTO list_attempt (list_key, attempts, attempted_at, reason) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (list_key) DO UPDATE SET "
            "attempts = excluded.attempts, attempted_at = excluded.attempted_at, "
            "reason = excluded.reason",
            (key, attempts, now_iso, reason[:REASON_MAX]),
        )
    return attempts


def gate(
    conn: sqlite3.Connection,
    *,
    source: str,
    key: str,
    label: str,
    decision: sqlite3.Row | None,
    held: sqlite3.Row | None,
    seen_at: str | None,
    stocked: bool,
    cutoff: datetime,
    stats: HarvestStats,
) -> tuple[bool, sqlite3.Row | None]:
    """Whether a list is fetched this run, and the failure record that still stands for it.

    A `retry` decided after the last failure clears the count. A list that has failed
    `MAX_FAILURES` times in a row is left alone (`stopped`). A list fetched inside the
    staleness window that holds entries is `cached`, unless a `refetch` was decided after
    that fetch. Each skip prints its log line and bumps `stats`.
    """
    if (
        held is not None
        and decision is not None
        and decision["decision"] == "retry"
        and is_before(held["attempted_at"], decision["decided_at"])
    ):
        with conn:
            conn.execute("DELETE FROM list_attempt WHERE list_key = ?", (key,))
        held = None
    if held is not None and held["attempts"] >= MAX_FAILURES:
        stats.lists_stopped += 1
        print(
            f"{source}: {label} -> stopped after {held['attempts']} failed fetches "
            f"(last {held['attempted_at']}: {held['reason']}); decide retry to try again",
            flush=True,
        )
        return False, held
    refetch = (
        decision is not None
        and decision["decision"] == "refetch"
        and (seen_at is None or is_before(seen_at, decision["decided_at"]))
    )
    if not refetch and seen_at is not None and not is_stale(seen_at, cutoff) and stocked:
        stats.lists_cached += 1
        print(f"{source}: {label} -> cached (fetched {seen_at})", flush=True)
        return False, held
    return True, held


@dataclass(frozen=True)
class ListEntry:
    """One title on a fetched list, whatever the source: every id it carries in this
    store's priority order, its media type (`movie` or `show`), and the source's own title
    and year."""

    external_ids: tuple[tuple[str, str], ...]
    media_type: str
    title: str | None = None
    year: int | None = None


def place_entries(
    index: dict[tuple[str, str, str], str], entries: Sequence[ListEntry], stats: HarvestStats
) -> tuple[list[tuple[int, str, str, str, str | None, int | None]], list[tuple[str, int]]]:
    """`(entry rows, memberships)` for a list's entries in list order.

    Array position is the rank, 1-indexed. A title outside the library is dropped from the
    memberships and counted; a title already placed for this list keeps its first (stronger)
    position rather than crashing the harvest on the table's primary key.
    """
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
    return stored, memberships


def write_snapshot(
    conn: sqlite3.Connection,
    *,
    collection_id: str,
    source: str,
    name: str | None,
    url: str | None,
    size: int | None,
    likes: int | None,
    provenance: str,
    list_key: str,
    fetched_iso: str,
    stored: Sequence[tuple[int, str, str, str, str | None, int | None]],
    memberships: Sequence[tuple[str, int]],
    seed_count: int | None = None,
) -> None:
    """Replace one list's collection row, entries and memberships in one transaction, and
    forget its failed fetches. A list is written whole or not at all."""
    with conn:
        conn.execute("DELETE FROM collection_entry WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM collection_membership WHERE collection_id = ?", (collection_id,))
        conn.execute(
            "INSERT INTO collection "
            "(collection_id, source, name, url, size, likes, observed_at, provenance, seed_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(collection_id) DO UPDATE SET "
            "source = excluded.source, name = excluded.name, url = excluded.url, "
            "size = excluded.size, likes = excluded.likes, "
            "observed_at = excluded.observed_at, provenance = excluded.provenance, "
            "seed_count = excluded.seed_count",
            (collection_id, source, name, url, size, likes, fetched_iso, provenance, seed_count),
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
                (collection_id, item_id, rank, fetched_iso),
            )
        conn.execute("DELETE FROM list_attempt WHERE list_key = ?", (list_key,))


@dataclass
class _Unit:
    """One list to consider: the key its decisions and failures are filed under, the URL
    to show, what the feed already said about it (`None` for a list known only by URL),
    and how it arrived."""

    key: str
    url: str | None
    meta: MDBListList | None
    provenance: str


def _units(
    source: MDBListSource,
    decisions: dict[str, sqlite3.Row],
    excluded: set[str],
    stats: HarvestStats,
) -> list[_Unit]:
    """The feed's lists, then every list a person added that the feed did not name. An
    excluded list is counted and left out; an added list that is also in the feed is
    fetched once, as added."""
    added = {url for url, row in decisions.items() if row["added"]}
    units: list[_Unit] = []
    seen: set[str] = set()
    for meta in source.top_lists():
        url = (
            f"https://mdblist.com/lists/{meta.user_name}/{meta.slug}"
            if meta.user_name and meta.slug
            else None
        )
        key = canonical_list_url(url) if url else None
        key = key or f"{MDBLIST_SOURCE}:{meta.list_id}"
        seen.add(key)
        if key in excluded:
            stats.lists_excluded += 1
            continue
        units.append(_Unit(key, url, meta, ADDED if key in added else FEED))
    for url in sorted(added - seen - excluded):
        units.append(_Unit(url, url, None, ADDED))
    return units


def refresh_mdblist(
    conn: sqlite3.Connection,
    source: MDBListSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> HarvestStats:
    """Refresh every MDBList top list, and every list a person added by URL, into
    `collection`, `collection_entry` and `collection_membership`.

    A list whose `collection` row is inside the staleness window and already
    holds entries is not re-fetched, so a re-run costs one request rather than
    one per list. Unlike the TMDB edge sweep this needs no sentinel row for the
    cursor: a fetched list always leaves a `collection` row behind even when
    none of its entries resolved, so `observed_at` on that row is the cursor.
    A list stored before entries were kept has a fresh row and no entries, so it
    is fetched once more.

    `list_decision` (folded from `list_decisions.json`) steers the run. An excluded list is
    wiped and never fetched; a removed list a person added is dropped; an added URL is
    fetched with provenance `added`; a `refetch` decided after a list was last fetched
    ignores staleness for that fetch. A list whose fetch fails — MDBList answered, but not
    with the list — records the attempt in `list_attempt` and the run goes on; after
    `MAX_FAILURES` failures in a row the list is left alone until a `retry` decided after
    the last attempt clears the count. A failure that says nothing about one list (the quota,
    a refused key, no answer) stops the run instead.

    Every list prints one log line: its URL, the HTTP status, the entries
    parsed and resolved to the library, and the outcome — `written`, `cached`,
    `stopped`, or `failed` with the reason.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=stale_days)
    stats = HarvestStats()

    decisions = decisions_for(conn, MDBLIST_SOURCE)
    excluded = apply_removals(conn, decisions, stats, MDBLIST_SOURCE)
    attempts = {row["list_key"]: row for row in conn.execute("SELECT * FROM list_attempt")}

    index = resolve_index(conn)
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
    by_url: dict[str, str] = {}
    for row in conn.execute(
        "SELECT collection_id, url FROM collection WHERE source = ? AND url IS NOT NULL",
        (MDBLIST_SOURCE,),
    ):
        canonical = canonical_list_url(row["url"])
        if canonical:
            by_url[canonical] = row["collection_id"]

    for unit in _units(source, decisions, excluded, stats):
        stats.lists_seen += 1
        label = unit.url or unit.key
        collection_id = (
            f"{MDBLIST_SOURCE}:{unit.meta.list_id}" if unit.meta else by_url.get(unit.key)
        )
        fetch, held = gate(
            conn,
            source=MDBLIST_SOURCE,
            key=unit.key,
            label=label,
            decision=decisions.get(unit.key),
            held=attempts.get(unit.key),
            seen_at=observed.get(collection_id) if collection_id else None,
            stocked=collection_id in stocked,
            cutoff=cutoff,
            stats=stats,
        )
        if not fetch:
            continue

        meta = unit.meta
        try:
            if meta is None:
                parts = list_url_parts(unit.key)
                if parts is None:
                    raise MDBListError(f"{unit.key} is not an MDBList list URL")
                meta = source.find_list(*parts)
            contents = source.list_entries(meta.list_id)
        except MDBListError as err:
            print(
                f"mdblist: {label} -> HTTP {err.status or 'none'}, failed: {err}",
                flush=True,
            )
            if is_systemic(err):
                raise
            record_failure(
                conn, unit.key, held, datetime.now(UTC).isoformat(timespec="seconds"), str(err)
            )
            stats.lists_failed += 1
            continue
        collection_id = f"{MDBLIST_SOURCE}:{meta.list_id}"
        url = (
            f"https://mdblist.com/lists/{meta.user_name}/{meta.slug}"
            if meta.user_name and meta.slug
            else unit.url
        )
        fetched_iso = datetime.now(UTC).isoformat(timespec="seconds")
        entries = contents.entries
        # Array position is the rank the list presents. MDBList's own `rank` field is a
        # different ordering entirely and is ignored — see `mdblist_client`'s docstring.
        stored, memberships = place_entries(
            index,
            [ListEntry(e.external_ids, e.media_type, e.title, e.year) for e in entries],
            stats,
        )
        write_snapshot(
            conn,
            collection_id=collection_id,
            source=MDBLIST_SOURCE,
            name=meta.name,
            url=url,
            size=meta.size,
            likes=meta.likes,
            provenance=unit.provenance,
            list_key=unit.key,
            fetched_iso=fetched_iso,
            stored=stored,
            memberships=memberships,
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
