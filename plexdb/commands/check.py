"""`plexdb check` — report the store's health without changing it.

The one command that is safe to point at the live store at any moment, mid-sweep
included: it opens read-only, never migrates, and takes no backup. `admin
host-exec check` therefore answers "what state is the live store in" without the
130 MB round trip `admin pull-baseline` costs — which matters when the answer is
what decides whether to deploy at all (issue #59).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from ..config import Config
from ..health import STALE_DAYS, FramesHealth, Freshness, Report, inspect
from ..store import outside_migration

NAME = "check"
#: First. It is what you run before deciding whether to run anything else, and
#: it declares no `SWEEP`, so it takes no part in a nightly run.
ORDER = 1

#: How often `--wait` re-reads a store that is behind.
POLL_SECONDS = 5.0


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


def _frames_lines(health: FramesHealth | None) -> list[str]:
    """The Frame index's lines. Missing is a state, not a failure: the file
    appears with the first frames command or harvest."""
    if health is None:
        return ["frames.db: not configured (PLEXDB_FRAMES_DIR is unset)"]
    if not health.exists:
        return [f"frames.db: none yet at {health.path}"]
    lines = [f"frames.db: {health.path}"]
    if health.version is None:
        lines.append(f"frames.db FAILED: {health.quick_check}")
        return lines
    if health.current:
        lines.append(f"frames.db is at schema v{health.version}; this build understands it")
    elif health.version > health.expected_version:
        lines.append(
            f"frames.db FAILED: at v{health.version}; this build only understands "
            f"v{health.expected_version} — upgrade plex-db-ex"
        )
    else:
        lines.append(
            f"frames.db FAILED: at v{health.version}; this build expects "
            f"v{health.expected_version} — run plexdb frames migrate"
        )
    if health.quick_check is not None:
        lines.append(
            "frames.db quick_check: ok"
            if health.sound
            else f"frames.db quick_check FAILED: {health.quick_check}"
        )
    return lines


def render(report: Report) -> list[str]:
    """The report as printed lines. Separate from `_cmd_check` so what the
    command says can be asserted on without capturing stdout."""
    lines = [str(report.path), _version_line(report)]
    lines.append("quick_check: ok" if report.sound else f"quick_check FAILED: {report.quick_check}")

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

    if report.lists:
        lines.append("lists by source:")
        for source_lists in report.lists:
            share = (
                "no entries"
                if source_lists.owned_share is None
                else f"{source_lists.owned_share:.1%} owned"
            )
            lines.append(
                f"  {source_lists.source}: {source_lists.lists:,} list(s), "
                f"{source_lists.entries:,} entries stored, {share}"
            )
            if source_lists.failed:
                lines.append(
                    f"lists FAILED: {len(source_lists.failed):,} {source_lists.source} list(s) "
                    "failed to fetch"
                )
                for failed in source_lists.failed:
                    waiting = " — stopped until a retry decision" if failed.stopped else ""
                    lines.append(
                        f"  {failed.url}: {failed.attempts} failed fetch(es), last "
                        f"{failed.attempted_at}: {failed.reason}{waiting}"
                    )

    if report.duplicates:
        lines.append(f"identities with more than one rating key: {len(report.duplicates)}")
        for duplicate in report.duplicates:
            keys = ", ".join(duplicate.rating_keys)
            title = duplicate.title or "(no title)"
            lines.append(f"  {duplicate.item_id}  {title}  [{keys}]")
    else:
        lines.append("identities with more than one rating key: none")

    lines.extend(_frames_lines(report.frames))

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


def _look(config: Config, *, frames_deep: bool = False) -> Report | None:
    """One report on the store, or `None` while a migration holds it — a read
    taken then could see a version that a failed verify is about to roll back.

    `frames.db`'s `quick_check` is skipped unless `frames_deep`: it reads the
    whole file, and a `--wait` poll only needs the version rows."""
    with outside_migration(config.store_path) as clear:
        if not clear:
            return None
        return inspect(
            config.store_path,
            config.backup_dir,
            frames_path=config.frames_path,
            frames_deep=frames_deep,
        )


def await_current(
    look: Callable[[], Report | None],
    wait: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Report | None:
    """Re-read the store until no migration holds it and it and `frames.db` are
    at this build's schema versions, or until `wait` seconds pass. Returns the
    last report, or `None` if a migration held the store every time it looked.

    This is how a deploy's last step waits out the container's startup
    migration, which migrates `frames.db` straight after the store. A file
    behind the build, or one being migrated, is the only state waiting can
    change; a file newer than the build, or a damaged one at the right version,
    is returned at once.
    """
    deadline = clock() + wait
    report = look()
    while (report is None or report.behind) and clock() < deadline:
        if report is None:
            print("a migration is running; waiting", flush=True)
        elif report.version < report.expected_version:
            print(
                f"store is at v{report.version}; waiting for v{report.expected_version}",
                flush=True,
            )
        else:
            print("frames.db is behind this build; waiting", flush=True)
        sleep(POLL_SECONDS)
        report = look()
    return report


def _cmd_check(args: argparse.Namespace) -> int:
    config = Config.from_env()
    report = await_current(lambda: _look(config), args.wait)
    if report is not None:
        # One full look, taken once the polling is over: the report printed is
        # a single consistent read, and the only one that runs frames.db's
        # quick_check.
        report = _look(config, frames_deep=True)
    if report is None:
        print(f"{config.store_path}: a migration is still running — not read")
        return 1
    for line in render(report):
        print(line)
    return 0 if report.healthy else 1


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    check_parser = sub.add_parser(
        NAME,
        help="report the store's health read-only; exits non-zero if it is behind or damaged",
    )
    check_parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="while the store is behind this build or being migrated, re-read it "
        "until it catches up or this many seconds pass",
    )
    check_parser.set_defaults(func=_cmd_check)
