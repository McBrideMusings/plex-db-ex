"""`plexdb fold-role-decisions` — apply `role_decisions.json` to
`keyword_role_decisions`.

Plex TVX never writes `plexdb.db` (ADR-0007, ADR-0017), so this is the only
way a person's accept, reject or clear of a keyword's role reaches the table.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..decisions import FoldStats
from ..roles import fold_role_decisions, role_decisions_path
from ..store import open_store
from ..sweep import Step

NAME = "fold-role-decisions"
#: Right after `fold-merge-decisions`, for the same reason: a sweep starts by
#: applying what a person decided since the last one, and needs no service to do it.
ORDER = 12
#: A malformed file costs the sweep one step's report, not a night's library read.
SWEEP = Step.BEST_EFFORT


def describe(stats: FoldStats, path: str) -> str:
    if not stats.file_found:
        return f"role decisions: no file at {path}"
    return (
        f"role decisions: {stats.entries:,} entr(ies) in {path}, "
        f"{stats.matched:,} matched a keyword role, {stats.unmatched:,} named none"
    )


def _cmd_fold_role_decisions(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = role_decisions_path(config)
    with open_store(config.store_path) as conn:
        stats = fold_role_decisions(conn, path)
    print(describe(stats, str(path)))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="apply role_decisions.json (accepted, rejected, cleared) to keyword_role_decisions",
    )
    parser.set_defaults(func=_cmd_fold_role_decisions)
