"""One scheduled run: what is in it, in what order, and what a failure costs.

The order and the membership are read off the command modules themselves, so
these tests assert against `plan()` rather than a list this file also keeps —
a second list would be the drift ADR-0014 exists to stop.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from plexdb.errors import ConfigError, PlexError, TMDbError
from plexdb.sweep import PlannedStep, Status, Step, plan, run_sweep

# What a sweep does, in order. Kept here rather than derived so a renumbering
# that silently reorders the run fails a test instead of quietly changing what
# runs at 3am.
EXPECTED_SWEEP = [
    ("migrate", Step.REQUIRED),
    ("walk", Step.REQUIRED),
    ("ingest-plays", Step.REQUIRED),
    ("enrich-tautulli-plays", Step.BEST_EFFORT),
    ("enrich-tmdb-keywords", Step.BEST_EFFORT),
    ("enrich-tmdb-edges", Step.BEST_EFFORT),
    ("refresh-map", Step.BEST_EFFORT),
    ("harvest-mdblist", Step.BEST_EFFORT),
    ("publish", Step.REQUIRED),
]

# Commands that exist and are deliberately not in a sweep. `schedule` wraps a
# whole run rather than taking part in one, and `check` only reads.
NOT_IN_A_SWEEP = {"sweep", "check", "schedule", "repair-identities", "latent-users"}


def test_the_sweep_runs_these_steps_in_this_order() -> None:
    assert [(step.name, step.step) for step in plan()] == EXPECTED_SWEEP


def test_publish_runs_last_so_it_snapshots_what_the_run_produced() -> None:
    """Guards the drift ADR-0014 was written about: `publish` used to sort
    ahead of every enrichment step, which nothing caught because ORDER only
    fed `--help`."""
    assert plan()[-1].name == "publish"


def test_plays_are_banked_before_the_enrichment_block() -> None:
    """Plays are the data no re-scan reproduces; TMDB is re-fetchable. A sweep
    killed partway should already have the plays."""
    names = [step.name for step in plan()]
    assert names.index("ingest-plays") < names.index("enrich-tmdb-keywords")


def test_read_only_and_repair_commands_are_not_in_a_sweep() -> None:
    """A report nobody reads at 3am, and a repair tool that rewinds the play
    cursor, have no business on a timer."""
    assert NOT_IN_A_SWEEP.isdisjoint({step.name for step in plan()})


def _fake_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_name: str, body: str
) -> ModuleType:
    """A throwaway command package on `sys.path`.

    `package_name` is per-test on purpose: `importlib` caches by name, so two
    tests sharing one would silently get the first one's module and the second
    assertion would be meaningless.
    """
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "newcomer.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module(package_name)


def test_a_new_command_declaring_sweep_joins_with_no_other_file_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(
        tmp_path,
        monkeypatch,
        "opted_in_commands",
        'from plexdb.sweep import Step\n\nNAME = "newcomer"\nORDER = 42\n'
        "SWEEP = Step.BEST_EFFORT\n\n\ndef register(sub):\n    pass\n",
    )

    assert [step.name for step in plan(package)] == ["newcomer"]


def test_a_new_command_declaring_no_sweep_stays_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence is the default and means out, so a command cannot silently join
    a nightly run that writes to consumers."""
    package = _fake_package(
        tmp_path,
        monkeypatch,
        "opted_out_commands",
        'NAME = "newcomer"\nORDER = 42\n\n\ndef register(sub):\n    pass\n',
    )

    assert plan(package) == []


def _steps(*spec: tuple[str, Step]) -> list[PlannedStep]:
    return [PlannedStep(name=name, step=step, module=ModuleType(name)) for name, step in spec]


def test_a_required_step_failing_stops_the_run_and_publishes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []

    def invoke(name: str) -> int:
        ran.append(name)
        if name == "walk":
            raise PlexError("the Plex server could not be reached")
        return 0

    code = run_sweep(
        steps=_steps(("walk", Step.REQUIRED), ("publish", Step.REQUIRED)), invoke=invoke
    )

    assert code == 1
    assert ran == ["walk"]
    captured = capsys.readouterr()
    assert "stopped early on a required step" in captured.out
    assert "the Plex server could not be reached" in captured.err


def test_a_best_effort_step_failing_is_reported_and_the_run_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []

    def invoke(name: str) -> int:
        ran.append(name)
        if name == "enrich-tmdb-keywords":
            raise TMDbError("TMDB returned 503")
        return 0

    code = run_sweep(
        steps=_steps(("enrich-tmdb-keywords", Step.BEST_EFFORT), ("publish", Step.REQUIRED)),
        invoke=invoke,
    )

    assert code == 0
    assert ran == ["enrich-tmdb-keywords", "publish"]
    out = capsys.readouterr().out
    assert "failed, continuing: TMDB returned 503" in out
    assert "failed 1: enrich-tmdb-keywords" in out
    assert "every step reached" in out


def test_a_best_effort_step_with_no_credentials_is_skipped_not_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A source nobody has set up must not log an error every night forever.
    `ConfigError` already means "a setting is missing" and nothing else does,
    so the sweep needs no extra declaration to tell them apart."""

    def invoke(name: str) -> int:
        if name == "harvest-mdblist":
            raise ConfigError("MDBLIST_API_KEY must be set in .env to harvest MDBList collections")
        return 0

    code = run_sweep(
        steps=_steps(("harvest-mdblist", Step.BEST_EFFORT), ("publish", Step.REQUIRED)),
        invoke=invoke,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "skipped: MDBLIST_API_KEY" in out
    assert "skipped 1 (not configured): harvest-mdblist" in out
    assert "failed" not in out


def test_a_required_step_with_no_credentials_still_stops_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Being unconfigured excuses an optional source, not a required one —
    `publish` with no snapshot path has nowhere to write."""

    def invoke(name: str) -> int:
        raise ConfigError("PLEXDB_SNAPSHOT_PATH is not set")

    assert run_sweep(steps=_steps(("publish", Step.REQUIRED)), invoke=invoke) == 1
    assert "stopped early on a required step" in capsys.readouterr().out


def test_an_unexpected_exception_in_a_best_effort_step_does_not_end_the_sweep(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure policy must not depend on every client's error discipline
    staying in sync with one tuple of exception types. It already did not:
    `tmdb_client.py` calls `resp.json()` outside its own `try`, so a 200
    carrying a proxy error page raises `JSONDecodeError` — not a `PlexdbError`,
    not an `OSError`, not a `sqlite3.Error` — and would have killed a 3am run
    before `publish`.
    """
    ran: list[str] = []

    def invoke(name: str) -> int:
        ran.append(name)
        if name == "enrich-tmdb-keywords":
            raise json.JSONDecodeError("Expecting value", "<!DOCTYPE html>", 0)
        return 0

    code = run_sweep(
        steps=_steps(("enrich-tmdb-keywords", Step.BEST_EFFORT), ("publish", Step.REQUIRED)),
        invoke=invoke,
    )

    assert code == 0
    assert ran == ["enrich-tmdb-keywords", "publish"]
    out = capsys.readouterr().out
    # The type name is in the message: this arm also catches real bugs in our
    # own code, and "failed, continuing" must not be their only trace.
    assert "failed, continuing: JSONDecodeError" in out
    assert "every step reached" in out


def test_an_unexpected_exception_in_a_required_step_is_not_swallowed() -> None:
    """A required step's failure ends the run either way, so there is nothing
    to gain by catching a bug here — and a traceback is how this project says
    "this is a bug, not a bad input" (`plexdb/errors.py`)."""

    def invoke(name: str) -> int:
        raise ZeroDivisionError("a real bug in our own code")

    with pytest.raises(ZeroDivisionError):
        run_sweep(steps=_steps(("walk", Step.REQUIRED)), invoke=invoke)


def test_a_required_failure_reports_on_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`cli.main` puts `error:` on stderr; a fatal sweep failure on the success
    channel is how a log reader misses it."""

    def invoke(name: str) -> int:
        raise PlexError("the Plex server could not be reached")

    assert run_sweep(steps=_steps(("walk", Step.REQUIRED)), invoke=invoke) == 1

    captured = capsys.readouterr()
    assert "error: walk:" in captured.err
    assert "error: walk:" not in captured.out


def test_a_clean_run_reports_every_step_it_ran(capsys: pytest.CaptureFixture[str]) -> None:
    code = run_sweep(
        steps=_steps(("walk", Step.REQUIRED), ("publish", Step.REQUIRED)), invoke=lambda _name: 0
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "ran 2: walk, publish" in out
    assert "every step reached" in out
    assert Status.SKIPPED.value not in out
