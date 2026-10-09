"""`plexdb refresh-map` — redraw Plex TVX's stored title maps.

Draws the default map (no noise excluded) of movies and of shows from the
keyword rows and stores it, skipping a kind whose stored map was drawn from the
same rows. The sweep runs it after both TMDB steps, and it is the way to run it
by hand against a pulled copy.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from ..config import Config
from ..store import open_store
from ..sweep import Step
from ..titlemap import Refreshed, refresh_title_maps

NAME = "refresh-map"
#: After every keyword writer and both judges (65, 66), so the map draws the merges
#: this sweep decided, and before `publish`, which snapshots what this writes.
ORDER = 67
#: Plex TVX draws a missing or stale map live, so a failed refresh costs
#: load time and nothing else.
SWEEP = Step.BEST_EFFORT


def describe(results: Iterable[Refreshed]) -> list[str]:
    lines = []
    for r in results:
        if r.redrawn:
            lines.append(f"title map: {r.kind}: drew {r.placed:,} title(s) in {r.seconds:.1f}s")
        else:
            lines.append(f"title map: {r.kind}: current, {r.placed:,} title(s) kept")
    return lines


def _cmd_refresh_map(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open_store(config.store_path) as conn:
        results = refresh_title_maps(conn)
    for line in describe(results):
        print(line)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="redraw Plex TVX's stored title maps; a kind whose keywords are unchanged is kept",
    )
    parser.set_defaults(func=_cmd_refresh_map)
