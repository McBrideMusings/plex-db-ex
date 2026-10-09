"""`plexdb film-suffix-verdicts` — pair every stored "X film" with its bare "X" as
a merge verdict, the deterministic first step of the keyword cleanup pass.

Wikidata names a genre "horror film" where TMDB says "horror"; Jev reads the two
as different things (a genre, and the subject), so a rule writes these pairs
instead (ADR-0018). `keywords.FILM_SUFFIX_KEEP` lists the bare keywords whose
"film" form means something else.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from ..config import Config
from ..keywords import FilmSuffixStats, write_film_suffix_verdicts
from ..store import open_store
from ..sweep import Step

NAME = "film-suffix-verdicts"
#: After the prune (63), so it pairs only values a title carries, and before the
#: pair judge (65), which then skips every pair this rule already holds.
ORDER = 64
#: It needs no service, and a failure leaves last night's verdicts in place.
SWEEP = Step.BEST_EFFORT


def describe(stats: FilmSuffixStats) -> list[str]:
    return [
        f"film-suffix rule: {stats.pairs:,} pair(s) held, {stats.pairs_written:,} written now, "
        f"{stats.pairs_removed:,} removed"
    ]


def _cmd_film_suffix_verdicts(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with open_store(config.store_path) as conn:
        stats = write_film_suffix_verdicts(conn, now)
    for line in describe(stats):
        print(line)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help='pair each stored "X film" with its bare "X" as a merge verdict',
    )
    parser.set_defaults(func=_cmd_film_suffix_verdicts)
