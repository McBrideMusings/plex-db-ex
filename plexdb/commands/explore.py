"""`plexdb explore` — serve Plex TVX over the store, read-only.

A browser view of every `tmdb_keywords` value: how many titles carry it, its
IDF, what it travels with, and which titles those are. See `plexdb.explore`.
Declares no `SWEEP`, so a nightly run never starts it.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from ..config import Config
from ..explore import make_server

NAME = "explore"
#: After the commands that fill the store and before `publish`; a reader of what
#: they wrote, not a step in writing it.
ORDER = 80

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5194


def _cmd_explore(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.store_path.exists():
        print(f"no store at {config.store_path} — run `admin pull-baseline`", file=sys.stderr)
        return 1
    server = make_server(
        config.store_path,
        args.host,
        args.port,
        plex_url=config.plex_url,
        plex_token=config.plex_token,
    )
    url = f"http://{args.host}:{server.server_port}/"
    print(
        f"Plex TVX on {url} reading {config.store_path} (read-only); Ctrl-C to stop",
        flush=True,
    )
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(NAME, help="serve the read-only Plex TVX page in a browser")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"default {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    parser.set_defaults(func=_cmd_explore)
