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
