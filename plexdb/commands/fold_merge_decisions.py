"""`plexdb fold-merge-decisions` — apply `merge_decisions.json` to
`keyword_pairs.decision`.

Plex TVX never writes `plexdb.db` (ADR-0007, ADR-0017), so this is the only
way a person's accept, reject or clear reaches the table.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..decisions import FoldStats
from ..store import open_store
from ..sweep import Step
from ..synonyms import fold_merge_decisions, merge_decisions_path

NAME = "fold-merge-decisions"
#: Right after `migrate`, so a sweep starts by applying what a person decided
#: since the last one, and needs neither Jev nor the embedding server to do it.
ORDER = 11
#: A malformed file costs the sweep one step's report, not a night's library read.
SWEEP = Step.BEST_EFFORT


def describe(stats: FoldStats, path: str) -> str:
    if not stats.file_found:
        return f"merge decisions: no file at {path}"
    return (
        f"merge decisions: {stats.entries:,} entr(ies) in {path}, "
        f"{stats.matched:,} matched a pair, {stats.unmatched:,} named no pair"
    )


def _cmd_fold_merge_decisions(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = merge_decisions_path(config)
    with open_store(config.store_path) as conn:
        stats = fold_merge_decisions(conn, path)
    print(describe(stats, str(path)))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="apply merge_decisions.json (accepted, rejected, cleared) to keyword_pairs.decision",
    )
    parser.set_defaults(func=_cmd_fold_merge_decisions)
