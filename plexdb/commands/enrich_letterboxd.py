"""`plexdb enrich-letterboxd` — the themes on each movie's Letterboxd page into
`keywords` (source='letterboxd'), a capped number of titles per run, exiting
non-zero when too many film pages stop parsing."""

from __future__ import annotations

import argparse
import sqlite3

from ..enrich_letterboxd import (
    DEFAULT_MAX_TITLES,
    MAX_PARSE_FAILURE_SHARE,
    LetterboxdStats,
    enrich_letterboxd,
    wipe,
)
from ..letterboxd_client import LiveLetterboxdClient
from ..sources import GatedSource
from ..sweep import Step

NAME = "enrich-letterboxd"
#: Beside `enrich-anilist` and `enrich-wikidata`, after both TMDB steps and
#: before `refresh-map` draws from the keywords; equal numbers sort by module
#: name, so this runs between them.
ORDER = 56
#: An optional source behind an adapter (ADR-0004), and a scraper with no API
#: contract: a skipped or failed pass costs only freshness.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    return [f"wiped {wipe(conn)} letterboxd row(s) from keywords and cursors"]


def _report(stats: LetterboxdStats) -> list[str]:
    lines = [
        f"letterboxd: {stats.titles_seen} movie(s) seen, {stats.titles_fetched} fetched "
        f"({stats.titles_matched} with themes, {stats.titles_not_listed} not listed), "
        f"{stats.titles_cached} already cached, {stats.titles_skipped_no_tmdb_id} without a "
        f"TMDB id, {stats.titles_capped} left for the next run, {stats.titles_failed} failed",
        f"letterboxd: {stats.keywords_written} keyword(s) written; "
        f"{stats.parse_failures} parse failure(s) of {stats.pages_fetched} film page(s)",
    ]
    if stats.titles_failed:
        lines.append(
            f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them"
        )
    if stats.parse_failed:
        lines.append(
            f"letterboxd: {stats.parse_failures} of {stats.pages_fetched} film page(s) had no "
            f"film marker, over the {MAX_PARSE_FAILURE_SHARE:.0%} limit — the page layout "
            "may have changed"
        )
    return lines


def _exit_code(stats: LetterboxdStats) -> int:
    return 1 if stats.parse_failed else 0


SOURCE = GatedSource(
    name="letterboxd",
    unit="title",
    # A lambda so a test can patch `LiveLetterboxdClient` in this module's globals.
    make_client=lambda: LiveLetterboxdClient(),
    refresh=enrich_letterboxd,
    wipe=_wipe,
    report=_report,
    default_limit=DEFAULT_MAX_TITLES,
    exit_code=_exit_code,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="scrape the themes on each walked movie's Letterboxd page, reached through its "
        "TMDB id, into keywords (source='letterboxd'); a fresh title is never re-asked",
        rewipe_help="delete every source='letterboxd' keyword and cursor row before the run, "
        "forcing a full re-fetch; other sources are untouched",
    )
