"""`plexdb harvest-frames` — fill the Frame index from the library, through Plex.

Runs until the hours in `PLEXDB_FRAME_HOURS` close (finishing the title in hand), the
work runs out, or the encoder stops answering. `--ignore-hours` runs outside the
window and `--limit N` stops after N titles, so one real title can be checked on
demand. Not a Step in any Sweep: `plexdb schedule` starts it when the hours open, and
it writes `frames.db` alone (`plexdb/frame_harvest.py`).
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..frame_encoder import from_config
from ..frame_harvest import configured_media, harvest
from ..frames import frames_path
from ..plex_client import LivePlexClient

NAME = "harvest-frames"
#: Beside `frames`, and no `SWEEP`: the nightly run does not index video.
ORDER = 76


def _cmd_harvest_frames(args: argparse.Namespace) -> int:
    config = Config.from_env()
    frames_path(config)
    media = configured_media(config)
    encoder = from_config(config)
    try:
        result = harvest(
            config,
            plex=LivePlexClient(config.plex_url, config.plex_token),
            media=media,
            encoder=encoder,
            ignore_hours=args.ignore_hours,
            limit=args.limit,
        )
    finally:
        encoder.close()
    print(
        f"harvest-frames stopped: {result.stopped}; "
        f"{result.indexed:,} indexed, {result.failed:,} failed"
    )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME, help="index titles into the Frame index through Plex, during PLEXDB_FRAME_HOURS"
    )
    parser.add_argument(
        "--ignore-hours", action="store_true", help="run now, whatever PLEXDB_FRAME_HOURS says"
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after this many titles")
    parser.set_defaults(func=_cmd_harvest_frames)
