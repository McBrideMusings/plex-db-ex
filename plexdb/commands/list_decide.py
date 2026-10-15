"""`plexdb list-decide <url> <decision>` — decide a crowd list from the terminal.

Appends to `list_decisions.json`, the file the explorer's Lists tab writes, and never
touches the store: `plexdb fold-list-decisions` folds the file at the next sweep, and the
next `harvest-mdblist` or `harvest-letterboxd-lists` acts on it. Opens the store read-only
to check the list is known.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import PlexdbError
from ..list_decisions import (
    ListDecisions,
    UnknownList,
    decide,
    list_decisions_path,
)
from ..store import open_readonly

NAME = "list-decide"
ORDER = 73


def _cmd_list_decide(args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = list_decisions_path(config)
    decisions = ListDecisions(path)
    try:
        with open_readonly(config.store_path) as conn:
            entry = decide(conn, decisions, args.url, args.decision)
    except UnknownList as err:
        raise PlexdbError(
            f"no stored or decided list has the URL {err} — see `plexdb lists`"
        ) from None
    except ValueError as err:
        raise PlexdbError(str(err)) from None
    print(f"{entry['decision']} {entry['url']} -> {path} (applied at the next sweep)")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="record a decision about a crowd list in list_decisions.json; applied at the "
        "next sweep",
    )
    parser.add_argument(
        "url",
        help="an MDBList or Letterboxd list URL, e.g. https://mdblist.com/lists/lina/top "
        "or https://letterboxd.com/lina/list/top/",
    )
    parser.add_argument("decision", choices=ListDecisions.DECISIONS)
    parser.set_defaults(func=_cmd_list_decide)
