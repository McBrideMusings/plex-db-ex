"""When the audio harvest runs (plex-db-ex-9ak.1). The harvest itself is Rust; this is the clock."""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from plexdb import audio_tick, rust, schedule
from plexdb.errors import ConfigError, PlexdbError


def test_every_defaults_to_an_hour_and_zero_turns_it_off() -> None:
    assert audio_tick.parse_every("") == 60
    assert audio_tick.parse_every(" 15 ") == 15
    assert audio_tick.parse_every("0") == 0
    with pytest.raises(ConfigError):
        audio_tick.parse_every("hourly")


def test_the_loop_harvests_then_sleeps_one_interval_between_runs() -> None:
    slept: list[float] = []
    runs: list[int] = []

    def harvest() -> str:
        runs.append(len(runs))
        return "ran"

    audio_tick.run_loop(every_minutes=60, harvest=harvest, sleep=slept.append, iterations=3)

    assert runs == [0, 1, 2]
    assert slept == [3600.0, 3600.0]


def test_a_busy_or_failed_run_is_tried_again_a_full_interval_later() -> None:
    outcomes = iter(["busy", "failed", "ran"])
    slept: list[float] = []

    audio_tick.run_loop(
        every_minutes=5, harvest=lambda: next(outcomes), sleep=slept.append, iterations=3
    )

    assert slept == [300.0, 300.0]


def test_tick_once_reports_busy_failed_and_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    results: list[Exception | None] = [
        rust.RustStepBusy("another audio harvest is running"),
        PlexdbError("boom"),
        None,
    ]

    def fake_run(args: list[str]) -> int:
        calls.append(list(args))
        result = results.pop(0)
        if result is not None:
            raise result
        return 0

    monkeypatch.setattr(rust, "run", fake_run)
    lines: list[str] = []

    assert audio_tick.tick_once(lines.append) == "busy"
    assert audio_tick.tick_once(lines.append) == "failed"
    assert audio_tick.tick_once(lines.append) == "ran"
    assert calls == [["harvest-audio"]] * 3
    assert "another audio harvest is running" in lines[0] and "boom" in lines[1]


def test_it_is_off_without_sections_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(audio_tick.SECTIONS_VAR, raising=False)

    assert audio_tick.start() is False
    assert "audio harvester off: PLEXDB_AUDIO_SECTIONS unset" in capsys.readouterr().out


def test_it_is_off_when_every_is_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(audio_tick.SECTIONS_VAR, "1")
    monkeypatch.setenv(audio_tick.EVERY_VAR, "0")

    assert audio_tick.start() is False
    assert "audio harvester off: PLEXDB_AUDIO_EVERY=0" in capsys.readouterr().out


def test_it_starts_a_daemon_thread_when_sections_are_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(audio_tick.SECTIONS_VAR, "1, 3")
    monkeypatch.setenv(audio_tick.EVERY_VAR, "30")
    started: list[tuple[str, bool]] = []

    class FakeThread:
        def __init__(
            self, *, target: object, kwargs: dict[str, object], name: str, daemon: bool
        ) -> None:
            assert kwargs["every_minutes"] == 30
            started.append((name, daemon))

        def start(self) -> None:
            started.append(("start", True))

    monkeypatch.setattr(threading, "Thread", FakeThread)

    assert audio_tick.start() is True
    assert started == [("audio-harvester", True), ("start", True)]
    assert "audio harvester on, libraries 1, 3, every 30 minute(s)" in capsys.readouterr().out


def test_run_scheduler_starts_the_audio_harvester_beside_the_extras_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(schedule.SCHEDULE_VAR, "03:30")
    order: list[str] = []

    schedule.run_scheduler(
        sweep=lambda: 0,
        sleep=lambda _seconds: None,
        clock=lambda: datetime(2026, 8, 11, 3, 0),
        migrate=lambda: None,
        explore=lambda: None,
        frames=lambda: None,
        extras=lambda: order.append("extras"),
        audio=lambda: order.append("audio"),
        walk_settings=lambda: None,
        iterations=1,
    )

    assert order == ["extras", "audio"]
