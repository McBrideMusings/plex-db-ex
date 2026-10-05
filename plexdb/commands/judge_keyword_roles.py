"""`plexdb judge-keyword-roles` — ask Jev, once per stored keyword, how likely it
is to be a tone, era, region, theme or character trait, and write one
`keyword_roles` row per role with `source = 'jev'` (ADR-0019)."""

from __future__ import annotations

import argparse

from ..config import Config
from ..jev_client import LiveJev
from ..roles import RoleStats, judge_keyword_roles
from ..store import open_store
from ..sweep import Step
from .judge_keyword_pairs import JEV_KEY_VAR, _positive, _require

NAME = "judge-keyword-roles"
#: After `judge-keyword-pairs` and every step that writes keywords, before
#: `publish`, which snapshots the table.
ORDER = 66
#: Jev is an optional service behind an adapter (ADR-0004). A keyword is judged
#: once and kept, so a skipped or failed run costs nothing but the keywords it
#: would have added.
SWEEP = Step.BEST_EFFORT


def _report(stats: RoleStats) -> list[str]:
    return [
        f"keyword roles: {stats.keywords_stored:,} keyword(s) stored, "
        f"{stats.keywords_already_judged:,} already judged, {stats.keywords_judged:,} judged now, "
        f"{stats.keywords_unjudgeable:,} unjudgeable, {stats.rows_written:,} row(s) written"
    ]


def _cmd_judge_keyword_roles(args: argparse.Namespace) -> int:
    config = Config.from_env()
    api_key = _require(JEV_KEY_VAR, "judge keyword roles")
    with open_store(config.store_path) as conn:
        stats = judge_keyword_roles(
            conn,
            LiveJev(api_key),
            limit=args.limit,
            log=lambda line: print(line, flush=True),
        )
    for line in _report(stats):
        print(line)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="ask Jev what roles each stored keyword plays; a judged keyword is never re-asked",
    )
    parser.add_argument(
        "--limit",
        type=_positive,
        default=None,
        metavar="N",
        help="ask Jev about at most N keywords this run; default: every unjudged keyword",
    )
    parser.set_defaults(func=_cmd_judge_keyword_roles)
