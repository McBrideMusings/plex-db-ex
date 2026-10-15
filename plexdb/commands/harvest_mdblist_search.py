"""`plexdb harvest-mdblist-search` — find MDBList crowd lists by searching outward from the
titles the household has watched, and fetch the ones several of those titles surface."""

from __future__ import annotations

import argparse
import sqlite3

from ..list_search import (
    DEFAULT_MAX_LISTS,
    DEFAULT_MAX_SEEDS,
    DEFAULT_MIN_TITLES,
    SearchStats,
    refresh_list_search,
    wipe_searched,
)
from ..mdblist_client import LiveMDBListClient
from ..sources import GatedSource, env_int
from ..sweep import Step

NAME = "harvest-mdblist-search"
#: After the letterboxd list harvest: a list can only resolve to a title `walk` already wrote.
ORDER = 62
#: An optional source behind an adapter (ADR-0004); seeds are staleness-gated, so a missed
#: run is picked up by the next.
SWEEP = Step.BEST_EFFORT


def _refresh(
    conn: sqlite3.Connection, client: LiveMDBListClient, *, stale_days: int, limit: int
) -> SearchStats:
    return refresh_list_search(
        conn,
        client,
        stale_days=stale_days,
        limit=limit,
        min_titles=env_int(
            "MDBLIST_SEARCH_MIN_TITLES", DEFAULT_MIN_TITLES, "a whole number of library titles"
        ),
        max_lists=env_int("MDBLIST_SEARCH_MAX_LISTS", DEFAULT_MAX_LISTS, "a whole number of lists"),
    )


def _wipe(conn: sqlite3.Connection) -> list[str]:
    lists, cursors = wipe_searched(conn)
    return [f"wiped {lists} searched list(s) and {cursors} seed cursor(s)"]


def _report(stats: SearchStats) -> list[str]:
    return [
        f"mdblist search: {stats.seeds_asked} seed(s) asked, {stats.seeds_cached} still fresh, "
        f"{stats.seeds_failed} failed; {stats.lists_found} list(s) found, "
        f"{stats.lists_qualified} fetched and kept, {stats.lists_rejected} fetched and rejected, "
        f"{stats.lists_cached} already cached, {stats.lists_failed} failed, "
        f"{stats.lists_stopped} stopped after repeated failures, "
        f"{stats.lists_excluded} excluded, "
        f"{stats.memberships_written} membership(s) written"
    ]


SOURCE = GatedSource(
    name="mdblist_search",
    unit="seed",
    credential="MDBLIST_API_KEY",
    credential_purpose="search MDBList for lists",
    make_client=lambda api_key: LiveMDBListClient(api_key),
    refresh=_refresh,
    wipe=_wipe,
    report=_report,
    default_limit=DEFAULT_MAX_SEEDS,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="search MDBList for lists named for watched titles and keep those holding enough of "
        "this library; a seed is not asked again until it is stale",
        rewipe_help="delete every list found by search and forget every seed, so the next run "
        "asks them all again; feed and added lists are untouched",
    )
