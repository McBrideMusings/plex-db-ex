"""`plexdb fold-name-decisions` — apply `name_decisions.json` to `keyword_names`.

Plex TVX never writes `plexdb.db` (ADR-0007, ADR-0017), so this is the only way a
display name a person typed there reaches the table (ADR-0021).
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..decisions import FoldStats
from ..names import fold_name_decisions, name_decisions_path
from ..store import open_store
from ..sweep import Step

NAME = "fold-name-decisions"
#: Right after `fold-role-decisions`, for the same reason: a sweep starts by
#: applying what a person decided since the last one, and needs no service to do it.
ORDER = 13
#: A malformed file costs the sweep one step's report, not a night's library read.
SWEEP = Step.BEST_EFFORT


def describe(stats: FoldStats, path: str) -> str:
    if not stats.file_found:
        return f"name decisions: no file at {path}"
    return (
        f"name decisions: {stats.entries:,} entr(ies) in {path}, "
        f"{stats.matched:,} matched a stored keyword, {stats.unmatched:,} named none"
    )


def _cmd_fold_name_decisions(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = name_decisions_path(config)
    with open_store(config.store_path) as conn:
        stats = fold_name_decisions(conn, path)
    print(describe(stats, str(path)))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="apply name_decisions.json (named, cleared) to keyword_names",
    )
    parser.set_defaults(func=_cmd_fold_name_decisions)
