"""The `plexdb` command line.

Subcommands live under `plexdb/commands/`, one module per command. Adding a
command is one new file there; this module discovers it and never changes.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sqlite3
import sys
from collections.abc import Sequence
from types import ModuleType

from . import __version__, commands, schema
from .errors import PlexdbError


def _register_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], package: ModuleType = commands
) -> None:
    """Import every module in `package` and call its `register(sub)`.

    Modules are visited in sorted name order, so `--help` lists commands the
    same way on every machine. A module with no `register` function is a
    programming error: it fails loudly, naming the module, rather than being
    silently skipped.
    """
    module_infos = sorted(
        pkgutil.iter_modules(package.__path__, f"{package.__name__}."),
        key=lambda info: info.name,
    )
    for info in module_infos:
        module = importlib.import_module(info.name)
        register = getattr(module, "register", None)
        if register is None:
            raise RuntimeError(
                f"{info.name} does not define register(sub) — every module under "
                f"{package.__name__}/ must expose one"
            )
        register(sub)


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
    _register_commands(sub)
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
