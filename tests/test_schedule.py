"""`plexdb.schedule` — the clock the container entrypoint runs on.

The whole reason this is Python rather than a `sleep` loop in the entrypoint is
that the arithmetic below is checkable in milliseconds instead of a day. These
are the cases a shell version would have had to be trusted on: the rollover
past midnight, the sweep that finishes inside its own scheduled minute, and a
`PLEXDB_SCHEDULE` somebody typed wrong.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
from fixture_store import init, publish

from plexdb import schedule
from plexdb.errors import ConfigError, PlexdbError


def test_parse_schedule_reads_hours_and_minutes() -> None:
    assert schedule.parse_schedule("03:30") == (3, 30)
    assert schedule.parse_schedule(" 23:59 ") == (23, 59)
    assert schedule.parse_schedule("0:00") == (0, 0)


@pytest.mark.parametrize("raw", ["3.30", "0330", "", "24:00", "03:60", "-1:00", "aa:bb", "3:30:00"])
def test_parse_schedule_refuses_anything_it_cannot_read_exactly(raw: str) -> None:
    """`"3.30"` is the one that matters. Read leniently it becomes 03:00, and a
    store that refreshes half an hour early every night looks entirely fine."""
    with pytest.raises(ConfigError):
        schedule.parse_schedule(raw)


def test_next_fire_is_later_the_same_day_when_the_time_has_not_passed() -> None:
    now = datetime(2026, 8, 11, 1, 0)
    assert schedule.next_fire(now, (3, 30)) == datetime(2026, 8, 11, 3, 30)


def test_next_fire_rolls_to_tomorrow_once_the_time_has_passed() -> None:
    now = datetime(2026, 8, 11, 4, 0)
    assert schedule.next_fire(now, (3, 30)) == datetime(2026, 8, 12, 3, 30)


def test_next_fire_rolls_over_a_month_boundary() -> None:
    now = datetime(2026, 8, 31, 23, 59)
    assert schedule.next_fire(now, (0, 30)) == datetime(2026, 9, 1, 0, 30)


def test_next_fire_skips_a_whole_day_when_called_exactly_on_the_minute() -> None:
    """A sweep finishing inside the minute it started must not see its own
    start time as the next one. At-or-after would sleep zero and re-run."""
    now = datetime(2026, 8, 11, 3, 30)
    assert schedule.next_fire(now, (3, 30)) == datetime(2026, 8, 12, 3, 30)


def test_run_scheduler_waits_before_the_first_sweep_and_then_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first run is at the next scheduled time, never at startup — which is
    what makes "set it a few minutes out and watch" a check of the schedule."""
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    slept: list[float] = []
    ran: list[int] = []
    times = iter([datetime(2026, 8, 11, 3, 0), datetime(2026, 8, 11, 3, 30)])

    def sweep() -> int:
        ran.append(len(ran))
        return 0

    schedule.run_scheduler(
        sweep=sweep,
        sleep=slept.append,
        clock=lambda: next(times),
        migrate=lambda: None,
        extras=lambda: None,
        walk_settings=lambda: None,
        iterations=2,
    )

    assert slept == [1800.0, 86400.0]
    assert ran == [0, 1]


def test_run_scheduler_keeps_going_after_a_sweep_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero sweep leaves the store one day staler; a stopped container
    leaves it stale forever and says nothing."""
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    ran: list[int] = []

    def failing_sweep() -> int:
        ran.append(len(ran))
        return 1

    schedule.run_scheduler(
        sweep=failing_sweep,
        sleep=lambda _seconds: None,
        clock=lambda: datetime(2026, 8, 11, 3, 0),
        migrate=lambda: None,
        extras=lambda: None,
        walk_settings=lambda: None,
        iterations=3,
    )

    assert ran == [0, 1, 2]


def test_run_scheduler_refuses_to_start_with_no_schedule_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "")
    migrated: list[int] = []
    with pytest.raises(ConfigError):
        schedule.run_scheduler(
            sweep=lambda: 0,
            sleep=lambda _seconds: None,
            migrate=lambda: migrated.append(1),
            extras=lambda: None,
            walk_settings=lambda: None,
            iterations=1,
        )
    # Nothing touches the store when the container is misconfigured: a schedule
    # that does not parse means no sweep will ever run, so migrating the file
    # would alter a live database on the way to exiting.
    assert migrated == []


@pytest.mark.parametrize("blank", ["PLEX_URL", "PLEX_TOKEN", "PLEX_SOURCE_ROOTS"])
def test_run_scheduler_refuses_to_start_when_walk_cannot_read_the_library(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """A container recreated from a template with empty defaults carries the variables
    with no value. `walk` is required, so every sweep would fail and none would publish;
    the scheduler stops at startup instead, before it touches the store."""
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")
    monkeypatch.setenv("PLEX_SOURCE_ROOTS", "/media")
    monkeypatch.setenv(blank, "  ")
    migrated: list[int] = []
    with pytest.raises(ConfigError, match=blank):
        schedule.run_scheduler(
            sweep=lambda: 0,
            sleep=lambda _seconds: None,
            migrate=lambda: migrated.append(1),
            extras=lambda: None,
            iterations=1,
        )
    assert migrated == []


def test_run_scheduler_starts_when_walk_has_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")
    monkeypatch.setenv("PLEX_SOURCE_ROOTS", "/media")
    migrated: list[int] = []
    schedule.run_scheduler(
        sweep=lambda: 0,
        sleep=lambda _seconds: None,
        clock=lambda: datetime(2026, 8, 11, 3, 0),
        migrate=lambda: migrated.append(1),
        explore=lambda: None,
        frames=lambda: None,
        extras=lambda: None,
        audio=lambda: None,
        iterations=1,
    )
    assert migrated == [1]


def test_run_scheduler_migrates_the_store_before_the_first_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema is brought up to date by the code that is running, at the
    moment it starts — not by the first sweep, which can be most of a day later
    (issue #58's repair failed on a column the store did not have yet)."""
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    order: list[str] = []

    def sweep() -> int:
        order.append("sweep")
        return 0

    schedule.run_scheduler(
        sweep=sweep,
        sleep=lambda _seconds: order.append("sleep"),
        clock=lambda: datetime(2026, 8, 11, 3, 0),
        migrate=lambda: order.append("migrate"),
        extras=lambda: None,
        walk_settings=lambda: None,
        iterations=1,
    )

    assert order == ["migrate", "sleep", "sweep"]


def test_schedule_is_not_itself_a_step_of_the_sweep() -> None:
    """A scheduler inside the thing it schedules would run sweeps until the
    process died. `plexdb.sweep.plan` includes a module only if it declares
    `SWEEP`; this one must not."""
    from plexdb.commands import schedule as schedule_command

    assert not hasattr(schedule_command, "SWEEP")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_the_explorer_serves_the_snapshot_once_one_is_published_and_reports_its_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the host Plex DB-EX is a `plexdb-rs explore` child of `plexdb schedule` reading
    the snapshot. Before the first sweep publishes one it answers 503; after, it serves the
    snapshot without a restart; when it exits, the scheduler hears the exit code."""
    store = tmp_path / "plexdb.db"
    init(store)
    snapshot = tmp_path / "snapshot" / "plexdb.snapshot.db"
    snapshot.parent.mkdir()
    port = _free_port()
    monkeypatch.setenv(schedule.EXPLORE_PORT_VAR, str(port))
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("PLEXDB_EXPLORE_SAVED_PATH", str(tmp_path / "explore" / "q.json"))
    exited: list[int] = []
    heard = threading.Event()

    def on_exit(code: int) -> None:
        exited.append(code)
        heard.set()

    child = schedule._start_explorer(on_exit=on_exit)
    assert child is not None
    try:
        url = f"http://127.0.0.1:{port}/api/tags?kind=movie"
        assert _get(url)[0] == 503
        publish(store, snapshot)
        status, body = _get(url)
        assert status == 200, body
        assert json.loads(body)["titles"] == 0
        assert _get(f"http://127.0.0.1:{port}/api/extras/items")[0] == 200
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert heard.wait(timeout=10)
    assert exited == [child.returncode]


def test_the_explorer_fails_the_scheduler_when_it_cannot_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        port = taken.getsockname()[1]
        monkeypatch.setenv(schedule.EXPLORE_PORT_VAR, str(port))
        monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(tmp_path / "plexdb.snapshot.db"))
        with pytest.raises(PlexdbError, match=f"before it listened on port {port}"):
            schedule._start_explorer(on_exit=lambda code: None)


def test_the_scheduler_refuses_an_explore_port_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    monkeypatch.setenv(schedule.EXPLORE_PORT_VAR, "http")
    with pytest.raises(ConfigError, match=schedule.EXPLORE_PORT_VAR):
        schedule.run_scheduler(
            migrate=lambda: None, extras=lambda: None, walk_settings=lambda: None, iterations=0
        )


def test_the_scheduler_refuses_an_explore_port_without_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(schedule.EXPLORE_PORT_VAR, "5194")
    monkeypatch.delenv("PLEXDB_SNAPSHOT_PATH", raising=False)
    with pytest.raises(ConfigError, match="PLEXDB_SNAPSHOT_PATH"):
        schedule._start_explorer(on_exit=lambda code: None)
