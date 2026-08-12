"""`plexdb repair-fs-identities` — move an fs: item_id still keyed on the disk
mount point to what the current identity rule produces (issue #55)."""

from __future__ import annotations

import argparse

from .. import backup
from ..config import Config
from ..errors import ConfigError, StoreError
from ..plex_client import LivePlexClient
from ..repair_fs_identities import FsRepairPlan, apply_fs_repairs, plan_fs_repairs
from ..store import open_store

NAME = "repair-fs-identities"
ORDER = 26
# No SWEEP: this moves an id a person has diagnosed as merely misnamed, not one
# the walk found broken — the walk cannot know a move is safe (ADR-0008), a
# person pointing this at a known problem can. Pointed at a known problem,
# never fired on a timer, same as repair-identities (ADR-0014).


def _summary(plan: FsRepairPlan, verb: str) -> str:
    """The one summary line both the dry run and the real run print, differing
    only in whether the moves already happened."""
    return (
        f"{plan.fs_found} identit(ies) keyed on fs:, {len(plan.moves)} {verb}, "
        f"{plan.unchanged} already correct, {plan.not_in_plex} skipped (left Plex "
        "since the last walk)"
    )


def _cmd_repair_fs_identities(args: argparse.Namespace) -> int:
    config = Config.from_env()

    if not config.plex_url or not config.plex_token:
        raise ConfigError(
            "PLEX_URL and PLEX_TOKEN must be set in .env to repair fs: identities — "
            "the corrected id needs each title's current playback path from Plex"
        )
    if not config.source_roots:
        raise ConfigError(
            "PLEX_SOURCE_ROOTS must be set in .env to repair fs: identities — it is "
            "the mount root a corrected id is computed relative to, the same "
            "requirement `plexdb walk` enforces"
        )
    client = LivePlexClient(config.plex_url, config.plex_token)

    with open_store(config.store_path) as conn:
        plan = plan_fs_repairs(conn, client, config.source_roots)

    if args.dry_run:
        print(_summary(plan, "would move"))
        for old_id in sorted(plan.moves)[:20]:
            print(f"  {old_id} -> {plan.moves[old_id]}")
        if len(plan.moves) > 20:
            print(f"  ... and {len(plan.moves) - 20} more")
        return 0

    if not plan.moves:
        if not plan.fs_found:
            print("no fs: identities found — nothing to repair")
        else:
            print(
                f"{plan.fs_found} identit(ies) keyed on fs:, all already correct or "
                "unreachable in Plex — nothing to move"
            )
        return 0

    taken = backup.take(config.store_path, config.backup_dir, "plexdb.pre-repair-fs-identities")
    try:
        with open_store(config.store_path) as conn:
            counts_before = backup.guarded_counts(conn)
            result = apply_fs_repairs(conn, plan)
            counts_after = backup.guarded_counts(conn)
    except Exception as err:
        backup.restore(taken.path, config.store_path)
        raise StoreError(
            f"repair-fs-identities failed and the store was rolled back from {taken.path}: {err}"
        ) from err

    # `items` and `enrichment` are both allowed to shrink here by design — a
    # merge folds two rows into one, or drops an exact duplicate fact. Compare
    # against exactly the shrinkage this pass itself accounts for, not a raw
    # before/after count: that blind check rolled back a repair that had
    # merged correctly, because a legitimate merge looks identical to row
    # loss under a plain count comparison.
    accounted_shrink = {
        "items": result.merges,
        "enrichment": result.duplicates_dropped.get("enrichment", 0),
    }
    for table, before in counts_before.items():
        after = counts_after.get(table, 0)
        expected = before - accounted_shrink.get(table, 0)
        if after != expected:
            backup.restore(taken.path, config.store_path)
            raise StoreError(
                f"repair-fs-identities rolled back from {taken.path}: "
                f"{table} went from {before:,} rows to {after:,}, "
                f"{expected:,} expected"
            )

    print(_summary(plan, "moved"))
    for table, count in sorted(result.rows_carried.items()):
        print(f"  {count} row(s) carried in {table}")
    if result.merges:
        print(f"  {result.merges} identit(ies) merged into one that another fs: id already reached")
    for table, count in sorted(result.duplicates_dropped.items()):
        print(f"  {count} duplicate row(s) in {table} dropped as part of a merge")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="move an fs: item_id still keyed on the disk mount point to what the "
        "current identity rule produces, carrying every row that references it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many identities would move and change nothing",
    )
    parser.set_defaults(func=_cmd_repair_fs_identities)
