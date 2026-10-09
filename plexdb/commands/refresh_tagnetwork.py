"""`plexdb refresh-tagnetwork` — redraw Plex TVX's stored tag networks.

Draws the default network (no noise excluded) of movies and of shows from the
keyword rows and stores it, skipping a kind whose stored network was drawn
from the same rows. The sweep runs it right after `refresh-map`, and it is
the way to run it by hand against a pulled copy.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from ..config import Config
from ..store import open_store
from ..sweep import Step
from ..tagnetwork import Refreshed, refresh_tag_networks

NAME = "refresh-tagnetwork"
#: Right after `refresh-map` and before `publish`, which snapshots what this writes.
ORDER = 68
#: Plex TVX draws a missing or stale network live, so a failed refresh
#: costs load time and nothing else.
SWEEP = Step.BEST_EFFORT


def describe(results: Iterable[Refreshed]) -> list[str]:
    lines = []
    for r in results:
        if r.redrawn:
            lines.append(f"tag network: {r.kind}: drew {r.nodes:,} node(s) in {r.seconds:.1f}s")
        else:
            lines.append(f"tag network: {r.kind}: current, {r.nodes:,} node(s) kept")
    return lines


def _cmd_refresh_tagnetwork(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open_store(config.store_path) as conn:
        results = refresh_tag_networks(conn)
    for line in describe(results):
        print(line)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="redraw Plex TVX's stored tag networks; an unchanged kind is kept",
    )
    parser.set_defaults(func=_cmd_refresh_tagnetwork)
