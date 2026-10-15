"""`plexdb harvest-letterboxd-lists` — fetch the Letterboxd lists a person added into the
`collection`, `collection_entry` and `collection_membership` tables."""

from __future__ import annotations

import argparse
import sqlite3

from ..collections import LETTERBOXD_SOURCE, HarvestStats, wipe_source
from ..letterboxd_client import LiveLetterboxdClient
from ..letterboxd_lists import refresh_letterboxd_lists
from ..sources import GatedSource
from ..sweep import Step

NAME = "harvest-letterboxd-lists"
#: Beside `harvest-mdblist`: a list entry can only resolve to a title `walk` already
#: wrote, so everything filling `items` and `external_ids` runs ahead of it.
ORDER = 61
#: Letterboxd is an optional keyless scraper behind an adapter (ADR-0004), and its lists
#: are staleness-gated, so a missed harvest is picked up by the next run.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    memberships, entries, collections = wipe_source(conn, LETTERBOXD_SOURCE)
    return [
        f"wiped {collections} collection(s), {entries} entry(s) and "
        f"{memberships} membership(s) from Letterboxd lists"
    ]


def _report(stats: HarvestStats) -> list[str]:
    return [
        f"letterboxd lists: {stats.lists_seen} list(s) seen, {stats.lists_fetched} fetched, "
        f"{stats.lists_cached} already cached, "
        f"{stats.lists_failed} failed, {stats.lists_stopped} stopped after repeated failures, "
        f"{stats.lists_excluded} excluded, {stats.lists_removed} dropped, "
        f"{stats.entries_stored} entry(s) stored, "
        f"{stats.memberships_written} membership(s) written, "
        f"{stats.entries_not_in_library} entry(s) skipped (not in library), "
        f"{stats.entries_duplicate} entry(s) skipped (already placed on that list)"
    ]


SOURCE = GatedSource(
    name="letterboxd_lists",
    unit="list",
    # A lambda so a test can patch `LiveLetterboxdClient` in this module's globals.
    make_client=lambda: LiveLetterboxdClient(),
    refresh=refresh_letterboxd_lists,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch the Letterboxd lists a person added into collection, collection_entry and "
        "collection_membership; a fresh list is never re-fetched",
        rewipe_help="delete every Letterboxd list's collection and membership before the "
        "harvest, forcing a full re-fetch; other sources are untouched",
    )
