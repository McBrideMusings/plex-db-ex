"""`plexdb schedule` — wait for the daily run time, run one sweep, repeat.

The container's entrypoint, and the only command that never returns. It
declares no `SWEEP`, so it is not itself a step of a sweep — a scheduler that
scheduled itself would be a fork bomb with a clock.
"""

from __future__ import annotations

import argparse

from ..schedule import SCHEDULE_VAR, run_scheduler

NAME = "schedule"
#: After `publish`, because a person reading `plexdb --help` meets the commands
#: in the order they matter and this one wraps the whole run.
ORDER = 95


def _cmd_schedule(_args: argparse.Namespace) -> int:
    return run_scheduler()


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help=f"run `plexdb sweep` every day at {SCHEDULE_VAR} (HH:MM) and never return; "
        "the container entrypoint",
    )
    parser.set_defaults(func=_cmd_schedule)
