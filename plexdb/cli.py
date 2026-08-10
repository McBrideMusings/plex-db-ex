"""The `plexdb` command line.

Subcommands are added as slices land. Today there are two: `init` and `publish`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from . import __version__, schema
from .config import Config
from .errors import ConfigError, PlexdbError
from .store import init as init_store
from .store import publish as publish_snapshot


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
