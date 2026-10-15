"""Finding crowd lists outward from the titles the household has watched.

MDBList's top-lists feed follows popularity. This harvest follows taste: each watched movie
or show is a **seed**, searched by title through MDBList's list search; every list the
search returns is a **candidate**; a candidate is fetched, and kept with provenance
`search` only when it holds at least `min_titles` titles of this library. Its
`collection.seed_count` is how many of those titles are seeds (watched).

**What the search can say.** MDBList matches a list's *name* against the query and nothing
else, and no endpoint answers "which lists contain this title" (`mdblist_client`'s
docstring; Letterboxd's per-film list pages sit behind a Cloudflare challenge). A name match
is therefore only a lead — "House" also surfaces "House of the Dragon" — and the fetched
list's own entries decide whether it is kept. A candidate that holds fewer than `min_titles`
library titles is written nowhere and marked rejected, so it is not fetched again until
`stale_days` have passed.

**Seeds are gated like every source.** A seed asked at `t` is not asked again until
`stale_days` have passed (`enrichment_cursor` under namespace `list_search`, source
`mdblist`, key `fetched`). Each run asks at most `limit` seeds, most-watched first, and
fetches at most `max_lists` candidates, those surfaced by the most seeds first. A search
that fails moves the seed to the back of the next run's order.

**What a seed found is bookkeeping, not a fact** (ADR-0013). A hit is an `enrichment_cursor`
row under the same namespace and source, `key = hit:<list url>`, rewritten whole each time
the seed is asked; a rejection is `key = rejected:<list url>` on one seed that surfaced it.
Neither needs a table of its own. A kept list is a `collection` row like any other, fetched
through the shared gate: an excluded list never re-enters, a stale one is refetched, a
failing one stops after three tries until a `retry` decision.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .collections import (
    ADDED,
    FEED,
    MDBLIST_SOURCE,
    SEARCH,
    HarvestStats,
    ListEntry,
    apply_removals,
    decisions_for,
    drop_lists,
    gate,
    is_systemic,
    place_entries,
    record_failure,
    resolve_index,
    write_snapshot,
)
from .cursors import (
    delete_cursors,
    load_cursors,
    mark_attempted,
    write_fetched,
)
from .errors import MDBListError
from .list_decisions import canonical_list_url, is_before, list_url_parts
from .mdblist_client import MDBListSearchSource
from .staleness import DEFAULT_STALE_DAYS, is_stale

NAMESPACE = "list_search"
CURSOR_SOURCE = "mdblist"
HIT_PREFIX = "hit:"
REJECTED_PREFIX = "rejected:"

#: Seeds one run asks, candidates one run fetches, and library titles a candidate must hold.
DEFAULT_MAX_SEEDS = 25
DEFAULT_MAX_LISTS = 10
DEFAULT_MIN_TITLES = 5

#: A watched movie, or a show with a watched episode, with how many plays it has. A show's
#: plays are its episodes'.
_SEEDS = """
SELECT w.item_id, i.title, COUNT(*) AS plays
FROM (
    SELECT p.item_id AS item_id FROM plays p JOIN items m ON m.item_id = p.item_id
     WHERE m.type = 'movie'
    UNION ALL
    SELECT e.show_item_id FROM plays p JOIN items e ON e.item_id = p.item_id
     WHERE e.type = 'episode' AND e.show_item_id IS NOT NULL
) w JOIN items i ON i.item_id = w.item_id
GROUP BY w.item_id
ORDER BY plays DESC, w.item_id
"""


@dataclass
class SearchStats(HarvestStats):
    """What one outward search touched, on top of the list harvest's own counts."""

    seeds_asked: int = 0
    seeds_cached: int = 0
    seeds_failed: int = 0
    #: Distinct lists this run's searches returned.
    lists_found: int = 0
    #: Candidates fetched that held `min_titles` library titles, and those that held fewer.
    lists_qualified: int = 0
    lists_rejected: int = 0


def _url(user_name: str, slug: str) -> str | None:
    if not user_name or not slug:
        return None
    return canonical_list_url(f"https://mdblist.com/lists/{user_name}/{slug}")


def _ask_seeds(
    conn: sqlite3.Connection,
    source: MDBListSearchSource,
    stats: SearchStats,
    seeds: list[sqlite3.Row],
    *,
    cutoff: datetime,
    limit: int,
) -> set[str]:
    """Search each due seed's title and record the lists returned. Returns the URLs found."""
    cursors = load_cursors(conn, NAMESPACE, CURSOR_SOURCE)
    due = [row for row in seeds if not _fresh(cursors.fetched.get(row["item_id"]), cutoff)]
    stats.seeds_cached = len(seeds) - len(due)
    due.sort(key=lambda row: cursors.order_key(row["item_id"]))
    found: set[str] = set()
    for row in due[:limit]:
        item_id, title = row["item_id"], row["title"]
        try:
            results = source.search_lists(title)
        except MDBListError as err:
            print(
                f'mdblist: search "{title}" (seed {item_id}) -> HTTP {err.status or "none"}, '
                f"failed: {err}",
                flush=True,
            )
            if is_systemic(err):
                raise
            mark_attempted(conn, NAMESPACE, CURSOR_SOURCE, [item_id])
            stats.seeds_failed += 1
            continue
        urls = sorted({u for r in results if (u := _url(r.user_name, r.slug))})
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with conn:
            conn.execute(
                "DELETE FROM enrichment_cursor WHERE item_id = ? AND namespace = ? "
                "AND source = ? AND key LIKE ?",
                (item_id, NAMESPACE, CURSOR_SOURCE, f"{HIT_PREFIX}%"),
            )
            for url in urls:
                conn.execute(
                    "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_id, NAMESPACE, CURSOR_SOURCE, f"{HIT_PREFIX}{url}", now),
                )
            write_fetched(conn, NAMESPACE, CURSOR_SOURCE, item_id, now)
        stats.seeds_asked += 1
        found.update(urls)
        print(
            f'mdblist: search "{title}" (seed {item_id}) -> {len(urls)} list(s)',
            flush=True,
        )
    return found


def _fresh(fetched_at: str | None, cutoff: datetime) -> bool:
    return fetched_at is not None and not is_stale(fetched_at, cutoff)


def refresh_list_search(
    conn: sqlite3.Connection,
    source: MDBListSearchSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    limit: int = DEFAULT_MAX_SEEDS,
    min_titles: int = DEFAULT_MIN_TITLES,
    max_lists: int = DEFAULT_MAX_LISTS,
) -> SearchStats:
    """Ask up to `limit` due seeds which lists carry their title, then fetch up to
    `max_lists` of the candidates and keep those holding at least `min_titles` library
    titles.

    Every search prints a line (the title, the seed, how many lists came back), every
    candidate prints the line `harvest-mdblist` prints for a fetch plus whether it
    qualified.
    """
    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    stats = SearchStats()

    decisions = decisions_for(conn, MDBLIST_SOURCE)
    excluded = apply_removals(conn, decisions, stats, MDBLIST_SOURCE)
    stats.lists_excluded = len(excluded)

    seeds = conn.execute(_SEEDS).fetchall()
    watched = {row["item_id"] for row in seeds}
    found = _ask_seeds(conn, source, stats, seeds, cutoff=cutoff, limit=limit)
    stats.lists_found = len(found)

    counts = {
        row["key"][len(HIT_PREFIX) :]: row["seeds"]
        for row in conn.execute(
            "SELECT key, COUNT(*) AS seeds FROM enrichment_cursor "
            "WHERE namespace = ? AND source = ? AND key LIKE ? GROUP BY key",
            (NAMESPACE, CURSOR_SOURCE, f"{HIT_PREFIX}%"),
        )
    }
    rejected = {
        row["key"][len(REJECTED_PREFIX) :]: row["fetched_at"]
        for row in conn.execute(
            "SELECT key, MAX(fetched_at) AS fetched_at FROM enrichment_cursor "
            "WHERE namespace = ? AND source = ? AND key LIKE ? GROUP BY key",
            (NAMESPACE, CURSOR_SOURCE, f"{REJECTED_PREFIX}%"),
        )
    }
    candidates = sorted(
        ((url, n) for url, n in counts.items() if url not in excluded),
        key=lambda pair: (-pair[1], pair[0]),
    )

    attempts = {row["list_key"]: row for row in conn.execute("SELECT * FROM list_attempt")}
    index = resolve_index(conn)
    stocked = {
        row["collection_id"]
        for row in conn.execute("SELECT DISTINCT collection_id FROM collection_entry")
    }
    stored = {
        canonical: row
        for row in conn.execute(
            "SELECT collection_id, url, observed_at, provenance FROM collection "
            "WHERE source = ? AND url IS NOT NULL",
            (MDBLIST_SOURCE,),
        )
        if (canonical := canonical_list_url(row["url"]))
    }

    over_cap = 0
    asked = 0
    for url, surfaced in candidates:
        stats.lists_seen += 1
        decision = decisions.get(url)
        redecided = (
            decision is not None
            and decision["decision"] in ("refetch", "retry")
            and rejected.get(url) is not None
            and is_before(rejected[url], decision["decided_at"])
        )
        if not redecided and _fresh(rejected.get(url), cutoff):
            stats.lists_cached += 1
            print(
                f"mdblist: {url} -> rejected earlier (fewer than {min_titles} library titles)",
                flush=True,
            )
            continue
        held_row = stored.get(url)
        fetch, held = gate(
            conn,
            source=MDBLIST_SOURCE,
            key=url,
            label=url,
            decision=decisions.get(url),
            held=attempts.get(url),
            seen_at=held_row["observed_at"] if held_row else None,
            stocked=held_row is not None and held_row["collection_id"] in stocked,
            cutoff=cutoff,
            stats=stats,
        )
        if not fetch:
            continue
        if asked >= max_lists:
            over_cap += 1
            continue
        asked += 1
        parts = list_url_parts(url)
        assert parts is not None
        try:
            meta = source.find_list(*parts)
            contents = source.list_entries(meta.list_id)
        except MDBListError as err:
            print(f"mdblist: {url} -> HTTP {err.status or 'none'}, failed: {err}", flush=True)
            if is_systemic(err):
                raise
            record_failure(
                conn, url, held, datetime.now(UTC).isoformat(timespec="seconds"), str(err)
            )
            stats.lists_failed += 1
            continue
        entries = [
            ListEntry(e.external_ids, e.media_type, e.title, e.year) for e in contents.entries
        ]
        entry_rows, memberships = place_entries(index, entries, stats)
        stats.entries_parsed += len(entries)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        collection_id = f"{MDBLIST_SOURCE}:{meta.list_id}"
        if len(memberships) < min_titles:
            _reject(conn, url, now)
            if held_row and held_row["provenance"] == SEARCH:
                drop_lists(conn, [held_row["collection_id"]])
            stats.lists_rejected += 1
            print(
                f"mdblist: {url} -> HTTP {contents.status}, {len(entries)} entries parsed, "
                f"{len(memberships)} resolved to the library, rejected (needs {min_titles})",
                flush=True,
            )
            continue
        seed_count = sum(1 for item_id, _rank in memberships if item_id in watched)
        keeps = held_row["provenance"] if held_row else None
        if url in decisions and decisions[url]["added"]:
            provenance = ADDED
        elif keeps in (FEED, ADDED):
            provenance = keeps
        else:
            provenance = SEARCH
        write_snapshot(
            conn,
            collection_id=collection_id,
            source=MDBLIST_SOURCE,
            name=meta.name,
            url=url,
            size=meta.size,
            likes=meta.likes,
            provenance=provenance,
            list_key=url,
            fetched_iso=now,
            stored=entry_rows,
            memberships=memberships,
            seed_count=seed_count if provenance == SEARCH else None,
        )
        stats.lists_fetched += 1
        stats.lists_qualified += 1
        stats.entries_stored += len(entry_rows)
        stats.memberships_written += len(memberships)
        print(
            f"mdblist: {url} -> HTTP {contents.status}, {len(entries)} entries parsed, "
            f"{len(memberships)} resolved to the library, qualified and written "
            f"(search, {seed_count} seed(s), surfaced by {surfaced})",
            flush=True,
        )
    if over_cap:
        print(
            f"mdblist: {over_cap} candidate(s) left for the next run (cap {max_lists})",
            flush=True,
        )
    return stats


def _reject(conn: sqlite3.Connection, url: str, now_iso: str) -> None:
    """Remember that a fetched candidate held too few library titles, on one seed that
    surfaced it, so it is not fetched again inside the staleness window."""
    row = conn.execute(
        "SELECT item_id FROM enrichment_cursor WHERE namespace = ? AND source = ? AND key = ? "
        "ORDER BY item_id LIMIT 1",
        (NAMESPACE, CURSOR_SOURCE, f"{HIT_PREFIX}{url}"),
    ).fetchone()
    if row is None:
        return
    with conn:
        conn.execute(
            "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (item_id, namespace, source, key) "
            "DO UPDATE SET fetched_at = excluded.fetched_at",
            (row["item_id"], NAMESPACE, CURSOR_SOURCE, f"{REJECTED_PREFIX}{url}", now_iso),
        )


def wipe_searched(conn: sqlite3.Connection) -> tuple[int, int]:
    """Delete every list found by search and every seed's cursor and hits, so the next run
    asks every seed again. Lists from the feed or added by a person are untouched. Returns
    `(lists_removed, cursors_removed)`."""
    ids = [
        row["collection_id"]
        for row in conn.execute(
            "SELECT collection_id FROM collection WHERE source = ? AND provenance = ?",
            (MDBLIST_SOURCE, SEARCH),
        )
    ]
    drop_lists(conn, ids)
    with conn:
        cursors = delete_cursors(conn, NAMESPACE, CURSOR_SOURCE)
    return len(ids), cursors
