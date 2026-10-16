"""The command line of `plexdb-read search`, built once for every Python caller.

`plexdb search` and the Plex DB-EX Search tab both run the reader crate's one
search (ADR-0024, ADR-0026). This module is where they agree on which binary,
which files and which flags, so neither can drift from the other.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from .config import Config
from .errors import ConfigError
from .frames import frames_path


def reader_binary(config: Config) -> str:
    """The `plexdb-read` binary, or a `ConfigError` saying how to get one."""
    found = shutil.which(config.read_bin)
    if found is None:
        raise ConfigError(
            f"cannot run {config.read_bin!r}: not found. The container image carries "
            "plexdb-read; elsewhere build it with `cargo build -p plexdb-reader` and set "
            "PLEXDB_READ_BIN to target/debug/plexdb-read"
        )
    return found


def search_argv(
    config: Config,
    phrase: str,
    *,
    snapshot: Path | None = None,
    negatives: Sequence[str] = (),
    weight: float | None = None,
    temperature: float | None = None,
    draw: int | None = None,
    seed: int | None = None,
) -> list[str]:
    """`plexdb-read search <phrase> --pool-file -`: the caller feeds the pool's
    item_ids, one per line, on standard input.

    `snapshot` names the Snapshot to search; unset means the configured one.
    """
    for text in (phrase, *negatives):
        if text.startswith("--"):
            raise ValueError("a phrase or negative cannot start with --")
    frames = frames_path(config)
    if not config.frame_encoder_url:
        raise ConfigError("PLEXDB_FRAME_ENCODER_URL is not set, so a new phrase cannot be embedded")
    if not config.frame_model:
        raise ConfigError("PLEXDB_FRAME_MODEL is not set, so a new phrase cannot be embedded")
    argv = [
        reader_binary(config),
        "--snapshot",
        str(snapshot or config.snapshot_path or config.store_path),
        "--frames",
        str(frames),
        "--phrase-cache",
        str(config.phrase_cache_path),
        "--encoder-url",
        config.frame_encoder_url,
        "--encoder-model",
        config.frame_model,
        "search",
        phrase,
        "--pool-file",
        "-",
    ]
    for negative in negatives or ():
        argv += ["--not", negative]
    for flag, value in (
        ("--weight", weight),
        ("--temperature", temperature),
        ("--draw", draw),
        ("--seed", seed),
    ):
        if value is not None:
            argv += [flag, str(value)]
    return argv
