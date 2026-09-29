"""`plexdb idle` — say whether nothing is writing the store right now.

The question `admin host-exec` asks before it runs a writer in the container.
The startup migration and the nightly sweep both run inside `plexdb schedule`,
so no process name gives either away; the locks they hold do
(`store.held_by`). Exits 0 only when nothing holds the store. Every other
outcome — a migration, a writer, or an error that kept it from looking — is
non-zero, so a caller that cannot tell refuses rather than proceeds.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..store import held_by

NAME = "idle"
#: Beside `check`: another read-only question, asked before running anything
#: else, and no part of a sweep.
ORDER = 2


def _cmd_idle(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    holder = held_by(config.store_path)
    if holder is None:
        print(f"{config.store_path}: idle")
        return 0
    print(f"{config.store_path}: {holder}")
    return 1


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    idle_parser = sub.add_parser(
        NAME,
        help="exit 0 only if no migration or writer holds the store now; changes nothing",
    )
    idle_parser.set_defaults(func=_cmd_idle)
