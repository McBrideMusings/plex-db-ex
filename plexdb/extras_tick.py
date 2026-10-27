"""The extras tick: when `plexdb schedule` runs `plexdb-rs extras-tick` (ADR-0030).

The tick itself is Rust: it folds `extras.json`, walks every declared library and holds
the store as its only writer, refusing (exit 75) while a sweep or another step has it open.
This module holds only *when*: every `PLEXDB_EXTRAS_EVERY` minutes (default 10; `0` turns
the tick off), and within a minute of `extra_decisions.json` changing, polled every 30
seconds. A refused tick is tried again at the next poll; a failed one waits for the next
interval, so a broken `extras.json` is reported every ten minutes, not every thirty seconds.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .errors import ConfigError, PlexdbError

__all__ = ["DECISIONS_FILE", "EVERY_VAR", "POLL_SECONDS", "parse_every", "run_ticks", "start"]

EVERY_VAR = "PLEXDB_EXTRAS_EVERY"
DEFAULT_EVERY_MINUTES = 10
DECISIONS_FILE = "extra_decisions.json"
#: How often the loop looks at the clock and at `extra_decisions.json`; under the
#: minute a decision may wait.
POLL_SECONDS = 30.0


def parse_every(raw: str) -> int:
    """`PLEXDB_EXTRAS_EVERY` as whole minutes: unset is 10, `0` is off."""
    text = raw.strip()
    if not text:
        return DEFAULT_EVERY_MINUTES
    if not text.isdigit():
        raise ConfigError(f"{EVERY_VAR} must be a whole number of minutes, got {raw!r}")
    return int(text)


def decisions_path() -> Path:
    """Where Plex DB-EX writes `extra_decisions.json`: beside its saved queries, else the
    snapshot, else the store (`plexdb_store::Config::decisions_dir`)."""
    saved = os.environ.get("PLEXDB_EXPLORE_SAVED_PATH", "").strip()
    if saved:
        return Path(saved).expanduser().parent / DECISIONS_FILE
    config = Config.from_env()
    return (config.snapshot_path or config.store_path).parent / DECISIONS_FILE


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def tick_once(log: Callable[[str], None]) -> str:
    """Run one tick through `plexdb-rs`. Returns `ran`, `refused` or `failed`."""
    from . import rust

    try:
        rust.run([_TICK])
    except rust.RustStepBusy as err:
        log(f"extras tick refused: {err}")
        return "refused"
    except (PlexdbError, OSError) as err:
        log(f"extras tick failed: {err}")
        return "failed"
    return "ran"


_TICK = "extras-tick"


def run_ticks(
    *,
    every_minutes: int,
    decisions: Path,
    tick: Callable[[], str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    iterations: int | None = None,
) -> None:
    """Poll forever (or `iterations` times): tick when `every_minutes` have passed since
    the last tick that ran or failed, or when `decisions` changed since the last tick that
    ran. The first poll ticks. A refused tick leaves both conditions standing."""
    last_tick: float | None = None
    seen = _mtime(decisions)
    polls = 0
    while iterations is None or polls < iterations:
        sleep(POLL_SECONDS)
        polls += 1
        now = clock()
        changed = _mtime(decisions) != seen
        due = last_tick is None or now - last_tick >= every_minutes * 60
        if not (due or changed):
            continue
        before = _mtime(decisions)
        outcome = tick()
        if outcome == "refused":
            continue
        last_tick = now
        if outcome == "ran":
            seen = before


def start() -> None:
    """Start the tick loop on a daemon thread, unless `PLEXDB_EXTRAS_EVERY` is `0`."""
    every = parse_every(os.environ.get(EVERY_VAR, ""))
    if every == 0:
        print(f"extras tick off: {EVERY_VAR}=0", flush=True)
        return
    decisions = decisions_path()
    thread = threading.Thread(
        target=run_ticks,
        kwargs={
            "every_minutes": every,
            "decisions": decisions,
            "tick": lambda: tick_once(lambda line: print(line, flush=True)),
        },
        name="extras-tick",
        daemon=True,
    )
    thread.start()
    print(f"extras tick on, every {every} minute(s), watching {decisions}", flush=True)
