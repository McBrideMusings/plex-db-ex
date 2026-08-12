# Working on this without destroying the data

The store holds things that cost real money and real time to acquire, and that no re-run
reproduces. This page is how to develop against it anyway.

## The store is not a build artifact

Most projects can delete their database and rebuild it. This one cannot, and it is worth being
precise about why, because the instinct to "just re-scan" is exactly the expensive mistake.

| What is in there | Why a re-run does not get it back |
|---|---|
| 25,837 plays over twenty months | A fresh walk gets whatever Plex and Tautulli still hold. Anything either has aged out is gone. This is the entire input to every taste vector. |
| 115,453 TMDB keyword rows over 11,395 titles | Fetched in one rate-limited pass. Re-fetching costs the same quota again for a byte-identical result. |
| Crowd-list memberships | A list harvested in August is not the list you get in November, and no source offers "fetch it as it was". |

So the rule is not "back up the database". It is **never point a destructive operation at the one
copy that matters**.

## Two stores, and only one of them is real

**The baseline** lives on the host — see [`baseline.md`](./baseline.md) for its path and why it is
there. Every sweep writes to it. Every consumer reads a snapshot published from it. It is the
production store, and nothing experimental ever runs against it.

**A working copy** is whatever is at `PLEXDB_PATH` in your checkout, normally `./data/plexdb.db`.
It is disposable. `data/` is gitignored, so a worktree's copy never enters a commit and never
follows a branch anywhere.

```
admin pull-baseline        # host -> ./data/plexdb.db
```

Pull it whenever you want a clean slate. Break it however you like: drop tables, run a migration
under development, rewrite the taste rollup and see what the vector does, inject fabricated plays
to test a hypothesis. When it is wrecked, pull again.

## Data flows up once, and then almost never again

**Seeding — done once.** The baseline was seeded from a laptop store that already held the plays
and the enrichment ([#44](https://github.com/McBrideMusings/plex-db-ex/issues/44)). That was a
one-time transfer to get the expensive data onto the host without re-acquiring it.

**Deploys do not re-seed.** Installing or upgrading the writer on the host must never overwrite the
baseline with a checkout's copy, and must never re-scan what the store already has. Every sweep is
incremental by construction: `enrichment_cursor` carries a `fetched_at` per title, so a keyword
sweep against a seeded store issues zero TMDB requests. A deploy that started from an empty store
would silently spend the whole quota again.

**Dev does not push back — with one exception.** A working copy is for breaking things, and
whatever you broke must not travel to the host. The exception is **newly acquired data that was
expensive to get**: if you are building a new source and it has already harvested a good result
into your working copy, pushing those rows up can beat re-scraping the same thing on the host.
That is a deliberate, narrow act — copy the rows for that source, not the file — and it is worth
saying out loud in the issue or PR when you do it.

The distinction to hold on to:

| Change | Where it belongs |
|---|---|
| schema migration, new command, changed algorithm | code, committed; the host gets it by deploying and running the migration |
| a table you dropped to test something | the working copy, and nowhere else |
| fabricated rows for a hypothesis | the working copy, and nowhere else |
| real rows a new scraper legitimately acquired | may go up, per-source, deliberately |

## Testing does not touch either store

Every test builds its own store in a temp directory from `plexdb init`, and no test reaches the
network. That is why the suite is safe to run anywhere, and why a fixture is never a substitute for
driving a change against a pulled copy — the fixture proves the arithmetic, the real store is the
only thing that shows what the arithmetic says about your library.

When a change is worth checking against real data, pull a copy and run it there. When it is worth
checking against the real *service* — an API's response shape, a rate limit — run it against a
copy too, and read the result before letting it near the host.

## Copies are taken with `VACUUM INTO`, never `cp`

The store runs in WAL mode, so rows that are fully committed can still be sitting in the `-wal`
sidecar rather than in the main file. A `cp`, `scp` or `rsync` of `plexdb.db` taken while a sweep is
running grabs the main file and leaves those rows behind. The result opens cleanly, passes an
integrity check, and is quietly missing the last stretch of work — the worst shape a bad backup can
take, because nothing about it looks wrong.

Every copy in this project therefore goes through `VACUUM INTO`, which reads through a normal
connection, sees the sidecar, and writes one self-contained file with no sidecars of its own. That
is what `plexdb publish` already used for snapshots ([`store.py`](../plexdb/store.py)), what
[`tools/baseline.sh`](../tools/baseline.sh) uses for pulls and manual backups, and what
[`plexdb/backup.py`](../plexdb/backup.py) uses before a migration.

```
admin pull-baseline      # consistent copy, host -> ./data/plexdb.db
admin backup-baseline    # consistent copy, left on the host in backups/
admin backups            # what the host is holding
```

## A migration takes a copy first, and rolls back if the result is wrong

`schema.apply` wraps every pending migration and the version row in one transaction, so a process
killed partway leaves the file untouched. That protects against a **crash**. It does nothing about a
migration that is simply **wrong** — bad SQL commits perfectly happily, and migrations here are
forward-only, so there is no down-migration to run.

So `plexdb init` — the first step of every sweep — does this instead, in
[`store.migrate`](../plexdb/store.py):

1. If the store is already current, stop. Nothing is copied; a copy per sweep would fill the disk.
2. Otherwise copy the store to `backups/plexdb.pre-v<target>.db` beside it, and record the row
   counts of `items`, `plays` and `enrichment` — the three tables no re-run reproduces.
3. Apply the migrations.
4. Check three things, in the order where a failure of one makes the next meaningless: the store
   reports the version it was migrated to, `PRAGMA quick_check` says `ok`, and none of those three
   tables lost rows.
5. If the migration raised, or any of those checks failed, **put the copy back** and report the
   failure with the copy's path in it.

Every copy is kept. A second attempt at the same version writes `plexdb.pre-v9.2.db` rather than
overwriting the copy taken before the first attempt — that one is older and therefore the more
valuable of the two. `PLEXDB_BACKUP_DIR` moves the directory; unset, it is `backups/` beside the
store, so the container needs no extra mount.

**A row guard is deliberately narrow.** `edges`, collections and cursors are re-fetchable, so a
migration that rebuilds one of those is not a red flag and is not guarded. If a future migration
genuinely needs to drop plays or enrichment rows, that guard has to be relaxed for it on purpose,
which is the point.

## Working a change that touches the schema

1. `admin pull-baseline` — a fresh consistent copy of the real store.
2. Write the migration and the code, with tests.
3. `admin dev init` against the pulled copy. Read what it prints: the backup path, the version
   change, and the before/after row counts per guarded table. This is the rehearsal, and it is
   worth doing on real data because the fixtures cannot show you what your migration does to
   115,453 enrichment rows.
4. `admin vet`.
5. Merge to `main`.
6. `admin deploy image` — **after** the merge, never before. The deploy builds the image for
   `linux/amd64` here, ships it over ssh, and recreates the container. The migration itself runs on
   the host at the next sweep, inside `init`, with the backup and the rollback above.
7. `admin logs live` at the next scheduled run to watch it land.

There is no automatic deploy on merge, on purpose: the host has one store, the migration is
forward-only, and a person deciding when it happens is worth more than the minutes it saves.
