# The baseline store

There is one real store, it lives on the Unraid host, and **it is never edited in place.**

```
host  : ${PLEXDB_BASELINE_HOST}      (tailnet address, from .env)
path  : /mnt/user/appdata/plexdb/plexdb.db
```

## Why it is a baseline and not a working copy

It cannot be reproduced by re-scanning. Three separate reasons, each sufficient on its own:

**Twenty months of watch history.** 25,837 plays spanning 2024-12-03 to 2026-08-11. A fresh walk
recovers whatever Plex and Tautulli still hold; anything either has rolled off is gone the moment
this file is. That history is the entire input to every taste vector — ADR-0011's rollup is
computed from it and nothing else.

**A rate-limited enrichment sweep.** 115,453 keyword rows over 11,395 titles, every one fetched in
a single TMDB pass on 2026-08-10. Throwing it away means paying for it again, against the same
rate limits, for a result that would be identical. `enrichment_cursor` carries each title's
`fetched_at`, so a sweep against this file re-fetches nothing.

**Harvested data has no source to re-read.** Crowd lists change under you: a list harvested in
August is not the list you get in November, and no source offers "fetch it as it was". The moment
a harvest runs, the store holds a snapshot of the outside world that cannot be reconstructed.

It is also the only thing a taste vector can honestly be judged against. A fixture store proves
the arithmetic; it cannot tell you whether the vector describes the household.

## Working on it

**Run:**

```
admin pull-baseline
```

That copies the host store to `./data/plexdb.db`. Break that copy freely — a migration under
development, a new sweep, a repair, a wrong idea. Pull again to get back to a known state.

Nothing pushes a working copy back up **as a file**. The one case where data legitimately travels
upward is a new source that has already harvested a good result into a working copy — copying
those rows up beats re-scraping the same thing on the host. That is per-source, deliberate, and
worth saying out loud when you do it; it is never "sync my copy over prod".

[`development.md`](./development.md) has the whole practice: what is safe to break, what a deploy
must never re-acquire, and where each kind of change belongs.

## The two paths are different on purpose

| | Path | Written by | Read by |
|---|---|---|---|
| baseline | `/mnt/user/appdata/plexdb/plexdb.db` | `plexdb` sweeps | nothing directly |
| snapshot | `/mnt/user/appdata/etv-station/data/plexdb.snapshot.db` | `plexdb publish` | consumers, read-only |

`plexdb publish` writes the snapshot with `VACUUM INTO` and an atomic rename, so a consumer never
observes a half-written file (ADR-0007). The snapshot path sits inside the station's own data
volume — `/data` inside that container — so nothing copies the file across a network and no second
mount is needed.

Consumers open the **snapshot**, never the baseline. A consumer holding the baseline open would be
reading a file a sweep is writing to.

## What is not done yet

Nothing schedules the sweeps on the host, and `plexdb` does not run there — the package still runs
from a checkout on a Mac. That is
[issue #43](https://github.com/McBrideMusings/plex-db-ex/issues/43): a container image, a schedule,
and credentials from the host environment. Until it lands, the baseline is a file on the host that
a person updates by running commands elsewhere, which is better than a file on a laptop but is not
the finished shape.
