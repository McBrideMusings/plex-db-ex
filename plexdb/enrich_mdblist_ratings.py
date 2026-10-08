"""Every rating MDBList relays for a movie or show, into `ratings`, cached per title.

MDBList's batch title endpoint answers up to 200 IMDb ids per request with each
title's ratings from IMDb, TMDB, Trakt, Letterboxd, Rotten Tomatoes (critics as
`tomatoes`, audience as `popcorn`), Metacritic (`metacritic`, `metacriticuser`),
RogerEbert and MyAnimeList (`mdblist_client.py`). Each becomes one row in the
`ratings` namespace under `source = 'mdblist'`: key = the site's name as MDBList
gives it, value = the site's own number on its own scale, exactly as the JSON
carried it. A `<site>_votes` row beside it holds the vote count where one is
given. A site whose value is null writes no row at all, its votes included,
and nothing is converted or combined (ADR-0012). `rank` stays NULL.

Titles are looked up by IMDb id only: that reaches all but a handful of the
library's movies and shows, and a title without one is counted as skipped.

Each title's rows under this source are a snapshot: a re-fetch deletes and
rewrites them. One `enrichment_cursor` row per title (namespace `ratings`,
source `mdblist`, key `fetched`) is written for every title in a batch that
answered — even one MDBList did not return, so it is not asked again until
stale (ADR-0013).

A failed request writes nothing for any title in its batch and records only an
`attempted` cursor for each, which is scheduling, not a fact: it never makes a
title fresh, and a later successful fetch deletes it. Three failed requests in
a row abort the run. A 429 is different: the day's quota is spent and every
later request would get the same answer, so the run stops at once, marks
nothing, and leaves every remaining title in its place for the next run.

A run sends at most `limit` requests (`DEFAULT_MAX_REQUESTS` unless
`MDBLIST_RATINGS_MAX_REQUESTS` or `--limit` says otherwise), because the daily
quota counts requests and `harvest-mdblist` draws on the same one. Due titles
never attempted come first, in `item_id` order, then attempted ones, oldest
attempt first; batches are cut from that order per media type and sent in the
order of their first title. A fetched title's cursor takes it out of the next
run's list and an attempted one moves to the back of it, so a capped run
continues where the last stopped and a batch that keeps failing never holds
the cap.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .cursors import delete_cursors, load_cursors, mark_attempted, write_fetched
from .errors import MDBListError, MDBListQuotaError
from .mdblist_client import BATCH_SIZE, MDBListRatingsSource, MDBListTitleRatings
from .staleness import DEFAULT_STALE_DAYS, is_stale

#: The namespace this writer fills, in `enrichment` and `enrichment_cursor`.
NAMESPACE = "ratings"
#: This writer's source name.
SOURCE = "mdblist"
_VOTES_SUFFIX = "_votes"

#: Requests one run sends at most. At 200 titles a batch this covers 20,000
#: titles — the whole library in one run — and leaves 900 of the free tier's
#: 1,000 daily requests for `harvest-mdblist`.
DEFAULT_MAX_REQUESTS = 100

#: Failed requests in a row that abort the run — MDBList is down or refusing
#: the key, and asking again only spends quota.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class RatingsStats:
    """What one run touched — the summary `plexdb enrich-mdblist-ratings` prints."""

    titles_seen: int = 0
    titles_cached: int = 0
    #: Movies and shows with no `imdb` row in `external_ids` — skipped, not an error.
    titles_skipped_no_imdb_id: int = 0
    #: Due titles in batches past the per-run request cap, left for the next run.
    titles_capped: int = 0
    requests_sent: int = 0
    requests_failed: int = 0
    #: Titles that got a `fetched` cursor this run.
    titles_fetched: int = 0
    #: Fetched titles MDBList's answer left out.
    titles_not_found: int = 0
    #: Fetched titles with at least one rating row.
    titles_rated: int = 0
    #: Titles in a failed batch. Nothing is written for them.
    titles_failed: int = 0
    ratings_written: int = 0
    votes_written: int = 0
    #: MDBList's message when it refused a request for a spent quota, which ends
    #: the run with nothing marked; `None` when it never did.
    quota_spent: str | None = None


@dataclass(frozen=True)
class _Due:
    item_id: str
    media_type: str
    imdb_id: str


def wipe(conn: sqlite3.Connection) -> int:
    """Delete this source's ratings and cursors. Every other source and
    namespace stays. Returns the number of rows removed."""
    with conn:
        removed = conn.execute(
            "DELETE FROM enrichment WHERE namespace = ? AND source = ?", (NAMESPACE, SOURCE)
        ).rowcount
        removed += delete_cursors(conn, NAMESPACE, SOURCE)
        return removed


def enrich_mdblist_ratings(
    conn: sqlite3.Connection,
    source: MDBListRatingsSource,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    limit: int = DEFAULT_MAX_REQUESTS,
) -> RatingsStats:
    """Fetch the ratings of every walked movie and show with an `imdb`
    external id whose cursor is missing or stale, in at most `limit` requests."""
    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    stats = RatingsStats()

    cursors = load_cursors(conn, NAMESPACE, SOURCE)
    # A join, not a correlated subquery, for the reason enrich_letterboxd.py gives.
    candidates = conn.execute(
        """
        SELECT i.item_id AS item_id, i.type AS type, MIN(e.value) AS imdb_id
        FROM items i
        LEFT JOIN external_ids e ON e.item_id = i.item_id AND e.ns = 'imdb'
        WHERE i.type IN ('movie', 'show')
        GROUP BY i.item_id
        ORDER BY i.item_id
        """
    ).fetchall()

    due: list[_Due] = []
    for row in candidates:
        stats.titles_seen += 1
        if not row["imdb_id"]:
            stats.titles_skipped_no_imdb_id += 1
            continue
        fetched_at = cursors.fetched.get(row["item_id"])
        if fetched_at is not None and not is_stale(fetched_at, cutoff):
            stats.titles_cached += 1
            continue
        due.append(_Due(row["item_id"], row["type"], row["imdb_id"]))
    # Stable, so titles never attempted keep their `item_id` order.
    due.sort(key=lambda t: cursors.order_key(t.item_id))

    batches = _batches(due)
    stats.titles_capped = sum(len(batch) for batch in batches[limit:])

    consecutive_failures = 0
    for index, batch in enumerate(batches[:limit]):
        stats.requests_sent += 1
        try:
            found = source.ratings(batch[0].media_type, [t.imdb_id for t in batch])
        except MDBListQuotaError as err:
            stats.quota_spent = str(err)
            stats.titles_capped += sum(len(b) for b in batches[index:limit])
            break
        except MDBListError as err:
            stats.requests_failed += 1
            stats.titles_failed += len(batch)
            consecutive_failures += 1
            mark_attempted(conn, NAMESPACE, SOURCE, [t.item_id for t in batch])
            print(f"mdblist: {batch[0].media_type} batch of {len(batch)} failed: {err}", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise MDBListError(
                    f"aborting after {MAX_CONSECUTIVE_FAILURES} consecutive failed requests: "
                    f"{stats.titles_fetched} title(s) fetched, {stats.titles_failed} failed; "
                    f"tripping error: {err}"
                ) from err
            continue
        consecutive_failures = 0
        _write_batch(conn, batch, found, datetime.now(UTC), stats)

    return stats


def _batches(due: list[_Due]) -> list[list[_Due]]:
    """Cut `due` into per-media-type batches of at most `BATCH_SIZE`, keeping
    `due`'s order inside each, and order the batches by their first title."""
    position = {t.item_id: i for i, t in enumerate(due)}
    by_type: dict[str, list[_Due]] = {}
    for title in due:
        by_type.setdefault(title.media_type, []).append(title)
    batches = [
        titles[start : start + BATCH_SIZE]
        for titles in by_type.values()
        for start in range(0, len(titles), BATCH_SIZE)
    ]
    batches.sort(key=lambda batch: position[batch[0].item_id])
    return batches


def _write_batch(
    conn: sqlite3.Connection,
    batch: list[_Due],
    found: list[MDBListTitleRatings],
    now: datetime,
    stats: RatingsStats,
) -> None:
    """Replace every batch title's rows and cursor in one transaction, so an
    interrupted run never leaves a cursor vouching for rows already deleted."""
    now_iso = now.isoformat(timespec="seconds")
    by_imdb = {title.imdb_id: title for title in found}
    with conn:
        for title in batch:
            conn.execute(
                "DELETE FROM enrichment WHERE item_id = ? AND namespace = ? AND source = ?",
                (title.item_id, NAMESPACE, SOURCE),
            )
            write_fetched(conn, NAMESPACE, SOURCE, title.item_id, now_iso)
            stats.titles_fetched += 1
            answer = by_imdb.get(title.imdb_id)
            if answer is None:
                stats.titles_not_found += 1
                continue
            rows: dict[str, str] = {}
            for rating in answer.ratings:
                if rating.value is None:
                    continue
                rows[rating.site] = json.dumps(rating.value)
                if rating.votes is not None:
                    rows[rating.site + _VOTES_SUFFIX] = str(rating.votes)
            votes = sum(key.endswith(_VOTES_SUFFIX) for key in rows)
            stats.votes_written += votes
            stats.ratings_written += len(rows) - votes
            conn.executemany(
                "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (title.item_id, NAMESPACE, SOURCE, key, value, now_iso)
                    for key, value in rows.items()
                ],
            )
            if rows:
                stats.titles_rated += 1
