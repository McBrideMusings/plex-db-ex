"""`plexdb idle [command ...]` — say whether nothing is writing the store right now.

The question `admin host-exec` asks before it runs a writer in the container.
The startup migration and the nightly sweep both run inside `plexdb schedule`,
so no process name gives either away; the locks they hold do
(`store.held_by`). Exits 0 only when nothing holds the store. Every other
outcome — a migration, a writer, or an error that kept it from looking — is
non-zero, so a caller that cannot tell refuses rather than proceeds.

Given a command, it runs that command while holding the store (`store.holding`)
and returns the command's exit code. The check and the claim are one act, so
nothing can start between asking and running, which two separate invocations
cannot promise.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..store import held_by, holding

NAME = "idle"
#: Beside `check`: another read-only question, asked before running anything
#: else, and no part of a sweep.
ORDER = 2


def _cmd_idle(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not args.command:
        holder = held_by(config.store_path)
        if holder is None:
            print(f"{config.store_path}: idle")
            return 0
        print(f"{config.store_path}: {holder}")
        return 1

    from ..cli import main

    with holding(config.store_path) as holder:
        if holder is not None:
            print(f"{config.store_path}: {holder}")
            return 1
        return main(args.command)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    idle_parser = sub.add_parser(
        NAME,
        help=(
            "exit 0 only if no migration or writer holds the store now; "
            "with a command, run it while holding the store"
        ),
    )
    idle_parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="a plexdb command and its arguments, run only if the store is idle",
    )
    idle_parser.set_defaults(func=_cmd_idle)
