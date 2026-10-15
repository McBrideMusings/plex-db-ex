"""`plexdb list <collection_id|url>` — print one crowd list: its facts, every entry
in order, and the keywords its owned titles are known for.

Read-only: opens the store `open_readonly`. `--json` prints exactly what
`plexdb.lists.list_detail` returns, the function the explorer's Lists tab calls.
"""

from __future__ import annotations

import argparse
import json

from ..config import Config
from ..errors import PlexdbError
from ..lists import list_detail
from ..store import open_readonly

NAME = "list"
ORDER = 72


def _cmd_list(args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open_readonly(config.store_path) as conn:
        detail = list_detail(conn, args.list)
    if detail is None:
        raise PlexdbError(f"no stored list has the id or URL {args.list!r} — see `plexdb lists`")
    if args.json:
        print(json.dumps(detail, indent=2, ensure_ascii=False))
        return 0
    print(f"{detail['collection_id']}  {detail['name'] or '(unnamed)'}")
    print(f"  {detail['url'] or '(no url)'}  by {detail['author'] or '?'}")
    print(
        f"  {detail['source']}, {detail['provenance']}; owned {detail['owned']:,}/"
        f"{detail['entry_count']:,}, size {detail['size']}, watched {detail['watched']:,}, "
        f"likes {detail['likes']}, fetched {detail['fetched_at']}"
    )
    if detail["keywords"]:
        print(
            f"  keywords over-represented among {detail['keyword_titles_in_list']:,} owned "
            f"{detail['keyword_kind']} titles (library: {detail['keyword_titles_in_library']:,}):"
        )
        for row in detail["keywords"]:
            print(
                f"    {row['keyword']}: {row['in_list']} in list, "
                f"{row['in_library']:,} in library, x{row['lift']:.1f}"
            )
    for entry in detail["entries"]:
        rank = "-" if entry["rank"] is None else str(entry["rank"])
        year = f" ({entry['year']})" if entry["year"] else ""
        mark = ("watched" if entry["watched"] else "owned") if entry["owned"] else "not owned"
        print(f"  {rank:>5}  {entry['title'] or '(untitled)'}{year}  [{mark}]")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="print one stored crowd list with every entry in order; read-only",
    )
    parser.add_argument("list", metavar="COLLECTION_ID_OR_URL", help="e.g. mdblist:3892")
    parser.add_argument(
        "--json", action="store_true", help="print the list detail as JSON instead of text"
    )
    parser.set_defaults(func=_cmd_list)
