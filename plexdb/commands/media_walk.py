"""`plexdb media-walk` — record each movie's and episode's files and tracks from Plex."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..media import media_walk
from ..plex_client import LivePlexClient
from ..store import open_store
from ..sweep import Step

NAME = "media-walk"
#: After `walk` (20), which writes the rating-key map this reads.
ORDER = 21
#: The tracks are a fact about a title, not what the sweep publishes a library
#: from, so a Plex hiccup here must not stop yesterday's snapshot being replaced.
SWEEP = Step.BEST_EFFORT


def _cmd_media_walk(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.plex_url or not config.plex_token:
        raise ConfigError("PLEX_URL and PLEX_TOKEN must be set in .env to read Plex's media")
    client = LivePlexClient(config.plex_url, config.plex_token)
    with open_store(config.store_path) as conn:
        stats = media_walk(conn, client, section_key=args.section)
    print(
        f"media: {stats.titles_listed} title(s) listed, {stats.unchanged} unchanged, "
        f"{stats.fetched} fetched, {stats.parts_written} part(s) and "
        f"{stats.streams_written} stream(s) written, {stats.emptied} emptied, "
        f"{stats.missing} missing from Plex"
    )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="record each movie's and episode's files and tracks from Plex; "
        "only titles whose Plex updatedAt changed are fetched",
    )
    parser.add_argument(
        "--section",
        metavar="KEY",
        default=None,
        help="read only this section key; default: every movie- and show-shaped section",
    )
    parser.set_defaults(func=_cmd_media_walk)
