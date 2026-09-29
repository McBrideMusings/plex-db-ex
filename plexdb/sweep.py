"""One scheduled run of the writer: every step in order, ending in a snapshot.

The order is each command module's own `ORDER`, and whether a step's failure
ends the run is each command module's own `SWEEP`. Nothing here holds a list of
command names — a module that declares `SWEEP` is in the sweep, and one that
does not is out (ADR-0014).

The distinction between a source that is *not configured* and one that is *not
reachable* is already in the exception taxonomy: `ConfigError` means a setting
is missing, everything else under `PlexdbError` means the thing itself failed.
A best-effort step that raises the first is reported as skipped, so a source
nobody has set up does not log an error every night forever.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import ModuleType

from .commands import iter_command_modules
from .errors import ConfigError, PlexdbError

__all__ = ["Outcome", "PlannedStep", "Status", "Step", "plan", "run_sweep"]


class Step(Enum):
    """What a command's failure means to the sweep it is part of."""

    #: Its failure ends the sweep. Nothing after it runs and nothing is
    #: published — a snapshot built on a half-read library is worse than
    #: yesterday's complete one.
    REQUIRED = "required"
    #: Its failure is reported and the sweep continues. Every optional source
    #: sits behind an adapter (ADR-0004); a source allowed to be absent is a
    #: source allowed to fail.
    BEST_EFFORT = "best-effort"


class Status(Enum):
    """How one step of a sweep turned out."""

    RAN = "ran"
    #: A best-effort step whose configuration is absent. Not a failure — the
    #: source was never set up.
    SKIPPED = "skipped"
    #: A best-effort step that was configured and did not work.
    FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    name: str
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class PlannedStep:
    name: str
    step: Step
    module: ModuleType


def plan(package: ModuleType | None = None) -> list[PlannedStep]:
    """Every command taking part in a sweep, in the order it runs.

    A module declares `SWEEP` to opt in. Absence is the default and means out,
    so a new command cannot silently join a nightly run that writes to
    consumers.
    """
    planned = [
        PlannedStep(name=module.NAME, step=module.SWEEP, module=module)
        for module in iter_command_modules(package)
        if hasattr(module, "SWEEP")
    ]
    return sorted(planned, key=lambda step: (step.module.ORDER, step.module.__name__))


def _run_through_the_cli() -> Callable[[str], int]:
    """Run a step the way a person would: `plexdb <name>`, with every flag at
    its declared default. Building the parser once means argparse fills those
    defaults, so the sweep never has to know a command's options."""
    from .cli import build_parser

    parser = build_parser()

    def invoke(name: str) -> int:
        args = parser.parse_args([name])
        return int(args.func(args))

    return invoke


def run_locked_sweep() -> int:
    """The real sweep, holding the writer lock from its first step to its last,
    so `plexdb idle` reads busy between steps as well as during them."""
    from .config import Config
    from .store import writing

    with writing(Config.from_env().store_path):
        return run_sweep()


def run_sweep(
    *, steps: list[PlannedStep] | None = None, invoke: Callable[[str], int] | None = None
) -> int:
    """Run every planned step in order. Returns the process exit code.

    Non-zero means the sweep stopped early on a required step. A run where an
    optional source failed still returns zero — it published, and the summary
    says what was missing.
    """
    steps = plan() if steps is None else steps
    invoke = _run_through_the_cli() if invoke is None else invoke
    outcomes: list[Outcome] = []

    for planned in steps:
        print(f"== {planned.name} ==", flush=True)
        try:
            code = invoke(planned.name)
        except ConfigError as err:
            if planned.step is Step.REQUIRED:
                return _stop(planned.name, err, outcomes)
            print(f"skipped: {err}", flush=True)
            outcomes.append(Outcome(planned.name, Status.SKIPPED, str(err)))
        except (PlexdbError, OSError, sqlite3.Error) as err:
            if planned.step is Step.REQUIRED:
                return _stop(planned.name, err, outcomes)
            print(f"failed, continuing: {err}", flush=True)
            outcomes.append(Outcome(planned.name, Status.FAILED, str(err)))
        except Exception as err:  # noqa: BLE001 — see below
            # A best-effort step must not be able to end the sweep, and making
            # that true only for the exception types listed above means it
            # depends on every client's error discipline staying in sync with
            # one tuple in this file. It already does not: `tmdb_client.py`
            # calls `resp.json()` outside its own `try`, so a 200 carrying a
            # proxy error page raises `JSONDecodeError` — none of the three
            # types above — and would kill a 3am run before `publish`.
            #
            # The type name goes in the message because this arm also catches
            # real bugs in our own code, and "failed, continuing" must not be
            # the only trace one leaves.
            if planned.step is Step.REQUIRED:
                raise
            print(f"failed, continuing: {type(err).__name__}: {err}", flush=True)
            outcomes.append(Outcome(planned.name, Status.FAILED, f"{type(err).__name__}: {err}"))
        else:
            if code != 0 and planned.step is Step.REQUIRED:
                return _stop(planned.name, f"exit code {code}", outcomes)
            if code != 0:
                print(f"failed, continuing: exit code {code}", flush=True)
                outcomes.append(Outcome(planned.name, Status.FAILED, f"exit code {code}"))
            else:
                outcomes.append(Outcome(planned.name, Status.RAN))

    _summarise(outcomes, completed=True)
    return 0


def _stop(name: str, err: object, outcomes: list[Outcome]) -> int:
    """A required step failed. Report what had already run and stop.

    The message goes to stderr, matching `cli.main` — a fatal failure on the
    success channel is how a log reader misses it.
    """
    print(f"error: {name}: {err}", file=sys.stderr, flush=True)
    outcomes.append(Outcome(name, Status.FAILED, str(err)))
    _summarise(outcomes, completed=False)
    return 1


def _summarise(outcomes: list[Outcome], *, completed: bool) -> None:
    """The last thing a sweep prints, and the only part of the log a nightly
    run is read by.

    The closing line reports whether every step was reached, not whether a
    snapshot exists — the loop cannot know that `publish` wrote one, only that
    it returned zero, and claiming otherwise would be a sentence that stops
    being true the moment `publish` is no longer the last required step.
    """
    ran = [o.name for o in outcomes if o.status is Status.RAN]
    skipped = [o.name for o in outcomes if o.status is Status.SKIPPED]
    failed = [o.name for o in outcomes if o.status is Status.FAILED]
    print("== sweep ==", flush=True)
    print(f"ran {len(ran)}: {', '.join(ran) if ran else 'nothing'}", flush=True)
    if skipped:
        print(f"skipped {len(skipped)} (not configured): {', '.join(skipped)}", flush=True)
    if failed:
        print(f"failed {len(failed)}: {', '.join(failed)}", flush=True)
    print("every step reached" if completed else "stopped early on a required step", flush=True)
