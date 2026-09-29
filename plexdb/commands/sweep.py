"""`plexdb sweep` — run one scheduled pass: every step in order, ending in a
published snapshot."""

from __future__ import annotations

import argparse

from ..sweep import run_locked_sweep

NAME = "sweep"
#: First in `--help`. It is the command you run; the rest are its parts.
ORDER = 5
# No SWEEP: a sweep does not contain itself.


def _cmd_sweep(_args: argparse.Namespace) -> int:
    return run_locked_sweep()


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sweep_parser = sub.add_parser(
        NAME,
        help="run every step in order and publish a snapshot; the whole scheduled pass",
    )
    sweep_parser.set_defaults(func=_cmd_sweep)
