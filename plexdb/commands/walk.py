"""`plexdb walk` — walk the Plex library into items, external_ids, and the
rating-key map."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plex_client import LivePlexClient
from ..store import open_store
from ..walk import walk_all

ORDER = 20


def _cmd_walk(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to walk the library")
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = walk_all(conn, client, config.source_roots, section_key=args.section)
    print(
        f"walked {stats.sections_walked} section(s): "
        f"{stats.titles_seen} title(s) seen, {stats.titles_written} written, "
        f"{stats.fallback_to_path} fell back to a path-derived id"
    )
    for kept, found_by in (
        (stats.identity_kept_on_guid_change, "rating key"),
        (stats.identity_kept_by_external_id, "external id"),
    ):
        if kept:
            print(
                f"{kept} title(s) would have derived a different item_id this walk; "
                f"kept their existing one (found by {found_by}) rather than forking a new row"
            )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    walk_parser = sub.add_parser(
        "walk",
        help="walk the Plex library into items, external_ids, and the rating-key map; "
        "safe to re-run",
    )
    walk_parser.add_argument(
        "--section",
        metavar="KEY",
        default=None,
        help="walk only this section key; default: every movie- and show-shaped section",
    )
    walk_parser.set_defaults(func=_cmd_walk)
