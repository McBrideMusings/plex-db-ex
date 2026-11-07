"""When `plexdb schedule` runs `plexdb-rs harvest-audio` (plex-db-ex-9ak.1).

The harvest itself is Rust: it analyzes the files of the libraries `PLEXDB_AUDIO_LIBRARIES`
names (Plex section titles and extras libraries, in work order), once per file hash, and
takes the store's writer lock only to write each result, so it needs no window and never
waits on a sweep. This module holds only *when*: every `PLEXDB_AUDIO_EVERY` minutes
(default 60; `0` turns it off). With no library named it is off. A run refused because
another harvest is running (exit 75) is tried again at the next interval, as is a failed one.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from .errors import ConfigError, PlexdbError

__all__ = ["EVERY_VAR", "LIBRARIES_VAR", "parse_every", "run_loop", "start", "tick_once"]

EVERY_VAR = "PLEXDB_AUDIO_EVERY"
LIBRARIES_VAR = "PLEXDB_AUDIO_LIBRARIES"
DEFAULT_EVERY_MINUTES = 60
_COMMAND = "harvest-audio"


def parse_every(raw: str) -> int:
    """`PLEXDB_AUDIO_EVERY` as whole minutes: unset is 60, `0` is off."""
    text = raw.strip()
    if not text:
        return DEFAULT_EVERY_MINUTES
    if not text.isdigit():
        raise ConfigError(f"{EVERY_VAR} must be a whole number of minutes, got {raw!r}")
    return int(text)


def tick_once(log: Callable[[str], None]) -> str:
    """Run one harvest through `plexdb-rs`. Returns `ran`, `busy` or `failed`."""
    from . import rust

    try:
        rust.run([_COMMAND])
    except rust.RustStepBusy as err:
        log(f"audio harvest busy: {err}")
        return "busy"
    except (PlexdbError, OSError) as err:
        log(f"audio harvest failed: {err}")
        return "failed"
    return "ran"


def run_loop(
    *,
    every_minutes: int,
    harvest: Callable[[], str],
    sleep: Callable[[float], None] = time.sleep,
    iterations: int | None = None,
) -> None:
    """Harvest, sleep `every_minutes`, repeat, forever or `iterations` times. Whatever a
    run returns (`ran`, `busy`, `failed`), the next one is a full interval away."""
    done = 0
    while iterations is None or done < iterations:
        harvest()
        done += 1
        if iterations is not None and done >= iterations:
            return
        sleep(every_minutes * 60)


def start() -> bool:
    """Start the loop on a daemon thread when it is configured; say on or off and why.
    Returns whether it started."""
    libraries = [s.strip() for s in os.environ.get(LIBRARIES_VAR, "").split(",") if s.strip()]
    if not libraries:
        print(f"audio harvester off: {LIBRARIES_VAR} unset", flush=True)
        return False
    every = parse_every(os.environ.get(EVERY_VAR, ""))
    if every == 0:
        print(f"audio harvester off: {EVERY_VAR}=0", flush=True)
        return False
    thread = threading.Thread(
        target=run_loop,
        kwargs={
            "every_minutes": every,
            "harvest": lambda: tick_once(lambda line: print(line, flush=True)),
        },
        name="audio-harvester",
        daemon=True,
    )
    thread.start()
    print(
        f"audio harvester on, libraries {', '.join(libraries)}, every {every} minute(s)",
        flush=True,
    )
    return True
