"""`plexdb reconcile-etv` — compare `item_id` against `etv-station`'s
`entry_id` for every title both stores know about; read-only on both sides."""

from __future__ import annotations

import argparse

from ..config import Config
from ..errors import ConfigError
from ..reconcile_etv import ReconcileReport, reconcile
from ..store import open_readonly

ORDER = 60

#: How many rows of a "present in only one store" category to print by
#: default. Every mismatch always prints in full — that's the finding this
#: command exists to surface — but a store that has simply never been
#: reconciled can carry thousands of one-sided titles, and dumping all of
#: them is noise, not a finding. `--limit 0` prints none.
_DEFAULT_LIMIT = 50


def _print_one_sided(label: str, rows: tuple[tuple[str, str], ...], limit: int) -> None:
    print(f"{len(rows)} title(s) {label}")
    for rating_key, title in rows[:limit]:
        print(f"  rating_key={rating_key} title={title!r}")
    if len(rows) > limit:
        print(f"  … and {len(rows) - limit} more (raise --limit to see them)")


def _print_report(report: ReconcileReport, limit: int) -> None:
    print(
        f"compared {report.compared} title(s) present in both stores: "
        f"{report.agree} agree, {report.mismatched} differ"
    )
    for mismatch in report.mismatches:
        print(
            f"  rating_key={mismatch.rating_key} title={mismatch.title!r} "
            f"item_id={mismatch.item_id} entry_id={mismatch.entry_id}"
        )
        print(f"    reason: {mismatch.reason}")
    _print_one_sided(
        "in plex-db-ex only (etv-station never ingested this rating key from Plex)",
        report.only_in_plexdb,
        limit,
    )
    _print_one_sided(
        "in etv-station only (this store's walk never saw this rating key)",
        report.only_in_etv,
        limit,
    )


def _cmd_reconcile_etv(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if config.etv_catalog_path is None:
        raise ConfigError(
            "ETV_CATALOG_PATH must be set in .env to reconcile against etv-station's catalog"
        )
    with (
        open_readonly(config.store_path) as plexdb_conn,
        open_readonly(config.etv_catalog_path) as etv_conn,
    ):
        report = reconcile(plexdb_conn, etv_conn)
    _print_report(report, args.limit)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    reconcile_parser = sub.add_parser(
        "reconcile-etv",
        help="compare item_id against etv-station's entry_id for every title both "
        "stores know about; read-only on both sides",
    )
    reconcile_parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        metavar="N",
        help="how many 'present in only one store' titles to list per category "
        f"(default {_DEFAULT_LIMIT}); every mismatch always prints in full",
    )
    reconcile_parser.set_defaults(func=_cmd_reconcile_etv)
