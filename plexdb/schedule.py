"""A clock, and nothing else.

`plexdb sweep` already owns which steps run and what a step's failure means
(ADR-0014). The only thing missing between a Mac checkout and an unattended
host is *when* it starts. That is what this module holds: a wall-clock time of
day, the arithmetic for the next occurrence of it, and a loop that waits and
calls `run_locked_sweep`.

**Why this is Python and not three lines of shell in the entrypoint.** Issue
#45 removed the run order from a shell script for a reason that applies here
word for word: a shell script cannot check itself. `next_fire` below is the
whole of the scheduling logic and it is a pure function of `(now, at)`, so a
test pins down midnight rollover, the exact-match case, and a malformed
`PLEXDB_SCHEDULE` without a container, a clock change, or a day of waiting.
A `sleep` loop in `bash` computing the same thing would be checkable only by
running it.

**What this deliberately does not hold.** No list of steps, no failure policy
for a step, no order — those stay in `sweep.py` where a module declares its own
`SWEEP` and `ORDER`. The one judgement made here is what follows a sweep that
returned non-zero: the loop goes on, and the next attempt comes `RETRY_AFTER`
later instead of at the next scheduled time, never later than that time. A
required step fails when something outside the store is down — Plex hung, a
source timed out — and the outage is usually over long before the next day's
run. That is a property of the schedule, not of any step.

The time is read in the process's local zone, so `TZ` in the container's
environment is what decides when "03:30" is. A container with no `TZ` runs UTC.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from .errors import ConfigError, PlexdbError

__all__ = [
    "RETRY_AFTER",
    "SCHEDULE_VAR",
    "next_attempt",
    "next_fire",
    "parse_schedule",
    "run_scheduler",
]

#: The environment variable carrying the daily run time, `HH:MM`, 24-hour.
SCHEDULE_VAR = "PLEXDB_SCHEDULE"

#: How long after a failed sweep the next attempt starts.
RETRY_AFTER = timedelta(hours=1)


def parse_schedule(raw: str) -> tuple[int, int]:
    """`"03:30"` -> `(3, 30)`.

    Strict on purpose. A schedule that half-parses is worse than one that
    refuses: `"3.30"` silently read as 03:00 would run at the wrong hour every
    night and look like it worked.
    """
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ConfigError(f"{SCHEDULE_VAR} must be HH:MM in 24-hour time, got {raw!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ConfigError(f"{SCHEDULE_VAR} must be HH:MM in 24-hour time, got {raw!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{SCHEDULE_VAR} must be HH:MM in 24-hour time, got {raw!r}")
    return hour, minute


def next_fire(now: datetime, at: tuple[int, int]) -> datetime:
    """The next moment matching `at`, strictly after `now`.

    Strictly after, not at-or-after: a sweep that finishes inside the same
    minute it started would otherwise see a "next" fire time it has already
    passed, sleep zero seconds, and run again immediately — a loop, not a
    schedule.
    """
    hour, minute = at
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_attempt(now: datetime, at: tuple[int, int], *, failed: bool) -> tuple[datetime, bool]:
    """When the next sweep starts, and whether it is a retry of a failed one.

    After a success, or before the first sweep, it is `next_fire`. After a
    failure it is `RETRY_AFTER` from `now`, unless the scheduled time comes
    first: then that is the next attempt, and it is not a retry but the day's
    own run.
    """
    scheduled = next_fire(now, at)
    if failed and now + RETRY_AFTER < scheduled:
        return now + RETRY_AFTER, True
    return scheduled, False


#: The settings the sweep's required `walk` step cannot run without
#: (`crates/plexdb/src/sources.rs`, `crates/plexdb/src/steps.rs` `walk`).
WALK_SETTINGS = ("PLEX_URL", "PLEX_TOKEN", "PLEX_SOURCE_ROOTS")


def require_walk_settings() -> None:
    """Raise `ConfigError` naming every one of `WALK_SETTINGS` that is unset or blank.

    A blank value counts as unset, as it does for `walk` itself: a container recreated
    from a template whose defaults are empty strings carries the variables with no value.
    """
    missing = [name for name in WALK_SETTINGS if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(
            f"{', '.join(missing)} empty or unset: the sweep's required walk step cannot "
            "read the library, so no sweep would ever publish a snapshot. Recreate the "
            "container with these set (admin deploy)."
        )


def _migrate_store() -> None:
    """Bring the store to the schema this build expects, and say what happened.

    `plexdb-rs migrate` prints the backup it took, the version change and the
    guarded row counts. Deliberately not wrapped: a store that fails to migrate
    has already been rolled back to the copy the migration took, and the right
    thing then is to take the container down loudly rather than sweep a store at
    a schema this code does not match. Under `restart: unless-stopped` that is a
    restart loop with the reason in `docker logs`, which is louder than a sweep
    writing against the wrong shape.
    """
    from . import rust
    from .config import Config
    from .frames import migrate_configured

    config = Config.from_env()
    rust.run(["migrate"], config)
    # The Frame index is optional (ADR-0024), so its failure is printed and the
    # container stays up to sweep the store; `plexdb check`, which a deploy
    # runs last, fails on a frames.db left damaged or behind.
    try:
        print(migrate_configured(config), flush=True)
    except (PlexdbError, OSError, sqlite3.Error) as err:
        print(f"frames.db FAILED to migrate: {err}", flush=True)


#: The port `plexdb schedule` serves Plex DB-EX on; unset serves nothing.
EXPLORE_PORT_VAR = "PLEXDB_EXPLORE_PORT"


def _stop_container(code: int) -> None:
    """End the whole process with the explorer's exit code (1 if it exited 0), so the
    container's restart policy starts it again."""
    os._exit(code or 1)


def _start_explorer(on_exit: Callable[[int], None] | None = None) -> subprocess.Popen[str] | None:
    """Serve Plex DB-EX over the published snapshot, if asked to.

    Off unless `PLEXDB_EXPLORE_PORT` is set. It runs `plexdb-rs explore` as a child process
    reading the snapshot, never the live store: ADR-0007 is where readers look, and the
    snapshot is a file the writer only ever replaces by rename, so a request cannot see it
    half-written. Before the first sweep publishes one, every request answers 503. Bound on
    every interface inside the container; which host address the port reaches is
    `[[docker_run.ports]]`'s decision, not this process's.

    The child inherits this process's environment, so the saved queries and decision files go
    where `PLEXDB_EXPLORE_SAVED_PATH` puts them and `/api/poster` uses this same
    `PLEX_URL`/`PLEX_TOKEN` (docs/adr/0017).

    It returns once the child prints the address it listens on, so a port it cannot bind
    fails the scheduler at startup. If the child exits later, `on_exit` gets its exit code;
    the default ends this process, because a container that sweeps with no explorer looks
    healthy and is not.
    """
    from . import rust
    from .config import Config

    raw = os.environ.get(EXPLORE_PORT_VAR, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"{EXPLORE_PORT_VAR} must be a port number, got {raw!r}")
    cfg = Config.from_env()
    snapshot = cfg.snapshot_path
    if snapshot is None:
        raise ConfigError(
            f"{EXPLORE_PORT_VAR} is set but PLEXDB_SNAPSHOT_PATH is not; "
            "Plex DB-EX reads the published snapshot"
        )
    child = subprocess.Popen(
        [rust.binary(cfg), "explore", "--host", "0.0.0.0", "--port", raw, "--store", str(snapshot)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    listening = child.stdout.readline()
    if not listening:
        raise PlexdbError(f"Plex DB-EX exited {child.wait()} before it listened on port {raw}")
    print(listening, end="", flush=True)
    stop = _stop_container if on_exit is None else on_exit

    def watch() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            print(line, end="", flush=True)
        code = child.wait()
        print(f"Plex DB-EX exited {code}; stopping so the container restarts", flush=True)
        stop(code)

    threading.Thread(target=watch, name="explore", daemon=True).start()
    return child


def _start_frame_harvester() -> None:
    """Start the frame harvester's window loop on a thread, if it is configured.

    It needs `PLEXDB_FRAMES_DIR`, `PLEXDB_FRAME_HOURS` and `PLEXDB_FRAME_ENCODER_URL`;
    any one unset leaves it off and says which. The thread is not a Step of any
    Sweep and writes `frames.db` alone, so the sweep and the harvest never contend
    for a file (ADR-0024).
    """
    import threading

    from .config import Config
    from .frame_harvest import harvest, run_in_windows
    from .frames import frames_path, parse_hours

    config = Config.from_env()
    missing = [
        name
        for name, value in (
            ("PLEXDB_FRAMES_DIR", config.frames_dir),
            ("PLEXDB_FRAME_HOURS", config.frame_hours),
            ("PLEXDB_FRAME_ENCODER_URL", config.frame_encoder_url),
        )
        if not value
    ]
    if missing:
        print(f"frame harvester off: {', '.join(missing)} unset", flush=True)
        return
    parse_hours(config.frame_hours)
    frames_path(config)

    def run_once() -> str:
        return harvest(config, lambda line: print(line, flush=True))

    thread = threading.Thread(
        target=run_in_windows,
        args=(config, run_once),
        kwargs={
            "clock": lambda: datetime.now().astimezone(),
            "log": lambda line: print(line, flush=True),
        },
        name="frame-harvester",
        daemon=True,
    )
    thread.start()
    print(f"frame harvester on, hours {config.frame_hours}", flush=True)


def _start_extras_ticks() -> None:
    """Start the extras tick (`plexdb/extras_tick.py`): `extras-tick` every
    `PLEXDB_EXTRAS_EVERY` minutes, and within a minute of `extra_decisions.json`
    changing. It runs as the store's only writer and is refused while a sweep holds the
    store, so it never overlaps one."""
    from . import extras_tick

    extras_tick.start()


def _start_audio_harvester() -> None:
    """Start the audio harvester's interval loop (`plexdb/audio_tick.py`): `harvest-audio`
    every `PLEXDB_AUDIO_EVERY` minutes, off unless `PLEXDB_AUDIO_LIBRARIES` names libraries.
    It holds the store's writer lock only to write each result, so it never holds up a
    sweep."""
    from . import audio_tick

    audio_tick.start()


def run_scheduler(
    *,
    sweep: Callable[[], int] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    migrate: Callable[[], None] | None = None,
    explore: Callable[[], object] | None = None,
    frames: Callable[[], None] | None = None,
    extras: Callable[[], None] | None = None,
    audio: Callable[[], None] | None = None,
    walk_settings: Callable[[], None] | None = None,
    iterations: int | None = None,
) -> int:
    """Migrate the store, then wait until the next scheduled time, run one sweep,
    repeat.

    Runs forever under a container. `iterations` bounds the loop so a test can
    watch it complete a fixed number of cycles; `sweep`, `sleep`, `clock`,
    `migrate`, `explore`, `frames`, `extras`, `audio` and `walk_settings` are injectable for
    the same reason. Nothing else supplies them.

    **It refuses to start without the settings `walk` needs** (`PLEX_URL`, `PLEX_TOKEN`,
    `PLEX_SOURCE_ROOTS`), checked right after the schedule. `walk` is a required step, so a
    container started with any of them empty fails every sweep and never publishes; exiting
    at startup makes that a stopped container instead of a nightly log line nobody reads.

    **Plex DB-EX starts after the migration**, when `PLEXDB_EXPLORE_PORT`
    is set, as a `plexdb-rs explore` child that serves for as long as the loop runs.

    **The migration runs at startup, once the schedule parses.** It used to
    wait for the first sweep's own `migrate` step, which meant a freshly
    deployed image could sit for most of a day running code that expected a
    column the store did not have yet — and anything run against the store in
    that window failed on the missing column rather than on anything it did
    wrong (issue #58's repair hit exactly that). Doing it here ties the schema to
    the code that is running rather than to the clock, and covers a restart
    nobody deployed for. It is a no-op on a store already current, so a restart
    loop cannot fill the disk with copies.

    **A failed sweep is retried every `RETRY_AFTER`** until one succeeds or the
    next scheduled time arrives, which runs as usual. Each retry logs
    `sweep retry at <time> (<seconds>s)`. An exception out of the sweep is not a
    sweep that failed: it ends the loop and the container with it.

    Never runs a *sweep* at startup, which is unchanged. The first sweep is at
    the next scheduled time, which is what makes "set the schedule a few minutes
    out and watch it" a real check that the schedule works rather than a check
    that the entrypoint runs.
    """
    from .sweep import run_locked_sweep

    do_sweep = run_locked_sweep if sweep is None else sweep
    do_sleep = time.sleep if sleep is None else sleep
    now_fn = datetime.now if clock is None else clock
    do_migrate = _migrate_store if migrate is None else migrate
    do_explore = _start_explorer if explore is None else explore
    do_frames = _start_frame_harvester if frames is None else frames
    do_extras = _start_extras_ticks if extras is None else extras
    do_audio = _start_audio_harvester if audio is None else audio
    do_walk_settings = require_walk_settings if walk_settings is None else walk_settings

    raw = os.environ.get(SCHEDULE_VAR, "").strip()
    if not raw:
        raise ConfigError(
            f"{SCHEDULE_VAR} must be set to the daily run time, HH:MM in 24-hour time "
            f"(the container's TZ decides the zone)"
        )
    at = parse_schedule(raw)
    do_walk_settings()

    # After the schedule is validated and before anything is swept: a container
    # started with a malformed PLEXDB_SCHEDULE has no business migrating a store
    # it is never going to sweep.
    do_migrate()
    do_explore()
    do_frames()
    do_extras()
    do_audio()

    completed = 0
    failed = False
    while iterations is None or completed < iterations:
        now = now_fn()
        target, retry = next_attempt(now, at, failed=failed)
        wait = (target - now).total_seconds()
        when = f"at {target.isoformat(timespec='seconds')} ({wait:.0f}s)"
        print(f"sweep retry {when}" if retry else f"next sweep {when}", flush=True)
        do_sleep(wait)
        code = do_sweep()
        print(f"sweep exited {code}", flush=True)
        failed = code != 0
        completed += 1
    return 0
