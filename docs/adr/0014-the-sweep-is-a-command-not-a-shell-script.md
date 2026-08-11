# The sweep is a command, not a shell script

`plexdb sweep` runs the whole scheduled pass: every Step in order, each one declaring whether its
failure ends the run or is merely reported. The run order is the `ORDER` number each command
already carries. The container's entrypoint invokes `plexdb sweep` and nothing else — it holds no
list of commands and no opinion about which may fail.

## Why

Before this, no module represented a run. Nine commands were to be invoked in order by something
outside the program, which meant the order and the failure policy would have lived in a shell
script — and been copied into the container's schedule, and into whatever a person types on the
Mac.

**The order had already drifted where nothing could see it.** `plexdb/commands/__init__.py`
describes `ORDER` as "its position in `plexdb --help` … in the order someone runs them — create the
store, fill it, enrich it, publish it". The numbers said otherwise:

| | |
|---|---|
| `publish` | 30 |
| `enrich-tmdb-keywords` | 40 |
| `enrich-tmdb-edges` | 45 |
| `enrich-tautulli-plays` | 55 |

`publish` sorted ahead of every step whose output it is supposed to publish. Nothing broke, because
`ORDER` only sorted `--help`, which is exactly why it went unnoticed. A number that claims to be a
run order and is never used as one will always drift; the fix is to use it, so a wrong value is a
wrong run rather than a cosmetic quirk.

**"Which steps may fail" is a fact about a source, and it was going to be stored away from the
source** — as `|| true` on some lines of a script and not others. Each command declares it instead:

```python
SWEEP = Step.REQUIRED  # init, walk, ingest-plays, publish
SWEEP = Step.BEST_EFFORT  # tautulli, keywords, edges, mdblist
```

A command that declares neither is not in the Sweep — `latent-users`, `reconcile-etv`,
`repair-identities` and `local-edges` say nothing and stay out. Absence is the safe default: a new
command cannot silently join a nightly run that writes to consumers.

**Absence earned its keep the first time the sweep ran.** `local-edges` was in the original
composition and came out again after a full run measured what it writes: 16,381,416
`local_collection` edges, taking the published snapshot from 72 MiB to 3.7 GB, because
co-membership is quadratic and one Plex collection has 3,004 members (#48). Nothing about the
command's own output said so — it prints a summary line and a list of large collections, and the
size only appears once something publishes the result. A default of "in unless it opts out" would
have shipped that to consumers.

This restores the promise the same docstring makes — "adding a command is one new file; nothing
else in this package or in `plexdb/cli.py` changes" — which was true of the parser and false of the
run. Issues #35–#38 add four more sources behind that gap.

**The line between required and best-effort is where this store's data ends and someone else's
begins.** ADR-0004 puts every source but Plex behind an optional adapter; a source allowed to be
absent is a source allowed to fail. The reverse also holds: a broken `walk` must not publish a
snapshot built on a half-read library.

**"Not configured" and "not reachable" were already distinguishable.** `plexdb/errors.py` separates
`ConfigError` ("a required setting is missing") from `PlexError`/`TMDbError`/`TautulliError`/
`MDBListError` ("could not be reached"). The Sweep reports the first as *skipped* and the second as
*failed but tolerated*, so a source nobody has set up does not log an error every night forever.
No new declaration was needed for this.

**A second sweep must not be able to start.** `PRAGMA busy_timeout` is 5 seconds
(`plexdb/store.py:23`), so two overlapping runs do not deadlock — the loser waits five seconds and
then errors on its first write batch. Under the policy above most of those errors are
`BEST_EFFORT`, so they would be logged, tolerated, and followed by a `publish` of a store two
processes had been writing at once. Silent, and it reaches consumers. Overlap is plausible: the
first full TMDB pass covered 11,395 titles against a rate limit.

So the container runs no cron. Its entrypoint is a loop — run a sweep, sleep until the next
wall-clock time in `PLEXDB_SCHEDULE`, run the next one. A second sweep cannot exist, because nothing
can start one while the loop is inside the first.

## Considered options

- **A shell script on the host holding the order and the `|| true`s.** Rejected: the order would
  exist in three places that cannot check each other, `ORDER` would stay a number that lies, and a
  new source in #35–#38 would require editing a file outside the package — the thing
  `commands/__init__.py` promises never happens.
- **A second number, `SWEEP_ORDER`, beside `ORDER` for display.** Rejected: two numbers that must
  be kept consistent by hand is the duplication this decision exists to remove. Renumbering so
  `ORDER` means the run order costs one line per file and makes `--help` read correctly as a
  side effect.
- **The Sweep holds an explicit list of its steps.** Rejected: readable in one place, but adding a
  source then means editing `sweep.py`, which breaks the one-new-file promise again in a new
  location.
- **Cron inside the container plus a lock file in `plexdb sweep`.** Rejected: it guards the overlap
  rather than removing it — two sweeps can still be started, and a container killed mid-run leaves
  a stale lock that becomes its own failure mode. The loop makes the second sweep impossible instead
  of impolite.

## Consequences

**The schedule is wall-clock, not cron.** `PLEXDB_SCHEDULE=03:00` and the entrypoint sleeps to the
next occurrence. Nothing on the roadmap wants "every six hours on weekdays"; if that changes, a cron
parser goes behind the same variable without any caller moving. The time is worth setting
deliberately — `walk` and `ingest-plays` hit Plex on every run regardless of staleness gating, so a
sweep does real work against a 87,913-item library each time.

**`plexdb sweep` stays a one-shot.** Only the container wraps it in a loop, so the Sweep is still a
command you run by hand on the Mac and still testable through its interface rather than as a daemon.

**A degraded run is a normal outcome.** A sweep where TMDB was down still publishes, with the TMDB
step reported as failed. The snapshot is fresh for everything that worked, and staleness gating
means the skipped titles are picked up next run. Distinguishing "yesterday's snapshot because the
sweep stopped" from "today's snapshot missing one source" is the reporting's job.

**Renumbering changed `--help` output.** `publish` now appears last and the plays steps appear
before the enrichment block, which is both the run order and the order someone would read.
