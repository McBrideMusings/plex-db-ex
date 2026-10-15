"""`plexdb fold-list-decisions` — apply `list_decisions.json` to `list_decision`.

Plex DB-EX never writes `plexdb.db` (ADR-0007, ADR-0017), so this is the only way a
decision a person made about a crowd list reaches the table the list harvest reads.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..decisions import FoldStats
from ..list_decisions import fold_list_decisions, list_decisions_path
from ..store import open_store
from ..sweep import Step

NAME = "fold-list-decisions"
#: Right after `fold-name-decisions`: a sweep starts by applying what a person decided
#: since the last one, and the harvest that acts on it runs much later.
ORDER = 14
#: A malformed file costs the sweep one step's report, not a night's library read.
SWEEP = Step.BEST_EFFORT


def describe(stats: FoldStats, path: str) -> str:
    if not stats.file_found:
        return f"list decisions: no file at {path}"
    return (
        f"list decisions: {stats.entries:,} entr(ies) in {path}, "
        f"{stats.matched:,} named an MDBList or Letterboxd list, {stats.unmatched:,} named none"
    )


def _cmd_fold_list_decisions(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = list_decisions_path(config)
    with open_store(config.store_path) as conn:
        stats = fold_list_decisions(conn, path)
    print(describe(stats, str(path)))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="apply list_decisions.json (added, excluded, refetch, retry, removed) to "
        "list_decision",
    )
    parser.set_defaults(func=_cmd_fold_list_decisions)
