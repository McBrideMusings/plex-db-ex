"""`plexdb enrich-mdblist-ratings` — every rating MDBList relays for each walked
movie and show into `ratings` (source='mdblist'), a capped number of requests
per run."""

from __future__ import annotations

import argparse
import sqlite3

from ..enrich_mdblist_ratings import (
    DEFAULT_MAX_REQUESTS,
    RatingsStats,
    enrich_mdblist_ratings,
    wipe,
)
from ..mdblist_client import LiveMDBListClient
from ..sources import GatedSource
from ..sweep import Step

NAME = "enrich-mdblist-ratings"
#: Beside `harvest-mdblist`, which draws on the same daily quota. Ratings are
#: not keywords, so nothing downstream of `refresh-map` waits on them; equal
#: numbers sort by module name, so this runs just before the harvest.
ORDER = 60
#: An optional source behind an adapter (ADR-0004), staleness-gated per title,
#: so a skipped or failed pass costs only freshness.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    return [f"wiped {wipe(conn)} mdblist row(s) from ratings and cursors"]


def _report(stats: RatingsStats) -> list[str]:
    lines = [
        f"mdblist ratings: {stats.titles_seen} title(s) seen, {stats.titles_fetched} fetched "
        f"({stats.titles_rated} rated, {stats.titles_not_found} not found), "
        f"{stats.titles_cached} already cached, {stats.titles_skipped_no_imdb_id} without an "
        f"IMDb id, {stats.titles_capped} left for the next run, {stats.titles_failed} failed",
        f"mdblist ratings: {stats.requests_sent} request(s) sent, {stats.requests_failed} "
        f"failed; {stats.ratings_written} rating(s) and {stats.votes_written} vote count(s) "
        "written",
    ]
    if stats.titles_failed:
        lines.append(
            f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them"
        )
    if stats.quota_spent:
        lines.append(f"mdblist ratings: stopped early — {stats.quota_spent}")
    return lines


def _exit_code(stats: RatingsStats) -> int:
    return 1 if stats.quota_spent else 0


SOURCE = GatedSource(
    name="mdblist_ratings",
    unit="title",
    # The daily quota counts requests, 200 titles each, so the cap does too:
    # MDBLIST_RATINGS_MAX_REQUESTS.
    limit_unit="request",
    credential="MDBLIST_API_KEY",
    credential_purpose="fetch MDBList ratings",
    # Late-bound on purpose — see the note in enrich_tmdb_keywords.py.
    make_client=lambda api_key: LiveMDBListClient(api_key),
    refresh=enrich_mdblist_ratings,
    wipe=_wipe,
    report=_report,
    default_limit=DEFAULT_MAX_REQUESTS,
    exit_code=_exit_code,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch every rating MDBList relays for each walked movie and show, by IMDb id, "
        "into ratings (source='mdblist'); a fresh title is never re-asked",
        rewipe_help="delete every source='mdblist' ratings and cursor row before the run, "
        "forcing a full re-fetch; harvest-mdblist's collections and other sources are "
        "untouched",
    )
