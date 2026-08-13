"""`plexdb.schedule` — the clock the container entrypoint runs on.

The whole reason this is Python rather than a `sleep` loop in the entrypoint is
that the arithmetic below is checkable in milliseconds instead of a day. These
are the cases a shell version would have had to be trusted on: the rollover
past midnight, the sweep that finishes inside its own scheduled minute, and a
`PLEXDB_SCHEDULE` somebody typed wrong.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from plexdb import schedule
from plexdb.errors import ConfigError


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
            iterations=1,
        )
    # Nothing touches the store when the container is misconfigured: a schedule
    # that does not parse means no sweep will ever run, so migrating the file
    # would alter a live database on the way to exiting.
    assert migrated == []


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
        iterations=1,
    )

    assert order == ["migrate", "sleep", "sweep"]


def test_schedule_is_not_itself_a_step_of_the_sweep() -> None:
    """A scheduler inside the thing it schedules would run sweeps until the
    process died. `plexdb.sweep.plan` includes a module only if it declares
    `SWEEP`; this one must not."""
    from plexdb.commands import schedule as schedule_command

    assert not hasattr(schedule_command, "SWEEP")
