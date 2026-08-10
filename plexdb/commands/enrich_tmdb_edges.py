"""`plexdb enrich-tmdb-edges` — fetch TMDB recommendations and similar titles
for walked movies and shows into the `edges` table, as `tmdb_recommendations`
and `tmdb_similar` edges."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..store import open_store
from ..tmdb_client import LiveTMDbClient
from ..tmdb_edges import (
    RECOMMENDATIONS_EDGE_TYPE,
    SIMILAR_EDGE_TYPE,
    refresh_tmdb_edges,
    wipe_edge_type,
)

ORDER = 45


def _cmd_enrich_tmdb_edges(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.tmdb_api_key:
        raise ConfigError("TMDB_API_KEY must be set in .env to fetch TMDB edges")
    stale_days = args.stale_days if args.stale_days is not None else config.tmdb_edges_stale_days
    client = LiveTMDbClient(config.tmdb_api_key)
    with open_store(config.store_path) as conn:
        if args.rewipe:
            for edge_type in (RECOMMENDATIONS_EDGE_TYPE, SIMILAR_EDGE_TYPE):
                edges_removed, _ = wipe_edge_type(conn, edge_type)
                print(f"wiped {edges_removed} edge(s) of type {edge_type}")
        stats_by_type = refresh_tmdb_edges(conn, client, stale_days=stale_days)

    any_failed = False
    for edge_type, stats in stats_by_type.items():
        print(
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
        print("some titles failed and were not cached — re-run to retry them")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    enrich_tmdb_edges_parser = sub.add_parser(
        "enrich-tmdb-edges",
        help="fetch TMDB recommendations/similar for walked movies/shows into "
        "tmdb_recommendations/tmdb_similar edges; a fresh set is never re-fetched",
    )
    enrich_tmdb_edges_parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="N",
        help="re-fetch a title's edge set once it is older than this many days; "
        "default: TMDB_EDGES_STALE_DAYS, or 45 if that is unset",
    )
    enrich_tmdb_edges_parser.add_argument(
        "--rewipe",
        action="store_true",
        help="delete every tmdb_recommendations/tmdb_similar edge and fetch cursor "
        "before the sweep, forcing a full re-fetch; other edge types are untouched",
    )
    enrich_tmdb_edges_parser.set_defaults(func=_cmd_enrich_tmdb_edges)
