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
`SWEEP` and `ORDER`. The one judgement made here is that a sweep returning
non-zero does not stop the loop: the next day's run is a fresh attempt at a
store that is one day staler, which is strictly better than a stopped container
nobody notices. That is a property of the schedule, not of any step.

The time is read in the process's local zone, so `TZ` in the container's
environment is what decides when "03:30" is. A container with no `TZ` runs UTC.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from .errors import ConfigError

__all__ = ["SCHEDULE_VAR", "next_fire", "parse_schedule", "run_scheduler"]

#: The environment variable carrying the daily run time, `HH:MM`, 24-hour.
SCHEDULE_VAR = "PLEXDB_SCHEDULE"


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


def _migrate_store() -> None:
    """Bring the store to the schema this build expects, and say what happened.

    Deliberately not wrapped: a store that fails to migrate has already been
    rolled back to the copy `store.migrate` took, and the right thing then is to
    take the container down loudly rather than sweep a store at a schema this
    code does not match. Under `restart: unless-stopped` that is a restart loop
    with the reason in `docker logs`, which is louder than a sweep writing
    against the wrong shape.
    """
    from .config import Config
    from .store import migrate

    config = Config.from_env()
    result = migrate(config.store_path, config.backup_dir)
    if result.was == 0:
        print(f"created store at schema v{result.now}", flush=True)
    elif result.migrated:
        print(f"backed up to {result.backup}", flush=True)
        print(f"migrated store v{result.was} -> v{result.now}", flush=True)
    else:
        print(f"store already current at schema v{result.now}", flush=True)


def _start_explorer() -> None:
    """Serve the tag explorer over the published snapshot, if asked to.

    Off unless `PLEXDB_EXPLORE_PORT` is set. It reads the snapshot, never the
    live store: ADR-0007 is where readers look, and the snapshot is a file the
    writer only ever replaces by rename, so a request cannot see it half-written.
    Bound on every interface inside the container; which host address the port
    reaches is `[[docker_run.ports]]`'s decision, not this process's.

    The Query tab's saved queries go to `PLEXDB_EXPLORE_SAVED_PATH` when set —
    the container's own dedicated mount, never the snapshot's directory
    (plex-db-ex-oyg.3) — and fall back to a file beside the snapshot when it
    isn't, which is the dev/test case.

    A title's poster is proxied from Plex, server-side, using this same
    process's own `PLEX_URL`/`PLEX_TOKEN` (plex-db-ex-oyg.2, docs/adr/0017) —
    empty in dev without a `.env`, in which case `/api/poster` answers 503
    rather than failing to start.
    """
    from .config import Config
    from .explore import EXPLORE_PORT_VAR, EXPLORE_SAVED_PATH_VAR, serve_in_background

    raw = os.environ.get(EXPLORE_PORT_VAR, "").strip()
    if not raw:
        return
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"{EXPLORE_PORT_VAR} must be a port number, got {raw!r}")
    cfg = Config.from_env()
    snapshot = cfg.snapshot_path
    if snapshot is None:
        raise ConfigError(
            f"{EXPLORE_PORT_VAR} is set but PLEXDB_SNAPSHOT_PATH is not; "
            "the explorer reads the published snapshot"
        )
    saved_raw = os.environ.get(EXPLORE_SAVED_PATH_VAR, "").strip()
    saved_path = Path(saved_raw).expanduser() if saved_raw else None
    server = serve_in_background(
        snapshot,
        "0.0.0.0",
        int(raw),
        saved_path,
        plex_url=cfg.plex_url,
        plex_token=cfg.plex_token,
    )
    where = saved_path or snapshot.with_name("explore-queries.json")
    print(
        f"tag explorer on port {server.server_port}, reading {snapshot}, saved queries at {where}",
        flush=True,
    )


def run_scheduler(
    *,
    sweep: Callable[[], int] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    migrate: Callable[[], None] | None = None,
    explore: Callable[[], None] | None = None,
    iterations: int | None = None,
) -> int:
    """Migrate the store, then wait until the next scheduled time, run one sweep,
    repeat.

    Runs forever under a container. `iterations` bounds the loop so a test can
    watch it complete a fixed number of cycles; `sweep`, `sleep`, `clock`,
    `migrate` and `explore` are injectable for the same reason. Nothing else
    supplies them.

    **The tag explorer starts after the migration**, when `PLEXDB_EXPLORE_PORT`
    is set, and serves on its own thread for as long as the loop runs.

    **The migration runs at startup, once the schedule parses.** It used to
    wait for the first sweep's own `migrate` step, which meant a freshly
    deployed image could sit for most of a day running code that expected a
    column the store did not have yet — and anything run against the store in
    that window failed on the missing column rather than on anything it did
    wrong (issue #58's repair hit exactly that). Doing it here ties the schema to
    the code that is running rather than to the clock, and covers a restart
    nobody deployed for. It is a no-op on a store already current, so a restart
    loop cannot fill the disk with copies.

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

    raw = os.environ.get(SCHEDULE_VAR, "").strip()
    if not raw:
        raise ConfigError(
            f"{SCHEDULE_VAR} must be set to the daily run time, HH:MM in 24-hour time "
            f"(the container's TZ decides the zone)"
        )
    at = parse_schedule(raw)

    # After the schedule is validated and before anything is swept: a container
    # started with a malformed PLEXDB_SCHEDULE has no business migrating a store
    # it is never going to sweep.
    do_migrate()
    do_explore()

    completed = 0
    while iterations is None or completed < iterations:
        now = now_fn()
        target = next_fire(now, at)
        wait = (target - now).total_seconds()
        print(f"next sweep at {target.isoformat(timespec='seconds')} ({wait:.0f}s)", flush=True)
        do_sleep(wait)
        code = do_sweep()
        print(f"sweep exited {code}", flush=True)
        completed += 1
    return 0
