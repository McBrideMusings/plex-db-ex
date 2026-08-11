"""`plexdb init` — create the store and apply the schema."""

from __future__ import annotations

import argparse

from ..config import Config
from ..store import init as init_store
from ..sweep import Step

NAME = "init"
ORDER = 10
#: Applies pending migrations. A sweep against a store the build cannot open
#: has nothing to walk into.
SWEEP = Step.REQUIRED


def _cmd_init(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    was, now = init_store(config.store_path)
    where = config.store_path.resolve()
    if was == now:
        print(f"store already current at schema v{now}: {where}")
    elif was == 0:
        print(f"created store at schema v{now}: {where}")
    else:
        print(f"migrated store v{was} -> v{now}: {where}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    init_parser = sub.add_parser(
        NAME,
        help="create the store and apply the schema; safe to re-run",
    )
    init_parser.set_defaults(func=_cmd_init)
