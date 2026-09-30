"""`plexdb check` reports on a store without altering it.

The case it exists for is a store the running build does not agree with, so
these drive it against a current store, a store one version behind, and a
damaged one.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plexdb import schema
from plexdb.cli import main
from plexdb.commands.check import _cmd_check, await_current, render
from plexdb.errors import StoreError
from plexdb.health import Report, inspect
from plexdb.store import (
    _migrating,
    held_by,
    holding,
    init,
    migrate,
    open_store,
    outside_migration,
    writing,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def _store_at(path: Path, version: int) -> None:
    """Build a store at exactly `version` by applying only that many migrations."""
    with open_store(path, create=True) as conn:
        # Every migration up to and including v9 is a SQL batch (v10 is the
        # first Python step); this helper is never called at v10 itself.
        script = "".join(m for m in schema.MIGRATIONS[:version] if isinstance(m, str))
        conn.executescript(script)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()


def _populate(path: Path) -> None:
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')"
        )
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('fs:abc', 'movie', 'Home Video')"
        )
        for rating_key in ("11", "12"):
            conn.execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
                "VALUES (?, 'imdb:tt1', '1', '2026-08-11T00:00:00+00:00')",
                (rating_key,),
            )
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
            "VALUES ('h1', 'imdb:tt1', 1, ?)",
            (int(datetime(2026, 8, 10, 12, 0, tzinfo=UTC).timestamp()),),
        )
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', 'heist', "
            "'2026-08-09T12:00:00+00:00')"
        )
        conn.execute("INSERT INTO keyword_forms (surface, keyword) VALUES ('heist', 'heist')")
        conn.commit()


def test_a_current_populated_store_is_healthy_and_reports_what_is_in_it(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    _populate(path)

    report = inspect(path, tmp_path / "backups", now=NOW)

    assert report.healthy
    assert report.version == schema.SCHEMA_VERSION
    assert report.quick_check == "ok"
    assert report.counts["items"] == 2
    assert report.counts["plays"] == 1
    assert report.counts["enrichment"] == 1
    assert report.fs_identities == 1
    assert report.keyword_counts == {"tmdb": 1}
    assert report.keyword_forms_count == 1
    assert report.legacy_tmdb_keywords == 0


def test_freshness_reports_the_newest_row_of_each_kind_and_its_age(tmp_path: Path) -> None:
    """A store whose newest play is days old is a store whose ingest stopped,
    and nothing else in the report would show it."""
    path = tmp_path / "plexdb.db"
    init(path)
    _populate(path)

    report = inspect(path, tmp_path / "backups", now=NOW)
    ages = {entry.label: entry.age_days for entry in report.freshness}

    assert ages["newest play"] == pytest.approx(2.0)
    assert ages["newest enrichment"] == pytest.approx(3.0)
    assert ages["newest rating key seen"] == pytest.approx(1.5)
    assert not any(entry.stale for entry in report.freshness)


def test_a_stamp_older_than_the_window_is_flagged_on_its_own_line(tmp_path: Path) -> None:
    """An age alone only reads as wrong to someone who already knows what
    normal is, so the line says so."""
    path = tmp_path / "plexdb.db"
    init(path)
    _populate(path)

    late = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    report = inspect(path, tmp_path / "backups", now=late)

    assert all(entry.stale for entry in report.freshness)
    printed = "\n".join(render(report))
    assert "newest play: 2026-08-10T12:00:00+00:00 (10.0 days ago) — STALE" in printed
    assert "freshness (flagged over 3 days):" in printed


def test_a_timestamp_that_cannot_be_read_is_printed_rather_than_dropped(tmp_path: Path) -> None:
    """A mangled fetched_at and a table nobody has ever written to must not
    produce the same report."""
    path = tmp_path / "plexdb.db"
    init(path)
    _populate(path)
    with open_store(path) as conn:
        conn.execute("UPDATE enrichment SET fetched_at = 'yesterday, ish'")
        conn.commit()

    report = inspect(path, tmp_path / "backups", now=NOW)
    entry = next(e for e in report.freshness if e.label == "newest enrichment")

    assert entry.unreadable
    assert entry.raw == "yesterday, ish"
    assert "newest enrichment: unreadable timestamp 'yesterday, ish'" in "\n".join(render(report))


def test_rating_keys_are_ordered_as_numbers_not_as_text(tmp_path: Path) -> None:
    """Sorted as text, a duplicate holding rating keys 9 and 100 prints
    `100, 9`."""
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt2', 'movie', 'Ronin')"
        )
        for rating_key in ("100", "9"):
            conn.execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
                "VALUES (?, 'imdb:tt2', '1', '2026-08-11T00:00:00+00:00')",
                (rating_key,),
            )
        conn.commit()

    report = inspect(path, tmp_path / "backups", now=NOW)

    assert report.duplicates[0].rating_keys == ("9", "100")


def test_identities_with_more_than_one_rating_key_are_named_not_counted(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    _populate(path)

    report = inspect(path, tmp_path / "backups", now=NOW)

    assert len(report.duplicates) == 1
    assert report.duplicates[0].item_id == "imdb:tt1"
    assert report.duplicates[0].title == "Heat"
    assert report.duplicates[0].rating_keys == ("11", "12")
    assert "imdb:tt1  Heat  [11, 12]" in "\n".join(render(report))


def test_a_store_one_version_behind_is_reported_rather_than_crashing_on_it(tmp_path: Path) -> None:
    """The failure this command exists for: a walk died with `no column named
    last_seen` because the store was at v7 while the deployed code expected v8.
    A check that could only read a current store could not have said so."""
    path = tmp_path / "plexdb.db"
    behind = schema.SCHEMA_VERSION - 1
    _store_at(path, behind)

    report = inspect(path, tmp_path / "backups", now=NOW)

    assert not report.healthy
    assert report.version == behind
    assert report.sound
    assert f"store is at v{behind}" in render(report)[1]
    assert "run plexdb migrate" in render(report)[1]


def test_a_store_newer_than_this_build_says_to_upgrade_not_to_migrate(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (schema.SCHEMA_VERSION + 1,))
        conn.commit()

    report = inspect(path, tmp_path / "backups", now=NOW)

    assert not report.healthy
    assert "upgrade plex-db-ex" in render(report)[1]


def test_a_store_with_a_version_table_and_no_row_is_reported_as_damaged(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute("DELETE FROM schema_version")
        conn.commit()

    with pytest.raises(StoreError, match="damaged"):
        inspect(path, tmp_path / "backups", now=NOW)


def test_check_writes_nothing_to_the_store(tmp_path: Path) -> None:
    """The whole point: safe against the live file mid-sweep. A read-only
    connection makes a write raise, and the file's mtime and size confirm
    nothing landed."""
    path = tmp_path / "plexdb.db"
    init(path)
    _populate(path)
    before = path.stat()

    inspect(path, tmp_path / "backups", now=NOW)

    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_backups_are_counted_and_sized_and_an_absent_directory_is_not_an_empty_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plexdb.db"
    init(path)

    assert inspect(path, tmp_path / "nothing-here", now=NOW).backups is None

    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "plexdb.pre-v8.db").write_bytes(b"x" * 2048)
    (backups / "plexdb.manual-2026.db").write_bytes(b"x" * 1024)
    (backups / "notes.txt").write_text("ignored")

    found = inspect(path, backups, now=NOW).backups
    assert found is not None
    assert (found.count, found.size) == (2, 3072)


def test_keyword_counts_are_grouped_by_source_and_printed(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')"
        )
        conn.executemany(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'keywords', ?, 'keyword', ?, '2026-08-09T12:00:00+00:00')",
            [("tmdb", "heist"), ("tmdb", "crime"), ("mdblist", "heist")],
        )
        conn.executemany(
            "INSERT INTO keyword_forms (surface, keyword) VALUES (?, ?)",
            [("heist", "heist"), ("crime", "crime")],
        )
        conn.commit()

    report = inspect(path, tmp_path / "backups", now=NOW)
    printed = "\n".join(render(report))

    assert report.keyword_counts == {"mdblist": 1, "tmdb": 2}
    assert report.keyword_forms_count == 2
    assert "keywords by source:" in printed
    assert "  tmdb: 2" in printed
    assert "  mdblist: 1" in printed
    assert "keyword_forms: 2" in printed


def test_a_surviving_tmdb_keywords_row_fails_health_and_says_so(tmp_path: Path) -> None:
    """The migration that folded `tmdb_keywords` into `keywords` (ADR-0016) is
    supposed to leave none of the old namespace behind — a store that still
    carries one is not current in the way that matters, even if its schema
    version says otherwise."""
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'Heat')"
        )
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
            "VALUES ('imdb:tt1', 'tmdb_keywords', 'tmdb', 'keyword', 'heist', "
            "'2026-08-09T12:00:00+00:00')"
        )
        conn.commit()

    report = inspect(path, tmp_path / "backups", now=NOW)

    assert not report.healthy
    assert report.legacy_tmdb_keywords == 1
    assert "tmdb_keywords rows FAILED: 1" in "\n".join(render(report))


def test_the_exit_code_is_zero_when_current_and_one_when_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-zero is what makes it usable as a deploy gate."""
    path = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    monkeypatch.setenv("PLEXDB_BACKUP_DIR", str(tmp_path / "backups"))
    init(path)

    assert _cmd_check(argparse.Namespace(wait=0.0)) == 0
    assert "quick_check: ok" in capsys.readouterr().out

    with open_store(path) as conn:
        conn.execute("UPDATE schema_version SET version = 1")
        conn.commit()

    assert _cmd_check(argparse.Namespace(wait=0.0)) == 1


class _Clock:
    """A clock that only moves when `sleep` is called."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_re_reads_a_behind_store_until_its_migration_lands(tmp_path: Path) -> None:
    """A deploy's check reads the store while the container is still migrating
    it; the report it acts on must be the one taken after the migration."""
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute("UPDATE schema_version SET version = 9")
        conn.commit()
    clock = _Clock()
    looks = 0

    def look() -> Report:
        nonlocal looks
        looks += 1
        if looks == 3:
            with open_store(path) as conn:
                conn.execute("UPDATE schema_version SET version = ?", (schema.SCHEMA_VERSION,))
                conn.commit()
        return inspect(path, tmp_path / "backups", now=NOW)

    report = await_current(look, 600, sleep=clock.sleep, clock=clock)

    assert report is not None
    assert report.healthy
    assert looks == 3


def test_wait_gives_up_on_a_store_still_behind_when_it_runs_out(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    init(path)
    with open_store(path) as conn:
        conn.execute("UPDATE schema_version SET version = 9")
        conn.commit()
    clock = _Clock()

    report = await_current(
        lambda: inspect(path, tmp_path / "backups", now=NOW), 30, sleep=clock.sleep, clock=clock
    )

    assert report is not None
    assert report.version == 9
    assert not report.healthy
    assert clock.now == 30


def test_check_reads_nothing_while_a_migration_holds_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The version is committed before the migration verifies it and can still
    roll back, so a current version read mid-migration is not a pass."""
    path = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    monkeypatch.setenv("PLEXDB_BACKUP_DIR", str(tmp_path / "backups"))
    init(path)

    with _migrating(path):
        assert _cmd_check(argparse.Namespace(wait=0.0)) == 1
    assert "a migration is still running" in capsys.readouterr().out

    assert _cmd_check(argparse.Namespace(wait=0.0)) == 0


def test_idle_exits_zero_only_while_nothing_holds_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`admin host-exec` runs a writer only on exit 0. The startup migration and
    the nightly sweep both run inside `plexdb schedule`, so only the locks show them."""
    path = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    assert main(["idle"]) == 0
    assert not list(tmp_path.iterdir()), "idle created a file on an untouched store"
    init(path)

    with _migrating(path):
        assert main(["idle"]) == 1
    assert "a migration is running" in capsys.readouterr().out

    with writing(path):
        assert main(["idle"]) == 1
    assert "a writer has the store open" in capsys.readouterr().out

    assert main(["idle"]) == 0
    assert "idle" in capsys.readouterr().out


def test_idle_sees_a_writer_in_another_process(tmp_path: Path) -> None:
    """The case the lock exists for: the writer is `plexdb schedule`, the asker
    is a separate `docker exec`."""
    path = tmp_path / "plexdb.db"
    init(path)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from plexdb.store import open_store\n"
            f"with open_store(Path({str(path)!r})):\n"
            "    print('open', flush=True); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "open"
        assert held_by(path) == "a writer has the store open"
    finally:
        holder.communicate("")
    assert held_by(path) is None


def test_idle_is_not_zero_when_it_cannot_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An error must not read as idle, or the guard runs a writer blind."""
    path = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    init(path)
    lock = tmp_path / "plexdb.db.write-lock"
    lock.unlink()
    lock.mkdir()
    assert main(["idle"]) != 0
    assert "error" in capsys.readouterr().err


def test_migrate_holds_the_lock_a_check_waits_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plexdb.db"
    _store_at(path, 9)
    seen: list[bool] = []

    def apply_and_look(conn: sqlite3.Connection) -> tuple[int, int, schema.DeclaredShrinks]:
        with outside_migration(path) as clear:
            seen.append(clear)
        return real_apply(conn)

    real_apply = schema.apply
    monkeypatch.setattr(schema, "apply", apply_and_look)
    migrate(path, tmp_path / "backups")

    assert seen == [False]
    with outside_migration(path) as clear:
        assert clear


def test_a_file_that_is_not_a_database_is_reported_not_traced(tmp_path: Path) -> None:
    path = tmp_path / "plexdb.db"
    path.write_text("this is not a database")

    with pytest.raises((StoreError, sqlite3.DatabaseError)):
        inspect(path, tmp_path / "backups", now=NOW)


def test_a_path_pointing_at_a_directory_says_so_rather_than_disk_io_error(tmp_path: Path) -> None:
    """SQLite opens a directory happily and fails on the first statement with
    `disk I/O error`, which reads as a failing disk instead of a path one level
    too high."""
    with pytest.raises(StoreError, match="is a directory, not a store"):
        inspect(tmp_path, tmp_path / "backups", now=NOW)


def test_idle_runs_its_command_under_a_claim_that_locks_out_a_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check and the command are one act: while the command runs, the store
    reads busy to every other process, and the command itself is not shut out by
    the claim it runs under."""
    path = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    init(path)
    seen: list[str | None] = []

    def command(_args: argparse.Namespace) -> int:
        seen.append(held_by(path))
        with open_store(path):  # takes `writing`, which must not wait on the claim
            pass
        return 7

    argvs: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        argvs.append(list(argv))
        return command(argparse.Namespace())

    monkeypatch.setattr("plexdb.cli.main", fake_main)
    assert main(["idle", "walk", "--section", "2"]) == 7
    assert argvs == [["walk", "--section", "2"]]
    assert seen == ["a writer has the store open"]
    assert held_by(path) is None


def test_idle_with_a_command_refuses_a_busy_store_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(path))
    init(path)
    ran: list[list[str]] = []

    def record(argv: list[str]) -> int:
        ran.append(list(argv))
        return 0

    monkeypatch.setattr("plexdb.cli.main", record)

    with writing(path):
        assert main(["idle", "walk"]) == 1
    assert "a writer has the store open" in capsys.readouterr().out
    with _migrating(path):
        assert main(["idle", "walk"]) == 1
    assert "a migration is running" in capsys.readouterr().out
    assert ran == []

    with holding(path) as holder:
        assert holder is None
        with holding(path) as again:
            assert again is None  # re-entry is the same claim, not a rival
        with outside_migration(path) as clear:
            assert clear
