"""The `plexdb` command line.

Subcommands are added as slices land. Today there are five: `init`, `walk`,
`publish`, `enrich-tmdb-keywords`, and `ingest-plays`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from . import __version__, schema
from .config import Config
from .enrich_tmdb import NAMESPACE as TMDB_KEYWORDS_NAMESPACE
from .enrich_tmdb import enrich_tmdb_keywords
from .enrich_tmdb import wipe_namespace as enrich_wipe_namespace
from .errors import ConfigError, PlexdbError
from .plays import ingest_plays
from .plex_client import LivePlexClient
from .store import init as init_store
from .store import open_store
from .store import publish as publish_snapshot
from .tmdb_client import LiveTMDbClient
from .walk import walk_all


def _cmd_init(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    was, now = init_store(config.store_path)
    where = config.store_path.resolve()
    if was == now:
        print(f"store already current at schema v{now}: {where}")
    elif was == 0:
        print(f"created store at schema v{now}: {where}")
    else:
        print(f"migrated store v{was} -> v{now}: {where}")
    return 0


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


def _cmd_enrich_tmdb_keywords(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.tmdb_api_key:
        raise ConfigError("TMDB_API_KEY must be set in .env to fetch TMDB keywords")
    stale_days = args.stale_days if args.stale_days is not None else config.tmdb_keywords_stale_days
    client = LiveTMDbClient(config.tmdb_api_key)
    with open_store(config.store_path) as conn:
        if args.rewipe:
            removed = enrich_wipe_namespace(conn)
            print(f"wiped {removed} row(s) from the {TMDB_KEYWORDS_NAMESPACE} namespace")
        stats = enrich_tmdb_keywords(conn, client, stale_days=stale_days)
    print(
        f"tmdb keywords: {stats.titles_seen} title(s) seen, "
        f"{stats.titles_fetched} fetched, {stats.titles_cached} already cached, "
        f"{stats.titles_skipped_no_tmdb_id} skipped (no tmdb id), "
        f"{stats.titles_failed} failed, "
        f"{stats.keywords_written} keyword(s) written"
    )
    if stats.titles_failed:
        print(f"{stats.titles_failed} title(s) failed and were not cached — re-run to retry them")
    return 0


def _cmd_ingest_plays(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to ingest watch history")
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = ingest_plays(conn, client)
    print(
        f"ingested {stats.plays_written} play(s) from {stats.events_seen} event(s) seen, "
        f"{stats.already_recorded} already recorded"
    )
    if stats.unresolved_rating_key:
        print(
            f"{stats.unresolved_rating_key} event(s) had a rating key not in the walk's map, "
            "skipped — run `plexdb walk` to catch up"
        )
    if stats.unresolved_device:
        print(
            f"{stats.unresolved_device} event(s) had a device id not in Plex's device list; "
            "recorded with no client identifier or platform"
        )
    return 0


def _cmd_publish(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    if config.snapshot_path is None:
        raise ConfigError(
            "PLEXDB_SNAPSHOT_PATH is not set — point it at where the published snapshot should land"
        )
    version, size = publish_snapshot(config.store_path, config.snapshot_path)
    where = config.snapshot_path.resolve()
    print(f"published snapshot at schema v{version}, {size} bytes: {where}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plexdb",
        description="Extended Plex metadata and affinity store — the writer.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"plexdb {__version__} (schema v{schema.SCHEMA_VERSION})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser(
        "init",
        help="create the store and apply the schema; safe to re-run",
    )
    init_parser.set_defaults(func=_cmd_init)

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

    publish_parser = sub.add_parser(
        "publish",
        help="publish a read-only snapshot for consumers; safe to re-run",
    )
    publish_parser.set_defaults(func=_cmd_publish)

    enrich_tmdb_parser = sub.add_parser(
        "enrich-tmdb-keywords",
        help="fetch TMDB keywords for walked movies/shows into the tmdb_keywords "
        "namespace; a fresh row is never re-fetched",
    )
    enrich_tmdb_parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="N",
        help="re-fetch a title whose tmdb_keywords row is older than this many days; "
        "default: TMDB_KEYWORDS_STALE_DAYS, or 45 if that is unset",
    )
    enrich_tmdb_parser.add_argument(
        "--rewipe",
        action="store_true",
        help="delete every tmdb_keywords row before the sweep, forcing a full re-fetch; "
        "other namespaces are untouched",
    )
    enrich_tmdb_parser.set_defaults(func=_cmd_enrich_tmdb_keywords)

    ingest_plays_parser = sub.add_parser(
        "ingest-plays",
        help="ingest Plex watch history into plays; safe to re-run",
    )
    ingest_plays_parser.set_defaults(func=_cmd_ingest_plays)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except (PlexdbError, OSError, sqlite3.Error) as err:
        # Everything a person can cause by pointing PLEXDB_PATH somewhere odd
        # arrives here. OSError covers FileNotFoundError and PermissionError;
        # sqlite3.Error is the backstop for a database problem no layer below
        # thought to translate. A traceback in this position is a bug.
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
