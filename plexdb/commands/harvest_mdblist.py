"""`plexdb harvest-mdblist` — fetch MDBList's top crowd lists into the
`collection` and `collection_membership` tables (issue #34)."""

from __future__ import annotations

import argparse
import sqlite3

from ..collections import MDBLIST_SOURCE, HarvestStats, refresh_mdblist, wipe_source
from ..mdblist_client import LiveMDBListClient
from ..sources import GatedSource
from ..sweep import Step

NAME = "harvest-mdblist"
#: Last of the enrichment sources. A list entry can only resolve to a title
#: `walk` already wrote, so everything filling `items` and `external_ids` runs
#: ahead of it.
ORDER = 60
#: MDBList is an optional source behind an adapter (ADR-0004), and its lists
#: are staleness-gated, so a missed harvest is picked up by the next run.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    memberships, collections = wipe_source(conn, MDBLIST_SOURCE)
    return [f"wiped {collections} collection(s) and {memberships} membership(s) from MDBList"]


def _report(stats: HarvestStats) -> list[str]:
    return [
        f"mdblist: {stats.lists_seen} list(s) seen, {stats.lists_fetched} fetched, "
        f"{stats.lists_cached} already cached, "
        f"{stats.memberships_written} membership(s) written, "
        f"{stats.entries_not_in_library} entry(s) skipped (not in library), "
        f"{stats.entries_duplicate} entry(s) skipped (already placed on that list)"
    ]


SOURCE = GatedSource(
    name="mdblist",
    unit="list",
    credential="MDBLIST_API_KEY",
    credential_purpose="harvest MDBList collections",
    # Late-bound on purpose — see the note in enrich_tmdb_keywords.py.
    make_client=lambda api_key: LiveMDBListClient(api_key),
    refresh=refresh_mdblist,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch MDBList's top crowd lists into collection/collection_membership; "
        "a fresh list is never re-fetched",
        rewipe_help="delete every MDBList collection and membership before the harvest, "
        "forcing a full re-fetch; other sources are untouched",
    )
