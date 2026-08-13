"""`plexdb migrate` — bring the store up to the current schema, creating it if
it does not exist yet.

Named for what it does on a store that already holds twenty months of watch
history, which is every run but the first. It was called `init` until the name
sent someone looking for a set-up command and left the one that alters a live
database sounding harmless.
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..store import migrate
from ..sweep import Step

NAME = "migrate"
ORDER = 10
#: Applies pending migrations. A sweep against a store the build cannot open
#: has nothing to walk into.
SWEEP = Step.REQUIRED


def _cmd_migrate(_args: argparse.Namespace) -> int:
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
    migrate_parser = sub.add_parser(
        NAME,
        help="bring the store up to the current schema (creates it if absent); safe to re-run",
    )
    migrate_parser.set_defaults(func=_cmd_migrate)
