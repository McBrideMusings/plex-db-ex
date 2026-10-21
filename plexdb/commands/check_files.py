"""`plexdb check-files` — probe each new or changed file against the health rules, and decode
a nightly batch of files in full."""

from __future__ import annotations

import argparse
import shutil

from ..config import Config
from ..errors import MediaError
from ..frame_harvest import FfmpegMedia, locator
from ..media_check import check_files
from ..store import open_store
from ..sweep import Step

NAME = "check-files"
#: Straight after `media-walk` (21), which writes the parts this reads.
ORDER = 22
#: A verdict is a fact about a file, not something a library is published from.
SWEEP = Step.BEST_EFFORT


def _cmd_check_files(args: argparse.Namespace) -> int:
    config = Config.from_env()
    locate = locator(config)
    if shutil.which(config.ffprobe) is None:
        raise MediaError(
            f"{config.ffprobe} must be installed (or named by PLEXDB_FFPROBE) to check files",
            "ffprobe not installed",
        )
    media = FfmpegMedia(ffmpeg=config.ffmpeg, ffprobe=config.ffprobe)
    with open_store(config.store_path) as conn:
        stats = check_files(conn, media, locate, limit=args.limit, decode_batch=config.decode_batch)
    print(
        f"files: {stats.checked} checked, {stats.passed} passed, {stats.failed} failed, "
        f"{stats.skipped} left for the next run"
    )
    print(
        f"decode: {stats.decoded} decoded in full, {stats.decode_failed} failed, "
        f"{stats.decode_skipped} left for the next run"
    )
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="probe each new or changed file with ffprobe and record whether it keeps "
        "the health rules, then decode PLEXDB_DECODE_BATCH files in full (oldest decode "
        "first); needs PLEXDB_MEDIA_DIR and PLEX_SOURCE_ROOTS",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        default=None,
        help="check at most N files this run; default: every unchecked file",
    )
    parser.set_defaults(func=_cmd_check_files)
