"""`plexdb harvest-mdblist` — fetch MDBList's top crowd lists into the
`collection` and `collection_membership` tables (issue #34)."""

from __future__ import annotations

import argparse

from ..collections import MDBLIST_SOURCE, refresh_mdblist, wipe_source
from ..config import Config
from ..errors import ConfigError
from ..mdblist_client import LiveMDBListClient
from ..store import open_store

#: Last of the enrichment sources, after `local-edges` (47) and before
#: `ingest-plays` (50). A list entry can only resolve to a title `walk` already
#: wrote, so everything filling `items` and `external_ids` runs ahead of it.
ORDER = 48


def _cmd_harvest_mdblist(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.mdblist_api_key:
        raise ConfigError("MDBLIST_API_KEY must be set in .env to harvest MDBList collections")
    stale_days = args.stale_days if args.stale_days is not None else config.mdblist_stale_days
    client = LiveMDBListClient(config.mdblist_api_key)
    with open_store(config.store_path) as conn:
        if args.rewipe:
            memberships, collections = wipe_source(conn, MDBLIST_SOURCE)
            print(f"wiped {collections} collection(s) and {memberships} membership(s) from MDBList")
        stats = refresh_mdblist(conn, client, stale_days=stale_days)

    print(
        f"mdblist: {stats.lists_seen} list(s) seen, {stats.lists_fetched} fetched, "
        f"{stats.lists_cached} already cached, "
        f"{stats.memberships_written} membership(s) written, "
        f"{stats.entries_not_in_library} entry(s) skipped (not in library), "
        f"{stats.entries_duplicate} entry(s) skipped (already placed on that list)"
    )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    harvest_parser = sub.add_parser(
        "harvest-mdblist",
        help="fetch MDBList's top crowd lists into collection/collection_membership; "
        "a fresh list is never re-fetched",
    )
    harvest_parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="N",
        help="re-fetch a list once its stored snapshot is older than this many days; "
        "default: MDBLIST_STALE_DAYS, or 45 if that is unset",
    )
    harvest_parser.add_argument(
        "--rewipe",
        action="store_true",
        help="delete every MDBList collection and membership before the harvest, forcing a "
        "full re-fetch; other sources are untouched",
    )
    harvest_parser.set_defaults(func=_cmd_harvest_mdblist)
