"""`plexdb init` — create the store and apply the schema."""

from __future__ import annotations

import argparse

from ..config import Config
from ..store import migrate
from ..sweep import Step

NAME = "init"
ORDER = 10
#: Applies pending migrations. A sweep against a store the build cannot open
#: has nothing to walk into.
SWEEP = Step.REQUIRED


def _cmd_init(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    result = migrate(config.store_path, config.backup_dir)
    where = config.store_path.resolve()
    if result.was == result.now and result.was > 0:
        print(f"store already current at schema v{result.now}: {where}")
        return 0
    if result.was == 0:
        print(f"created store at schema v{result.now}: {where}")
        return 0

    print(f"backed up to {result.backup}")
    print(f"migrated store v{result.was} -> v{result.now}: {where}")
    for table, after in sorted(result.counts_after.items()):
        before = result.counts_before.get(table)
        if before is None:
            print(f"  {table}: {after:,} rows (new)")
        elif before == after:
            print(f"  {table}: {after:,} rows")
        else:
            print(f"  {table}: {before:,} -> {after:,} rows")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    init_parser = sub.add_parser(
        NAME,
        help="create the store and apply the schema; safe to re-run",
    )
    init_parser.set_defaults(func=_cmd_init)
