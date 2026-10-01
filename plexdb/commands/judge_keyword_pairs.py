"""`plexdb judge-keyword-pairs` — find synonym keywords: embed the stored keywords
that have no `keyword_pairs` row, propose each one's nearest neighbours, ask Jev
whether the two mean the same thing, and write one row per judged pair
(ADR-0018)."""

from __future__ import annotations

import argparse
import os

from ..config import Config
from ..embed_client import LlamaSwapEmbedder
from ..errors import ConfigError
from ..jev_client import LiveJev
from ..store import open_store
from ..sweep import Step
from ..synonyms import JudgeStats, find_synonym_pairs

NAME = "judge-keyword-pairs"
#: After every step that writes keywords, before `publish`, which snapshots the table.
ORDER = 65
#: The embedding server and Jev are optional services behind adapters (ADR-0004).
#: A pair is judged once and kept, so a skipped or failed run costs nothing but
#: the pairs it would have added.
SWEEP = Step.BEST_EFFORT

EMBED_URL_VAR = "LLAMA_SWAP_BASE_URL"
JEV_KEY_VAR = "TYPESAFE_API_KEY"


def _require(name: str, purpose: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} must be set in .env to {purpose}")
    return value


def _positive(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _report(stats: JudgeStats) -> list[str]:
    return [
        f"keyword pairs: {stats.keywords_stored:,} keyword(s) stored, "
        f"{stats.keywords_pending:,} pending, {stats.keywords_examined:,} examined, "
        f"{stats.pairs_proposed:,} pair(s) proposed, "
        f"{stats.pairs_already_judged:,} already judged, {stats.pairs_judged:,} judged now"
    ]


def _cmd_judge_keyword_pairs(args: argparse.Namespace) -> int:
    config = Config.from_env()
    base_url = _require(EMBED_URL_VAR, "embed keywords")
    api_key = _require(JEV_KEY_VAR, "judge keyword pairs")
    with open_store(config.store_path) as conn:
        stats = find_synonym_pairs(
            conn,
            LlamaSwapEmbedder(base_url),
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
        help="ask Jev whether nearby keywords mean the same thing; a judged pair is never re-asked",
    )
    parser.add_argument(
        "--limit",
        type=_positive,
        default=None,
        metavar="N",
        help="examine at most N pending keywords this run; default: all of them",
    )
    parser.set_defaults(func=_cmd_judge_keyword_pairs)
