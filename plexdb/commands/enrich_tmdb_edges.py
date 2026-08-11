"""`plexdb enrich-tmdb-edges` — fetch TMDB recommendations and similar titles
for walked movies and shows into the `edges` table, as `tmdb_recommendations`
and `tmdb_similar` edges."""

from __future__ import annotations

import argparse
import sqlite3

from ..sources import GatedSource
from ..sweep import Step
from ..tmdb_client import LiveTMDbClient
from ..tmdb_edges import (
    RECOMMENDATIONS_EDGE_TYPE,
    SIMILAR_EDGE_TYPE,
    EdgeStats,
    refresh_tmdb_edges,
    wipe_edge_type,
)

NAME = "enrich-tmdb-edges"
ORDER = 55
#: Same optional-source reasoning as the keywords sweep beside it.
SWEEP = Step.BEST_EFFORT


def _wipe(conn: sqlite3.Connection) -> list[str]:
    lines = []
    for edge_type in (RECOMMENDATIONS_EDGE_TYPE, SIMILAR_EDGE_TYPE):
        edges_removed, _ = wipe_edge_type(conn, edge_type)
        lines.append(f"wiped {edges_removed} edge(s) of type {edge_type}")
    return lines


def _report(stats_by_type: dict[str, EdgeStats]) -> list[str]:
    lines = []
    any_failed = False
    for edge_type, stats in stats_by_type.items():
        lines.append(
            f"{edge_type}: {stats.titles_seen} title(s) seen, {stats.titles_fetched} fetched, "
            f"{stats.titles_cached} already cached, "
            f"{stats.titles_skipped_no_tmdb_id} skipped (no tmdb id), "
            f"{stats.titles_failed} failed, "
            f"{stats.edges_written} edge(s) written, "
            f"{stats.edges_skipped_not_in_library} edge(s) skipped (target not in library)"
        )
        if stats.titles_failed:
            any_failed = True
    if any_failed:
        lines.append("some titles failed and were not cached — re-run to retry them")
    return lines


SOURCE = GatedSource(
    name="tmdb_edges",
    unit="title",
    credential="TMDB_API_KEY",
    credential_purpose="fetch TMDB edges",
    # Late-bound on purpose — see the note in enrich_tmdb_keywords.py.
    make_client=lambda api_key: LiveTMDbClient(api_key),
    refresh=refresh_tmdb_edges,
    wipe=_wipe,
    report=_report,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    SOURCE.register(
        sub,
        NAME,
        help="fetch TMDB recommendations/similar for walked movies/shows into "
        "tmdb_recommendations/tmdb_similar edges; a fresh set is never re-fetched",
        rewipe_help="delete every tmdb_recommendations/tmdb_similar edge and fetch cursor "
        "before the sweep, forcing a full re-fetch; other edge types are untouched",
    )
