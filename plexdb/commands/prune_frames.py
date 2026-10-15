"""`plexdb prune-frames` — delete `frames.db` rows for titles no longer in `items`.

A title Plex no longer has, or one whose item_id a repair re-keyed, leaves its
vectors, cuts, queue row and state row behind in the Frame index. This step
removes them. It reads `items` through a read-only connection and then writes
`frames.db` alone, so no transaction spans the two files (ADR-0024).
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import StoreError
from ..frames import PruneStats, frames_path, open_frames, prune_orphans
from ..store import open_readonly
from ..sweep import Step

NAME = "prune-frames"
#: Straight after `walk` (20) and the identity repairs (25, 26), the steps that
#: decide which item_ids exist.
ORDER = 27
#: Without PLEXDB_FRAMES_DIR it is skipped, and a failure leaves only rows that
#: match no title.
SWEEP = Step.BEST_EFFORT


def describe(stats: PruneStats) -> list[str]:
    counts = ", ".join(f"{count:,} {table} row(s)" for table, count in stats.deleted.items())
    lines = [f"pruned {len(stats.item_ids):,} title(s) no longer in items: {counts}"]
    lines.extend(f"pruned {item_id}" for item_id in stats.item_ids)
    return lines


def _cmd_prune_frames(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = frames_path(config)
    if not path.exists():
        print(f"no frames.db at {path}; nothing to prune")
        return 0
    with open_readonly(config.store_path) as store_conn:
        live = {str(row[0]) for row in store_conn.execute("SELECT item_id FROM items")}
    if not live:
        # A store re-created or restored before its first walk holds no titles,
        # and pruning against it would delete the whole Frame index — months of
        # encoder time to rebuild — when no title was actually removed.
        raise StoreError(f"items is empty in {config.store_path}; refusing to prune frames.db")
    with open_frames(path) as frames_conn:
        stats = prune_orphans(frames_conn, live)
    for line in describe(stats):
        print(line)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(NAME, help="delete frames.db rows whose item_id is no longer in items")
    parser.set_defaults(func=_cmd_prune_frames)
