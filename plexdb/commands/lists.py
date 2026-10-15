"""`plexdb lists` — print every crowd list the store holds, one row each.

Read-only: opens the store `open_readonly`. `--json` prints exactly what
`plexdb.lists.list_table` returns, the function the explorer's Lists tab calls.
"""

from __future__ import annotations

import argparse
import json

from ..config import Config
from ..lists import list_table
from ..store import open_readonly

NAME = "lists"
#: Next to `latent-users`: a report on what the enrichment steps stored, not a
#: pipeline step, so it declares no `SWEEP`.
ORDER = 71


def _cmd_lists(args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open_readonly(config.store_path) as conn:
        rows = list_table(conn, source=args.source)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no lists stored" + (f" for source {args.source}" if args.source else ""))
        return 0
    for row in rows:
        size = "?" if row["size"] is None else f"{row['size']:,}"
        likes = "?" if row["likes"] is None else f"{row['likes']:,}"
        print(
            f"{row['collection_id']}  {row['name'] or '(unnamed)'}  "
            f"[{row['provenance']}, by {row['author'] or '?'}]  "
            f"owned {row['owned']:,}/{row['entry_count']:,} (size {size})  "
            f"watched {row['watched']:,}  likes {likes}  fetched {row['fetched_at']}"
        )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="print every stored crowd list with its owned and watched counts; read-only",
    )
    parser.add_argument("--source", help="only this source's lists, e.g. mdblist")
    parser.add_argument(
        "--json", action="store_true", help="print the list table as JSON instead of text"
    )
    parser.set_defaults(func=_cmd_lists)
