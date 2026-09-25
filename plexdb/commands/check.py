"""`plexdb check` — report the store's health without changing it.

The one command that is safe to point at the live store at any moment, mid-sweep
included: it opens read-only, never migrates, and takes no backup. `admin
host-exec check` therefore answers "what state is the live store in" without the
130 MB round trip `admin pull-baseline` costs — which matters when the answer is
what decides whether to deploy at all (issue #59).
"""

from __future__ import annotations

import argparse

from ..config import Config
from ..health import STALE_DAYS, Freshness, Report, inspect

NAME = "check"
#: First. It is what you run before deciding whether to run anything else, and
#: it declares no `SWEEP`, so it takes no part in a nightly run.
ORDER = 1


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:,.0f} MB"


def _freshness(entry: Freshness) -> str:
    """One freshness line's value.

    A stamp too old to be normal says so on the line itself: `8.9 days ago` only
    reads as wrong if you already know what normal is. A stamp that cannot be
    read is printed verbatim, because a mangled value and a table nobody has
    written to must not look the same.
    """
    if entry.when is None or entry.age_days is None:
        return f"unreadable timestamp {entry.raw!r}"
    when = entry.when.isoformat(timespec="seconds")
    flag = " — STALE" if entry.stale else ""
    return f"{when} ({entry.age_days:.1f} days ago){flag}"


def _version_line(report: Report) -> str:
    if report.current:
        return f"store is at schema v{report.version}; this build understands v{report.version}"
    if report.version > report.expected_version:
        return (
            f"store is at v{report.version}; this build only understands "
            f"v{report.expected_version} — upgrade plex-db-ex"
        )
    return (
        f"store is at v{report.version}; this build expects "
        f"v{report.expected_version} — run plexdb migrate"
    )


def render(report: Report) -> list[str]:
    """The report as printed lines. Separate from `_cmd_check` so what the
    command says can be asserted on without capturing stdout."""
    lines = [str(report.path), _version_line(report)]
    lines.append(
        "quick_check: ok" if report.sound else f"quick_check FAILED: {report.quick_check}"
    )

    lines.append("rows:")
    for table, count in report.counts.items():
        lines.append(f"  {table}: {count:,}")

    if report.freshness:
        lines.append(f"freshness (flagged over {STALE_DAYS:.0f} days):")
        for entry in report.freshness:
            lines.append(f"  {entry.label}: {_freshness(entry)}")

    lines.append(f"fs: identities: {report.fs_identities:,}")

    if report.keyword_counts:
        lines.append("keywords by source:")
        for source, count in report.keyword_counts.items():
            lines.append(f"  {source}: {count:,}")
    lines.append(f"keyword_forms: {report.keyword_forms_count:,}")
    if report.legacy_tmdb_keywords:
        lines.append(
            f"tmdb_keywords rows FAILED: {report.legacy_tmdb_keywords:,} row(s) never "
            "migrated to the keywords namespace (ADR-0016) — run plexdb migrate"
        )

    if report.duplicates:
        lines.append(f"identities with more than one rating key: {len(report.duplicates)}")
        for duplicate in report.duplicates:
            keys = ", ".join(duplicate.rating_keys)
            title = duplicate.title or "(no title)"
            lines.append(f"  {duplicate.item_id}  {title}  [{keys}]")
    else:
        lines.append("identities with more than one rating key: none")

    where = report.backup_dir
    if report.backups is None:
        lines.append(f"backups: no directory at {where}")
    else:
        plural = "" if report.backups.count == 1 else "s"
        lines.append(
            f"backups: {report.backups.count} file{plural}, "
            f"{_megabytes(report.backups.size)} in {where}"
        )
    return lines


def _cmd_check(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    report = inspect(config.store_path, config.backup_dir)
    for line in render(report):
        print(line)
    return 0 if report.healthy else 1


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    check_parser = sub.add_parser(
        NAME,
        help="report the store's health read-only; exits non-zero if it is behind or damaged",
    )
    check_parser.set_defaults(func=_cmd_check)
