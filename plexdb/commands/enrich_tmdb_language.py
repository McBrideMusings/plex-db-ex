"""`plexdb enrich-tmdb-language` — record each walked movie's and show's original
language from TMDB into `enrichment` (namespace `tmdb`, key `original_language`)."""

from __future__ import annotations

import argparse
import sqlite3

from ..enrich_tmdb_language import LanguageStats, enrich_tmdb_language, wipe
from ..sources import GatedSource
from ..sweep import Step
from ..tmdb_client import LiveTMDbClient

NAME = "enrich-tmdb-language"
#: Beside `enrich-tmdb-keywords` (50), which asks TMDB about the same titles.
ORDER = 51
#: An optional source behind an adapter (ADR-0004), staleness-gated per title.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    return [f"wiped {wipe(conn)} original-language and cursor row(s) from the tmdb namespace"]


def _report(stats: LanguageStats) -> list[str]:
    lines = [
        f"tmdb language: {stats.titles_seen} title(s) seen, "
        f"{stats.titles_fetched} fetched, {stats.titles_cached} already cached, "
        f"{stats.titles_skipped_no_tmdb_id} skipped (no tmdb id), "
        f"{stats.titles_failed} failed, "
        f"{stats.languages_written} language(s) written, "
        f"{stats.titles_without_language} without one"
    ]
    if stats.titles_failed:
        lines.append(
            f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them"
        )
    return lines


SOURCE = GatedSource(
    name="tmdb_language",
    unit="title",
    credential="TMDB_API_KEY",
    credential_purpose="fetch TMDB original languages",
    # Late-bound so a test can substitute a fixture-backed client; see
    # enrich_tmdb_keywords.py.
    make_client=lambda api_key: LiveTMDbClient(api_key),
    refresh=enrich_tmdb_language,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="record each walked movie's and show's original language from TMDB into "
        "enrichment (namespace tmdb); a fresh title is never re-asked",
        rewipe_help="delete every original_language row and its cursor before the run, "
        "forcing a full re-fetch; keywords and edges are untouched",
    )
