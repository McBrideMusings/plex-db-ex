# The schedule is a command too

`plexdb schedule` is the container's entrypoint. It reads `PLEXDB_SCHEDULE`, sleeps until the next
occurrence of that wall-clock time, calls `plexdb sweep`, and repeats. It lives in
`plexdb/schedule.py`, inside the package, and the Dockerfile's `ENTRYPOINT` is
`["plexdb", "schedule"]` with no shell in front of it.

This supersedes one sentence of [ADR-0014](./0014-the-sweep-is-a-command-not-a-shell-script): "the
container's entrypoint invokes `plexdb sweep` and nothing else." Everything else that ADR decides
stands unchanged — including its own later paragraph describing the entrypoint as a loop, which is
the shape this record makes real. What moves is only *where the loop is written*.

## Why

**A `sleep` loop in the entrypoint is the same mistake ADR-0014 was written to undo, one layer
up.** That record's whole argument is that a run order living in a shell script is an order nothing
can check. A clock in a shell script is a clock nothing can check either, and it fails in a quieter
way: an order that is wrong produces a visibly wrong run, while a schedule that is wrong produces a
correct run at the wrong time, which looks like a correct run.

The arithmetic is small and it is exactly where the mistakes are:

| Case | What a wrong answer does |
|---|---|
| It is 04:00 and the schedule is 03:30 | Sleeps a negative number, or runs immediately, then runs again — a loop, not a schedule |
| A sweep finishes inside the minute it started | Sees its own start time as "next", sleeps zero, re-runs against the store it just wrote |
| It is 23:59 and the schedule is 00:30 | Rolls to tomorrow, or waits 23½ hours |
| `PLEXDB_SCHEDULE` is `3.30` | Read leniently, becomes 03:00, and refreshes half an hour early every night forever |

`next_fire(now, at)` is a pure function of two values, so each of those is a test that runs in
microseconds (`tests/test_schedule.py`). The shell version's only test is to set a time, wait, and
see. Nobody runs that test twice.

**The entrypoint holds a clock and nothing else.** No list of steps, no order, no `|| true`, no
opinion about which sources may fail — those stay in `sweep.py` where a command module declares its
own `ORDER` and `SWEEP`. The one judgement `schedule` makes is that a sweep returning non-zero does
not end the loop, and that is a property of the schedule rather than of any step: tomorrow's run is
a fresh attempt against a store one day staler, which beats a stopped container nobody notices.

**Overlap stays impossible for the reason ADR-0014 gives.** The sleep and the sweep are sequential
statements in one process, and the next fire time is computed from the clock *after* the sweep
returns, so a run that overshoots its next slot delays the following one rather than stacking a
second writer against the same file.

## Considered options

- **`sleep` and a `date` comparison in the Dockerfile's `ENTRYPOINT`.** Rejected above.
- **`cron` inside the container.** Rejected in ADR-0014 already, for overlap. It also adds a third
  scheduler to reason about and a second log surface — cron's mail, or nothing.
- **Unraid's User Scripts plugin running `docker start plexdb` on a cron, entrypoint `plexdb
  sweep`.** The purest reading of ADR-0014's superseded sentence, and rejected for where the
  schedule would then live: in a plugin's configuration on one machine, in nothing this repository
  commits, checkable by nothing here.
- **A cron expression instead of `HH:MM`.** Rejected as unneeded, not as wrong. Nothing wants "every
  six hours on weekdays"; if that changes, a parser goes behind the same variable and no caller
  moves. `HH:MM` is what can be validated strictly today.

## Consequences

**`plexdb schedule` never returns, and it is the only command that does not.** It declares no
`SWEEP`, so it is not a step of a sweep — a scheduler inside the thing it schedules would spawn
sweeps until the process died. `tests/test_schedule.py` asserts the absence.

**The zone comes from the container's `TZ`.** `03:30` means 03:30 wherever the container thinks it
is; with `TZ` unset that is UTC. The Unraid template carries it as a required variable for that
reason.

**The first run is at the next scheduled time, never at startup.** That is what makes "set the
schedule a few minutes out, start it, and watch" a real check that the schedule works, rather than
a check that the entrypoint runs.

**A dev checkout never sets `PLEXDB_SCHEDULE`.** `plexdb sweep` remains the one-shot a person runs
by hand on the Mac, unchanged.
