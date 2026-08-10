"""`plexdb publish` — publish a read-only snapshot for consumers."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..store import publish as publish_snapshot


def _cmd_publish(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    if config.snapshot_path is None:
        raise ConfigError(
            "PLEXDB_SNAPSHOT_PATH is not set — point it at where the published snapshot should land"
        )
    version, size = publish_snapshot(config.store_path, config.snapshot_path)
    where = config.snapshot_path.resolve()
    print(f"published snapshot at schema v{version}, {size} bytes: {where}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    publish_parser = sub.add_parser(
        "publish",
        help="publish a read-only snapshot for consumers; safe to re-run",
    )
    publish_parser.set_defaults(func=_cmd_publish)
