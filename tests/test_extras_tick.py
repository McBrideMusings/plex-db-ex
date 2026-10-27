"""When the extras tick runs (ADR-0030). The tick itself is Rust; this is the clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from plexdb import extras_tick, rust
from plexdb.errors import ConfigError


def test_every_defaults_to_ten_minutes_and_zero_turns_it_off() -> None:
    assert extras_tick.parse_every("") == 10
    assert extras_tick.parse_every(" 3 ") == 3
    assert extras_tick.parse_every("0") == 0
    with pytest.raises(ConfigError):
        extras_tick.parse_every("ten")


def _run(
    tmp_path: Path, outcomes: list[str], polls: int, every: int = 10
) -> tuple[list[float], Path]:
    """Poll `polls` times with a clock that advances one poll per sleep; return the times
    a tick was attempted."""
    decisions = tmp_path / "extra_decisions.json"
    now = [0.0]
    attempts: list[float] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def tick() -> str:
        attempts.append(now[0])
        return outcomes.pop(0) if outcomes else "ran"

    extras_tick.run_ticks(
        every_minutes=every,
        decisions=decisions,
        tick=tick,
        sleep=sleep,
        clock=lambda: now[0],
        iterations=polls,
    )
    return attempts, decisions


def test_it_ticks_on_the_first_poll_and_then_every_interval(tmp_path: Path) -> None:
    attempts, _ = _run(tmp_path, [], polls=45)
    # 30 s polls: ticks at 30 s, then 600 s later (poll 21), then 600 s after that (poll 41).
    assert attempts == [30.0, 630.0, 1230.0]


def test_a_refused_tick_is_tried_again_at_the_next_poll(tmp_path: Path) -> None:
    attempts, _ = _run(tmp_path, ["refused", "refused", "ran"], polls=5)
    assert attempts == [30.0, 60.0, 90.0]


def test_a_failed_tick_waits_for_the_next_interval(tmp_path: Path) -> None:
    attempts, _ = _run(tmp_path, ["failed"], polls=25)
    assert attempts == [30.0, 630.0]


def test_a_changed_decisions_file_ticks_within_a_poll(tmp_path: Path) -> None:
    decisions = tmp_path / "extra_decisions.json"
    now = [0.0]
    attempts: list[float] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] == 90.0:
            decisions.write_text("[]")

    def tick() -> str:
        attempts.append(now[0])
        return "ran"

    extras_tick.run_ticks(
        every_minutes=10,
        decisions=decisions,
        tick=tick,
        sleep=sleep,
        clock=lambda: now[0],
        iterations=6,
    )
    assert attempts == [30.0, 90.0]


def test_a_busy_exit_from_the_binary_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(_args: object) -> int:
        raise rust.RustStepBusy("a writer has the store open")

    monkeypatch.setattr(rust, "run", busy)
    lines: list[str] = []
    assert extras_tick.tick_once(lines.append) == "refused"
    assert lines == ["extras tick refused: a writer has the store open"]


def test_the_binary_reports_exit_75_as_busy_and_78_as_not_configured() -> None:
    with pytest.raises(rust.RustStepBusy):
        rust._check(rust.BUSY, "error: a writer has the store open")
    with pytest.raises(ConfigError):
        rust._check(rust.NOT_CONFIGURED, "error: unset")


def test_decisions_sit_beside_the_saved_queries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLEXDB_EXPLORE_SAVED_PATH", str(tmp_path / "saved" / "queries.json"))
    assert extras_tick.decisions_path() == tmp_path / "saved" / "extra_decisions.json"
    monkeypatch.delenv("PLEXDB_EXPLORE_SAVED_PATH")
    monkeypatch.setenv("PLEXDB_PATH", str(tmp_path / "plexdb.db"))
    monkeypatch.delenv("PLEXDB_SNAPSHOT_PATH", raising=False)
    assert extras_tick.decisions_path() == tmp_path / "extra_decisions.json"
