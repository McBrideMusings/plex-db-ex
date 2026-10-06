"""`plexdb enrich-wikidata` — Wikidata's narrative location, set in period, main
subject and genre into `keywords` (source='wikidata'), with region and era roles,
and award received into `awards`."""

from __future__ import annotations

import argparse
import sqlite3

from ..enrich_wikidata import WikidataStats, enrich_wikidata, wipe
from ..sources import GatedSource
from ..sweep import Step
from ..wikidata_client import LiveWikidataClient

NAME = "enrich-wikidata"
#: After both TMDB steps, before `refresh-map` draws from the keywords.
ORDER = 56
#: An optional source behind an adapter (ADR-0004); a skipped pass costs only
#: freshness, and the next run picks up exactly the titles this one missed.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    return [f"wiped {wipe(conn)} wikidata row(s) from keywords, awards, roles and cursors"]


def _report(stats: WikidataStats) -> list[str]:
    lines = [
        f"wikidata: {stats.titles_seen} title(s) seen, "
        f"{stats.titles_fetched} fetched ({stats.titles_matched} matched) "
        f"in {stats.queries_sent} quer{'y' if stats.queries_sent == 1 else 'ies'}, "
        f"{stats.titles_cached} already cached, "
        f"{stats.titles_skipped_no_imdb_id} skipped (no imdb id), "
        f"{stats.titles_failed} failed",
        f"wikidata: {stats.keywords_written} keyword(s), "
        f"{stats.roles_stated} role row(s), {stats.awards_written} award(s) written, "
        f"{stats.roles_pruned} role row(s) deleted, their keyword on no title",
    ]
    if stats.titles_failed:
        lines.append(
            f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them"
        )
    return lines


SOURCE = GatedSource(
    name="wikidata",
    unit="title",
    # A lambda so a test can patch `LiveWikidataClient` in this module's globals.
    make_client=lambda: LiveWikidataClient(),
    refresh=enrich_wikidata,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch Wikidata setting, period, subject and genre keywords "
        "(source='wikidata'), region/era roles and awards for walked movies/shows "
        "by IMDb id; a fresh title is never re-asked",
        rewipe_help="delete every source='wikidata' keyword, award, role and cursor row "
        "before the sweep, forcing a full re-fetch; other sources are untouched",
    )
