"""`plexdb search` — rank Shots against a Phrase (ADR-0024, ADR-0026).

The search itself is `plexdb-read search`, the reader crate's one implementation;
this command resolves where the files are from the environment, hands them to
that binary, and prints what it prints. The JSON shape is documented in
docs/schema.md under "Searching the Frame index".
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Config
from ..errors import ConfigError
from ..frames import frames_path

NAME = "search"
#: With the other look-and-ask commands, after `frames`. No `SWEEP`: a search is
#: asked for, never scheduled.
ORDER = 76


def _binary(config: Config) -> str:
    """The `plexdb-read` binary, or a `ConfigError` saying how to get one."""
    found = shutil.which(config.read_bin)
    if found is None:
        raise ConfigError(
            f"cannot run {config.read_bin!r}: not found. The container image carries "
            "plexdb-read; elsewhere build it with `cargo build -p plexdb-reader` and set "
            "PLEXDB_READ_BIN to target/debug/plexdb-read"
        )
    return found


def _pool(args: argparse.Namespace) -> str:
    ids: list[str] = list(args.pool or [])
    if args.pool_file:
        text = sys.stdin.read() if args.pool_file == "-" else Path(args.pool_file).read_text()
        ids.extend(line.strip() for line in text.splitlines() if line.strip())
    if not ids:
        raise ConfigError("name the titles to search with --pool or --pool-file")
    return "\n".join(ids)


def _cmd_search(args: argparse.Namespace) -> int:
    config = Config.from_env()
    frames = frames_path(config)
    cache = config.phrase_cache_path
    if not config.frame_encoder_url:
        raise ConfigError("PLEXDB_FRAME_ENCODER_URL is not set, so a new phrase cannot be embedded")
    snapshot = config.snapshot_path or config.store_path
    argv = [
        _binary(config),
        "--snapshot",
        str(snapshot),
        "--frames",
        str(frames),
        "--phrase-cache",
        str(cache),
        "--encoder-url",
        config.frame_encoder_url,
        "search",
        args.phrase,
        "--pool-file",
        "-",
    ]
    if args.not_:
        argv += ["--not", *args.not_]
    for flag, value in (
        ("--weight", args.weight),
        ("--temperature", args.temperature),
        ("--draw", args.draw),
        ("--seed", args.seed),
    ):
        if value is not None:
            argv += [flag, str(value)]
    # stdout and stderr go straight through: the binary's JSON is the answer, and
    # its exit code is this command's.
    return subprocess.run(argv, input=_pool(args), text=True, check=False).returncode


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="rank the Shots of a pool of titles against a Phrase and print them as JSON",
    )
    parser.add_argument("phrase", help="text to search the Frame index with")
    parser.add_argument(
        "--pool", nargs="+", metavar="ITEM_ID", help="the item_ids to search (put the phrase first)"
    )
    parser.add_argument(
        "--pool-file",
        metavar="PATH",
        help="a file of item_ids, one per line; - reads standard input",
    )
    parser.add_argument(
        "--not",
        dest="not_",
        nargs="+",
        metavar="PHRASE",
        help="phrases a Shot is penalised for resembling",
    )
    parser.add_argument(
        "--weight", type=float, help="how much of the strongest negative a Shot loses (default 0.5)"
    )
    parser.add_argument(
        "--temperature",
        type=float,
        help="give each Shot a probability, exp(score / T) over the Shots",
    )
    parser.add_argument("--draw", type=int, metavar="N", help="draw N Shots instead of listing all")
    parser.add_argument("--seed", type=int, help="seed for --draw (default 0)")
    parser.set_defaults(func=_cmd_search)
