"""`plexdb prune-keyword-verdicts` — delete `keyword_pairs`, `keyword_roles` and
`keyword_role_decisions` rows keyed on a keyword value no title carries any more.

A source rewrites a title's keywords on its next fetch, so a change to
`normalize_keyword`, or a source that stops listing a keyword, leaves rows keyed
on a value `enrichment` no longer holds. This step removes them; a person's
decision among them stays in its decisions file and comes back with the value.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..keywords import PruneStats, prune_keyword_verdicts
from ..store import open_store
from ..sweep import Step

NAME = "prune-keyword-verdicts"
#: After every writer that stores keywords (50-60), so it sees this sweep's
#: values, and before the judges (65, 66), so a moved value is judged the same
#: night and no judge is asked about a value that is about to be deleted.
ORDER = 64
#: It needs no service, and a failure leaves only rows that match nothing.
SWEEP = Step.BEST_EFFORT


def describe(stats: PruneStats) -> list[str]:
    lines = [
        f"pruned {len(stats.keywords_pruned):,} keyword value(s) no title carries "
        f"(of {stats.keywords_stored:,} stored): {stats.pairs_deleted:,} pair row(s), "
        f"{stats.roles_deleted:,} role row(s), {stats.decisions_deleted:,} decision row(s)"
    ]
    lines.extend(f"pruned keyword {keyword!r}" for keyword in stats.keywords_pruned)
    return lines


def _cmd_prune_keyword_verdicts(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open_store(config.store_path) as conn:
        stats = prune_keyword_verdicts(conn)
    for line in describe(stats):
        print(line)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="delete keyword_pairs, keyword_roles and keyword_role_decisions rows "
        "for keyword values no title carries",
    )
    parser.set_defaults(func=_cmd_prune_keyword_verdicts)
