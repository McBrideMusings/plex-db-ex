"""`plexdb frames` — the Frame index's operator surface (ADR-0024).

`plexdb frames status` prints one JSON object describing `frames.db`; its shape
is documented in docs/schema.md. `plexdb frames queue <item_id>` asks the
harvester to index a title ahead of the backlog. `plexdb frames migrate` brings
`frames.db` to this build's schema. Each creates `frames.db` and applies its
migrations when needed, and none writes `plexdb.db`.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from ..config import Config
from ..frames import frames_path, migrate_configured, open_frames, queue, status
from ..store import open_readonly

NAME = "frames"
#: With the other look-and-ask commands after the sweep's own steps. No `SWEEP`:
#: the harvester, not the nightly run, fills the Frame index.
ORDER = 75


def _cmd_status(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = frames_path(config)
    with open_frames(path) as frames_conn, open_readonly(config.store_path) as store_conn:
        report = status(
            frames_conn,
            store_conn,
            path=path,
            hours=config.frame_hours,
            now=datetime.now().astimezone(),
        )
    print(json.dumps(report, indent=2))
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = frames_path(config)
    with open_readonly(config.store_path) as store_conn:
        known = (
            store_conn.execute("SELECT 1 FROM items WHERE item_id = ?", (args.item_id,)).fetchone()
            is not None
        )
    with open_frames(path) as frames_conn:
        added = queue(
            frames_conn,
            args.item_id,
            known=known,
            by=args.by,
            now=datetime.now().astimezone(),
        )
        waiting = int(frames_conn.execute("SELECT count(*) FROM frame_queue").fetchone()[0])
    verb = "queued" if added else "already queued"
    print(f"{verb} {args.item_id}; {waiting:,} title(s) in the queue")
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    frames_path(config)
    print(migrate_configured(config))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME, help="report on the Frame index, queue a title for it, or migrate it"
    )
    actions = parser.add_subparsers(dest="frames_command", required=True)

    migrate_parser = actions.add_parser(
        "migrate", help="create frames.db or bring it to the current schema; safe to re-run"
    )
    migrate_parser.set_defaults(func=_cmd_migrate)

    status_parser = actions.add_parser(
        "status", help="print frames.db's coverage, size, hours and failures as JSON"
    )
    status_parser.set_defaults(func=_cmd_status)

    queue_parser = actions.add_parser(
        "queue", help="index this title next; exits non-zero for an item_id not in items"
    )
    queue_parser.add_argument("item_id")
    queue_parser.add_argument(
        "--by", default="cli", help="who asked, stored as queued_by (default: cli)"
    )
    queue_parser.set_defaults(func=_cmd_queue)
