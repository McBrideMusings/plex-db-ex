"""`plexdb local-edges` — recompute `local_collection` edges from Plex
collection co-membership; the whole set is replaced every run, never
staled."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..local_edges import DEFAULT_REPORT_THRESHOLD, refresh_local_edges
from ..plex_client import LivePlexClient
from ..store import open_store

ORDER = 47


def _cmd_local_edges(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to compute local edges")
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = refresh_local_edges(conn, client, report_threshold=args.report_threshold)

    print(
        f"{stats.collections_processed} collection(s) processed "
        f"({stats.collections_skipped_smart} smart, skipped), "
        f"{stats.edges_written} edge(s) written, "
        f"{stats.members_skipped_not_in_library} member(s) skipped (not in library)"
    )
    for large in stats.large_collections:
        print(
            f"large collection: {large.title!r} has {large.member_count} member(s) "
            "— worth a second look"
        )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    local_edges_parser = sub.add_parser(
        "local-edges",
        help="recompute local_collection edges from Plex collection co-membership; "
        "the whole set is replaced every run, never staled",
    )
    local_edges_parser.add_argument(
        "--report-threshold",
        type=int,
        default=DEFAULT_REPORT_THRESHOLD,
        metavar="N",
        help="flag a collection in the summary once its resolvable membership reaches "
        f"this many titles; default {DEFAULT_REPORT_THRESHOLD}",
    )
    local_edges_parser.set_defaults(func=_cmd_local_edges)
