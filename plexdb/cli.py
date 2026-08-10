"""The `plexdb` command line.

Subcommands are added as slices land. Today there are three: `init`, `walk`, and `publish`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from . import __version__, schema
from .config import Config
from .errors import ConfigError, PlexdbError
from .plex_client import LivePlexClient
from .store import init as init_store
from .store import open_store
from .store import publish as publish_snapshot
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
    if stats.identity_kept_on_guid_change:
        print(
            f"{stats.identity_kept_on_guid_change} title(s) had a changed GUID set "
            "since the last walk; kept their existing item_id rather than forking a new row"
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
