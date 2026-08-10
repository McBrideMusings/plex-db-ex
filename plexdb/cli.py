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

    Modules are visited by their own `ORDER`, ties broken by module name, so
    `--help` lists commands in the order someone runs them rather than
    alphabetically, and lists them the same way on every machine. Each module
    owns its number, so adding a command still edits no existing file.

    A module missing `register` or `ORDER` is a programming error: it fails
    loudly, naming the module, rather than being silently skipped or silently
    sorted last.
    """
    modules = [
        importlib.import_module(info.name)
        for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
    ]
    for module in modules:
        for attribute in ("register", "ORDER"):
            if not hasattr(module, attribute):
                raise RuntimeError(
                    f"{module.__name__} does not define {attribute} — every module under "
                    f"{package.__name__}/ must expose one"
                )
    for module in sorted(modules, key=lambda module: (module.ORDER, module.__name__)):
        module.register(sub)


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
