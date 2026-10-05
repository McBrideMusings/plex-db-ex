"""`plexdb enrich-anilist` — AniList's tags on anime titles into `keywords`
(source='anilist') with AniList's 0–100 rank, spoilers under `spoiler_keyword`,
and the roles their categories state."""

from __future__ import annotations

import argparse
import sqlite3

from ..anilist_client import LiveAniListClient
from ..enrich_anilist import AniListStats, enrich_anilist, wipe
from ..sources import GatedSource
from ..sweep import Step

NAME = "enrich-anilist"
#: Beside `enrich-wikidata`, after both TMDB steps and before `refresh-map` draws
#: from the keywords; equal numbers sort by module name, so this runs first.
ORDER = 56
#: An optional source behind an adapter (ADR-0004); a skipped pass costs only
#: freshness, and the next run picks up exactly the titles this one missed.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    return [f"wiped {wipe(conn)} anilist row(s) from keywords, roles and cursors"]


def _report(stats: AniListStats) -> list[str]:
    lines = [
        f"anilist: {stats.titles_seen} title(s) seen, {stats.titles_anime} anime, "
        f"{stats.titles_fetched} fetched ({stats.titles_matched} matched) "
        f"in {stats.batches_sent} batch(es) of {stats.anilist_ids_asked} anilist id(s), "
        f"{stats.titles_cached} already cached, {stats.titles_failed} failed",
        f"anilist: {stats.keywords_written} keyword(s), "
        f"{stats.spoiler_keywords_written} spoiler keyword(s), "
        f"{stats.roles_stated} role row(s) written",
    ]
    if stats.titles_failed:
        lines.append(
            f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them"
        )
    return lines


SOURCE = GatedSource(
    name="anilist",
    unit="title",
    # A lambda so a test can patch `LiveAniListClient` in this module's globals.
    make_client=lambda: LiveAniListClient(),
    refresh=enrich_anilist,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch AniList tags with their 0-100 rank (source='anilist') and the roles "
        "their categories state, for walked anime found through Fribb's mapping; "
        "a fresh title is never re-asked",
        rewipe_help="delete every source='anilist' keyword, spoiler keyword, role and "
        "cursor row before the sweep, forcing a full re-fetch; other sources are untouched",
    )
