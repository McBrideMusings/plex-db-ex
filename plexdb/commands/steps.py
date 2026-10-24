"""`plexdb steps [--json]` — every sweep step, in order, with its failure policy and
which implementation runs it.

The writer is moving to Rust one sweep step at a time (ADR-0001); this is the one
query that says how far the move has got. `--json` prints
`[{name, order, policy, impl}]`.
"""

from __future__ import annotations

import argparse
import json

from ..sweep import plan

NAME = "steps"
#: Beside `check` and `idle`: a read-only question, and no part of a sweep.
ORDER = 3


def _cmd_steps(args: argparse.Namespace) -> int:
    planned = plan()
    if args.json:
        rows = [
            {"name": s.name, "order": s.order, "policy": s.step.value, "impl": s.impl.value}
            for s in planned
        ]
        print(json.dumps(rows))
        return 0
    for s in planned:
        print(f"{s.order:>3}  {s.name:<26} {s.step.value:<12} {s.impl.value}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME, help="list every sweep step in order, its failure policy and its implementation"
    )
    parser.add_argument("--json", action="store_true", help="print a JSON list")
    parser.set_defaults(func=_cmd_steps)
