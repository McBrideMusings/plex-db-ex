"""`plexdb harvest-plex-collections` — Plex's own hand-built collections into
the `collection` and `collection_membership` tables (issue #48)."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..plex_client import LivePlexClient
from ..plex_collections import refresh_plex_collections
from ..store import open_store
from ..sweep import Step

NAME = "harvest-plex-collections"
#: With the other membership sources. A collection member can only resolve to
#: a title `walk` already wrote, so everything filling `items` and
#: `plex_items` runs ahead of it.
ORDER = 61
#: Safe to sweep since #48. It used to be excluded because co-membership was
#: stored as pairs and one run wrote 16 million rows; it now writes one row per
#: membership — 19,365 against a real library — so a nightly pass costs
#: nothing. Best-effort rather than required: Plex being briefly unreachable
#: should not fail a whole sweep, and the next run recomputes wholesale anyway.
SWEEP = Step.BEST_EFFORT


def _cmd_harvest_plex_collections(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to harvest Plex collections")
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = refresh_plex_collections(conn, client)

    print(
        f"plex: {stats.collections_written} collection(s) written "
        f"({stats.collections_skipped_smart} smart, skipped), "
        f"{stats.memberships_written} membership(s) written, "
        f"{stats.members_skipped_not_in_library} member(s) skipped (not in library)"
    )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="harvest Plex's hand-built collections into collection/collection_membership; "
        "the whole set is replaced every run, never staled",
    )
    parser.set_defaults(func=_cmd_harvest_plex_collections)
