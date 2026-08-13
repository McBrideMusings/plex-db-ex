"""Asking a store how it is, without changing it.

Every other way to learn something about the store alters it or copies it: a
migration is the only thing that runs `PRAGMA quick_check`, and it runs it on
the way past; row counts mean pulling 130 MB down to a laptop. So a walk that
failed with `table external_ids has no column named last_seen` read as a broken
command for a minute or two, when the store was simply one schema version behind
the code that had just been deployed (issue #59).

Nothing here writes, migrates, or backs anything up. It opens the store
read-only, so it is safe against the live file mid-sweep.

**It runs against stores this build does not agree with.** That is the case it
exists for, so every question is asked defensively: a table or a column the
store has not reached yet is reported as absent rather than raising. A check
that only worked on a current store could not tell you a store was old.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import backup, schema, store

#: Reported in this order — the three guarded tables first, since they are the
#: ones no re-run reproduces, then the rest of the shape of the store.
COUNTED_TABLES = (
    *backup.GUARDED_TABLES,
    "external_ids",
    "plex_items",
    "edges",
    "collection_membership",
)


#: How old the newest row of a kind may be before the report says so. The sweep
#: runs nightly, so one missed night plus slack: anything past this means a step
#: has been failing quietly, or — for plays — that nobody watched anything for
#: three days, which is worth a glance either way. It changes no exit code; a
#: quiet week is not a damaged store.
STALE_DAYS = 3.0


@dataclass(frozen=True)
class Freshness:
    """The newest timestamp in one table, and how old it is."""

    label: str
    #: Exactly what the column held, kept so a value that could not be read is
    #: still shown rather than disappearing from the report.
    raw: str
    #: `None` when `raw` is not a timestamp this code can read.
    when: datetime | None
    age_days: float | None

    @property
    def unreadable(self) -> bool:
        return self.when is None

    @property
    def stale(self) -> bool:
        return self.age_days is not None and self.age_days > STALE_DAYS


@dataclass(frozen=True)
class Duplicate:
    """One identity that more than one Plex rating key points at."""

    item_id: str
    title: str
    rating_keys: tuple[str, ...]


@dataclass(frozen=True)
class Backups:
    """What is sitting in the backups directory, which nothing prunes."""

    count: int
    size: int


@dataclass(frozen=True)
class Report:
    """What one look at a store found. Printed by `plexdb check`."""

    path: Path
    version: int
    expected_version: int
    quick_check: str
    counts: dict[str, int]
    freshness: tuple[Freshness, ...]
    duplicates: tuple[Duplicate, ...]
    fs_identities: int
    backup_dir: Path
    #: `None` when `backup_dir` does not exist — a checkout that has never
    #: migrated has nothing to report, which is not an empty directory.
    backups: Backups | None

    @property
    def current(self) -> bool:
        return self.version == self.expected_version

    @property
    def sound(self) -> bool:
        return self.quick_check == "ok"

    @property
    def healthy(self) -> bool:
        """Whether `plexdb check` exits zero.

        Only two things make a store *wrong*: it is at a version this build does
        not speak, or SQLite says the file is damaged. Nine-day-old plays and a
        rising `fs:` count are things to look at, not things to fail on — a
        non-zero exit for them would make the command useless as a gate the day
        an enrichment source is down.
        """
        return self.current and self.sound


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has(conn: sqlite3.Connection, table: str, column: str, tables: set[str]) -> bool:
    return table in tables and column in _columns(conn, table)


def _read_text(raw: str) -> datetime | None:
    """An ISO-8601 text stamp as a datetime, or `None` if it cannot be read.

    Every writer stamps these with an offset-aware `isoformat()`, so a value
    without one is not something this store produced; it is read as UTC rather
    than raising. A value that is not a timestamp at all is reported verbatim by
    the caller rather than dropped — a mangled `fetched_at` must not read the
    same as a table nobody has ever written to.
    """
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _read_epoch(raw: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _freshness(conn: sqlite3.Connection, tables: set[str], now: datetime) -> tuple[Freshness, ...]:
    """The newest row in each table that records when something last happened.

    A store whose newest play is nine days old is a store whose ingest has been
    failing quietly, and nothing else in this report would show that.
    """
    found: list[Freshness] = []
    candidates: list[tuple[str, str, str, bool]] = [
        ("newest play", "plays", "viewed_at", True),
        ("newest enrichment", "enrichment", "fetched_at", False),
        ("newest external id seen", "external_ids", "last_seen", False),
        ("newest rating key seen", "plex_items", "last_seen", False),
    ]
    for label, table, column, epoch in candidates:
        if not _has(conn, table, column, tables):
            continue
        row = conn.execute(f"SELECT max({column}) FROM {table}").fetchone()  # noqa: S608
        if row is None or row[0] is None:
            continue
        raw = str(row[0])
        when = _read_epoch(raw) if epoch else _read_text(raw)
        age = None if when is None else (now - when).total_seconds() / 86400
        found.append(Freshness(label, raw, when, age))
    return tuple(found)


def _duplicates(conn: sqlite3.Connection, tables: set[str]) -> tuple[Duplicate, ...]:
    """Identities more than one rating key points at, named rather than counted.

    Since the repair in issue #58 this set should be genuine duplicates only —
    the same film present twice in the library. Listing them is how the seventh
    one gets noticed; a bare count would only say the number moved.
    """
    if "plex_items" not in tables:
        return ()
    titled = "items" in tables
    title = "coalesce(i.title, '')" if titled else "''"
    join = "LEFT JOIN items i ON i.item_id = p.item_id" if titled else ""
    rows = conn.execute(
        f"SELECT p.item_id, {title} AS title, group_concat(p.rating_key) AS keys "  # noqa: S608
        f"FROM plex_items p {join} "
        "GROUP BY p.item_id HAVING count(*) > 1 ORDER BY p.item_id"
    ).fetchall()
    return tuple(
        Duplicate(
            item_id=str(row["item_id"]),
            title=str(row["title"]),
            rating_keys=_sorted_keys(str(row["keys"])),
        )
        for row in rows
    )


def _sorted_keys(concatenated: str) -> tuple[str, ...]:
    """`group_concat` has no defined order, so the keys are sorted here — as
    numbers where they are numbers, since Plex's rating keys are digits and
    sorting them as text puts 100 before 9."""

    def order(key: str) -> tuple[int, int, str]:
        return (0, int(key), "") if key.isdigit() else (1, 0, key)

    return tuple(sorted(concatenated.split(","), key=order))


def _fs_identities(conn: sqlite3.Connection, tables: set[str]) -> int:
    """Titles Plex reported no external id for, so their identity is a path hash
    (ADR-0002). A rising count means Plex has stopped matching titles it used
    to."""
    if "items" not in tables:
        return 0
    row = conn.execute("SELECT count(*) FROM items WHERE item_id LIKE 'fs:%'").fetchone()
    return int(row[0])


def _backups(directory: Path) -> Backups | None:
    """What is in the backups directory, or `None` when there is no such
    directory. Nothing prunes the pre-migration copies by design, so the number
    and the space they take is something to be able to see."""
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.db"))
    return Backups(count=len(files), size=sum(f.stat().st_size for f in files))


def inspect(path: Path, backup_dir: Path, *, now: datetime | None = None) -> Report:
    """Open the store read-only and report on it. Writes nothing."""
    now = now or datetime.now(tz=UTC)
    with store.open_readonly(path) as conn:
        tables = _tables(conn)
        return Report(
            path=path.resolve(),
            version=schema.current_version(conn),
            expected_version=schema.SCHEMA_VERSION,
            quick_check=store.quick_check(conn),
            counts=backup.counts_for(conn, COUNTED_TABLES),
            freshness=_freshness(conn, tables, now),
            duplicates=_duplicates(conn, tables),
            fs_identities=_fs_identities(conn, tables),
            backup_dir=backup_dir.resolve(),
            backups=_backups(backup_dir),
        )
