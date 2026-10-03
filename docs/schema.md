---
outline: deep
---

# Schema

The SQLite schema **is** the public API of this project. One process writes it; every consumer
opens the same file read-only. There is no version negotiation, so a schema change is a
breaking change for every consumer at once ([ADR-0001](./adr/0001-one-writer-many-readers-sqlite-file-is-the-interface)).

## Versioning

The store carries its version in a `schema_version` table. Migrations are an append-only list
in `plexdb/schema.py`: index 0 takes an empty database to version 1, index 1 takes it to
version 2, and so on. A shipped migration is never edited — another is added.

Opening a store whose version is **higher** than the running build understands is refused
outright, because a newer writer may have added rows this build cannot see. A store carrying a
`schema_version` table with no row is reported as damaged rather than treated as empty.

**Versions 1 through 14 are live.** Everything under "Not yet built" is the target for later
slices.

### A migration is copied before it runs, and rolled back if it goes wrong

Every pending migration and the version row go in one transaction, so a process killed partway
leaves the store untouched. That covers a crash. It does not cover a migration that is simply
wrong — bad SQL commits perfectly happily, and this list is forward-only, so there is nothing to
run backwards.

`plexdb migrate` therefore copies the store before applying anything, into
`backups/plexdb.pre-v<target>.db` beside it (`PLEXDB_BACKUP_DIR` moves the directory). After the
migration it checks that the store reports the target version, that `PRAGMA quick_check` says
`ok`, and that `items`, `plays` and `enrichment` did not lose rows. Any of those failing restores
the copy and reports which file it came back from.

Those three tables are guarded because no re-run reproduces them: watch history Tautulli
eventually forgets, and a TMDB sweep paid for against a rate limit. Edges, collections and cursors
are re-fetchable and are not guarded, so a migration that rebuilds one of them passes. A future
migration that genuinely must drop guarded rows has to relax the guard on purpose.

A store that is already current is not copied — `migrate` runs at the top of every sweep and again whenever the container starts, and a copy
per sweep would fill the disk with identical files. Copies are never pruned: a second attempt at
the same version writes `plexdb.pre-v9.2.db` rather than overwriting the one taken before the
first attempt.

## Version 1 — identity and enrichment

Every table keys on `item_id` — the opaque, deterministic string described in
[ADR-0002](./adr/0002-item-id-is-first-hit-wins-over-external-guids), derived first-hit-wins from external
GUIDs: `imdb:tt1375666`, else `tmdb:…`, else `tvdb:…`, else `plex:…`, else `fs:<hash>`.

```sql
CREATE TABLE items (
    item_id        TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    title          TEXT NOT NULL,
    title_sort     TEXT,
    show_title     TEXT,
    show_item_id   TEXT,
    season         INTEGER,
    episode        INTEGER,
    year           INTEGER,
    duration_ms    INTEGER,
    content_rating TEXT,
    studio         TEXT
);

-- Every other id a title is known by. Primary key is (ns, value, kind), so one
-- id resolves to exactly one item *of that media kind* and a collision within
-- a kind is a write-time error rather than a silent duplicate. `kind` matches
-- items.type: 'movie', 'show' or 'episode'.
CREATE TABLE external_ids (
    item_id   TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns        TEXT NOT NULL,
    value     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (ns, value, kind)
);

-- Where Plex keeps a title. Watch history identifies a title only by rating
-- key, so this is the join that makes a play resolvable to an item.
CREATE TABLE plex_items (
    rating_key TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    section_id TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE enrichment (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    rank       INTEGER,
    PRIMARY KEY (item_id, namespace, source, key, value)
);

-- Every raw keyword spelling a source has ever written, mapped to the
-- normalized-and-stemmed value stored above it.
CREATE TABLE keyword_forms (
    surface TEXT PRIMARY KEY,
    keyword TEXT NOT NULL
);
```

**`source` joined the primary key in schema v10**
([ADR-0016](./adr/0016-keywords-are-one-source-agnostic-namespace-with-a-source-column)). Before it,
a source's own name was encoded into the namespace string itself (`tmdb_keywords`), which meant a
second keyword source needed a second namespace, and a source's refresh — a blanket `DELETE FROM
enrichment WHERE namespace = ?` — would have deleted another source's rows sharing that namespace.
Every keyword-bearing writer now shares the namespace `keywords` and scopes its own refresh to
`WHERE namespace = 'keywords' AND source = <itself>`, so a keyword two sources both list on an item
survives either one's refresh alone.

**Every keyword is normalized and stemmed before it lands here** (`plexdb/keywords.py`): lowercase,
trim, collapse whitespace, `-`/`_` become spaces, then each word is run through Snowball's English
stemmer. `Heists`, `heist`, and `bank-heist`/`bank heist` all resolve to one stored value, so a
consumer scoring by keyword-cosine sees one confirmed signal instead of unrelated near-misses.
`keyword_forms` is what lets a reader still show the spelling a source actually used — every keyword
write upserts a row mapping its raw `surface` to the `keyword` value stored in `enrichment`.

`external_ids` is what lets an enrichment fetcher that needs a TMDb id find one without
assuming the primary key is one. It is also where a Trakt slug, a Letterboxd URL, and a
resolved free-text Reddit title land.

**`kind` is in the key because a TMDB or TVDB number is only unique inside one media type.**
Both sources number movies and TV shows in separate lists that start at 1, and Plex reports
each as a bare `tmdb://1678` with no type attached — movie 1678 is *Godzilla* (1954), show
1678 is *The Golden Girls* (1985). Keyed on `(ns, value)` alone, those two unrelated records
claimed one row and the walk fused the two titles into a single identity (issue #23). IMDb
numbers everything in one shared list and Plex's own `plex://` ids are already type-qualified,
so neither collides — but `kind` is in the key for every namespace rather than the two that
happen to need it. A store that fused identities before schema version 5 is repaired with
`plexdb repair-identities`.

**`last_seen` is stamped by every walk that observes the row** — schema v8
([issue #57](https://github.com/McBrideMusings/plex-db-ex/issues/57)), following
`plex_items.last_seen`'s precedent one level up. A row Plex reports again on a later walk gets
`last_seen` moved forward; a row Plex stops reporting keeps whatever value it already had, so its
`last_seen` falls behind the current walk's timestamp. That is the only way this table can tell
"Plex stopped publishing this id" apart from "Plex published it once and we kept it" — the two
were indistinguishable before this column existed. See
[ADR-0008's amendment](./adr/0008-the-walk-never-repoints-an-existing-item-id) for what this means
when the id Plex stops reporting is the one an `item_id` was derived from.

**An `item_id` covering two rating keys means one title Plex lists twice — the walk now refuses
the case where it does not.** Two copies of a film in two sections, and one episode Plex lists
under two season numbers, both land two rating keys on one identity, which is intended. But Plex
also hands a freshly-aired episode still showing as `TBA` the TVDB id of a *different* episode of
the same show, and one shared id was enough for the walk to adopt the other episode's identity,
overwrite its title, and leave it with no row at all: *The Simpsons* S37E16 took S37E14's
`imdb:tt36431487` and S37E14 vanished from the store
([issue #58](https://github.com/McBrideMusings/plex-db-ex/issues/58)). No column changed for this
— the walk now compares titles before adopting an identity another rating key already holds: same
title, it
merges as before; different title, the record takes an id derived from only the external ids
nothing else has claimed — an `fs:` path hash when the shared id was the only one Plex reported
for it. `external_ids` keeps pointing the shared id at whichever title had it first, so a reader
looking up `tvdb 11464298` still gets S37E14. Every occurrence is named in `plexdb walk`'s
summary, and a store already holding a fused row is repaired by the next walk — the episode whose
own GUIDs derive the contested id keeps it, the other one moves off.

### Namespaces in use

| Namespace | Source | Writer | Keys |
|---|---|---|---|
| `keywords` | `tmdb` | `plexdb enrich-tmdb-keywords` | `keyword`, one row per normalized-and-stemmed keyword, `value` is the stored keyword text (see `keyword_forms` above for the raw spelling). Nothing else. Only `items.type` `movie` and `show` are enriched — TMDB has no keywords endpoint for an episode. |
| `keywords` | `wikidata` | `plexdb enrich-wikidata` | `keyword`, stored the same way: the English labels of the title's Wikidata narrative location (P840), set in period (P2408), main subject (P921) and genre (P136). Movies and shows with an `imdb` external id only. A P840 keyword also gets a `region` row in `keyword_roles`, a P2408 keyword an `era` row. |
| `awards` | `wikidata` | `plexdb enrich-wikidata` | `award`, one row per English label of the title's award received (P166), stored verbatim — not normalized or stemmed, and not a keyword. |

**A reader rolling up keyword rows into a count or a set reads `enrichment` through
`SELECT DISTINCT item_id, value`, never a bare row count or row list.** Once a second source can list
the same keyword on the same item, `namespace`+`key` alone matches one row per source — a bare
`COUNT(*)` or unfiltered `SELECT value` would count or list that keyword once per agreeing source
instead of once per item. `plexdb/clusters.py`'s `build_keyword_profile` and every keyword rollup
in `plexdb/explore.py` (`build_index`, `titles_tagged`, `tag_network`, `title_map`,
`title_keywords`) dedupe this way.

## enrichment_cursor

How far each writer has got. **Never mixed into `enrichment`**
([ADR-0013](./adr/0013-bookkeeping-never-shares-a-table-with-facts)).

```sql
enrichment_cursor(
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, namespace, source, key)
)
```

| Namespace | Source | Key | Written by |
|---|---|---|---|
| `keywords` | `tmdb` | `fetched` | `enrich-tmdb-keywords`, one per title |
| `keywords` | `wikidata` | `fetched` | `enrich-wikidata`, one per title it asked about, covering both its `keywords` and its `awards` rows, written even when Wikidata had nothing |
| `tmdb_edges` | `tmdb` | `fetched_recommendations`, `fetched_similar` | `enrich-tmdb-edges`, one per title per edge type |

**`source` joined this table's primary key in the same schema v10 that added it to `enrichment`**
([ADR-0016](./adr/0016-keywords-are-one-source-agnostic-namespace-with-a-source-column)): a
per-title fetch cursor is state belonging to one writer, and without `source` a second writer of
`keywords` would read and overwrite the first one's cursor row, and a `--rewipe` would delete both
writers' cursors instead of just its own. `enrich-tmdb-keywords` and `enrich-tmdb-edges` both scope
every cursor read, write, and delete to their own `source = 'tmdb'`.

A cursor exists so a title whose result was **empty** is still cacheable. A movie TMDB has no
keywords for leaves no `enrichment` row, and a title whose recommendations all fall outside the
library leaves no `edges` row — without a cursor, both would be re-asked on every sweep forever.

**There is no `value` column, because a cursor has no value.** It used to carry the string `"1"`,
which was filler; `fetched_at` was always the whole point.

**These used to live in `enrichment` with `_`-prefixed keys, and it was a real bug** (issue #41).
A leading underscore meant "bookkeeping, skip me", and exactly one query in the reader crate knew
it. On the author's store the sentinels were **43.6%** of a taste vector's total weight, all of it
the string `"1"` — and because a title's weight is divided across its attributes, each sentinel
also shrank every genuine keyword on that title. The divisor error reaches **41%** on a title
carrying one real keyword, so it does not cancel across a library; it reorders the bottom of every
ranked list.

Moving them to a different *namespace* would not have fixed it: the rollup scans every namespace,
and only the key prefix was hiding them. A different **table** does fix it, permanently and for
every reader — including one querying the published snapshot with plain SQLite, which ADR-0007
makes an expected thing to do. A writer needing bookkeeping puts it here; nothing filters anything.

`enrich-tmdb-keywords` re-fetches a title only once its row is older than `TMDB_KEYWORDS_STALE_DAYS`
(default 45 days). `--rewipe` deletes every `source = 'tmdb'` row under `keywords` before a sweep,
forcing a full re-fetch, without touching another source's keywords or any other namespace.

`enrich-wikidata` asks again about a title only once its cursor is older than `WIKIDATA_STALE_DAYS`
(default 45 days), and replaces that title's `wikidata` rows in both namespaces when it does.
`--rewipe` deletes every `source = 'wikidata'` row in `keywords`, `awards`, `keyword_roles` and
`enrichment_cursor`, and nothing else.

Foreign keys are enforced (`PRAGMA foreign_keys = ON`) and the live store runs in WAL mode for
the writer's own benefit. Consumers never open that file. `plexdb publish` writes a consistent,
single-file copy with `VACUUM INTO` — no `-wal` or `-shm` sidecars — and every consumer opens
*that* file, read-only, including from a directory with no write permission
([ADR-0007](./adr/0007-readers-get-a-snapshot-not-the-live-store)).

## Version 2 — watch history

One row per Plex watch-history event ([ADR-0004](./adr/0004-the-store-owns-watch-history)),
written by `plexdb ingest-plays`.

```sql
CREATE TABLE plays (
    history_key       TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    plex_account_id   INTEGER NOT NULL,
    client_identifier TEXT,
    platform          TEXT,
    viewed_at         INTEGER NOT NULL,
    ip                TEXT,
    percent_complete  INTEGER,
    paused_counter    INTEGER
);

-- The incremental-fetch watermark, one row. See below for why it is not
-- simply MAX(plays.viewed_at).
CREATE TABLE plays_ingest_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    since_viewed_at INTEGER NOT NULL
);
```

`history_key` is Plex's own identity for the *viewing* (its `historyKey`, e.g.
`/status/sessions/history/58536`) — not to be confused with `plex_items.rating_key`, which is
the *title's* identity. It is what makes ingest idempotent: re-recording an event already on
file is `ON CONFLICT(history_key) DO NOTHING`, a no-op.

`item_id` resolves through `plex_items`, populated by `plexdb walk`. An event whose rating key
has no row there means the walk and the history have diverged — the ingest counts and reports
it, never drops it silently.

**Staying reachable until a walk catches up is why `plays_ingest_cursor` exists.** The obvious
watermark — the newest `viewed_at` already written to `plays` — breaks that promise: an
unresolved event is never written, so that watermark would advance past it the moment any
*newer* event resolved, and it would never be asked for again even after a later walk added its
rating key. `plays_ingest_cursor` instead tracks the oldest event a run could not resolve. As
long as anything is outstanding, the next run's cutoff stays pinned there, so that event — and
everything at or after it — is asked for again. Only once a run resolves everything does the
cursor advance, to the newest event that run saw. Plex's own `viewedAt>` history filter (used to
narrow the request server-side) is **inclusive** in practice despite its name — the boundary
event comes back on every run — which `ON CONFLICT` absorbs for free.

`plex_account_id` and `client_identifier`+`platform` come from Plex directly, the latter via a
join against `/devices` (cached per ingest — a play references a device far more often than the
device list changes). `ip`, `percent_complete`, and `paused_counter` are filled only by
`plexdb enrich-tautulli-plays` (issue #9, Version 4 below) and are null on a Plex-only
deployment — the normal state, not a degraded one. Measured against a live server: of 268 plays
finished at 90% or more, 261 appear in Plex's own history; of 17 plays abandoned under 40%, only
3 do — so a Plex-only ingest delivers positive signal only.

## Version 3 — affinity edges

Directed, typed, ranked item-to-item relationships
([issue #6](https://github.com/McBrideMusings/plex-db-ex/issues/6)), written by
`plexdb enrich-tmdb-edges`.

```sql
CREATE TABLE edges (
    from_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    to_id      TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    edge_type  TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, edge_type)
);
```

**Edges are snapshots, not facts.** On re-pull, the whole `(from_id, edge_type)` set is deleted
and rewritten inside one transaction — a run interrupted partway through never leaves a
half-replaced set. Additions and removals fall out on their own: a title the source stopped
recommending simply isn't in the new set. `rank` is the source's own ordering, stored verbatim —
never collapsed to a boolean — so a #2 recommendation stays distinguishable from a #20.

**Both `from_id` and `to_id` reference `items`.** `items` is populated only by `plexdb walk`
([ADR-0005](./adr/0005-the-store-walks-plex-itself-and-augments-never-replaces)), so a
recommendation pointing at a title this library has never walked has no `item_id` to land on. Such
a result is dropped and counted, never stored under an invented id
([ADR-0009](./adr/0009-an-edge-only-connects-two-items-this-store-already-knows)).

### Edge types in use

| Edge type | Writer | Signal |
|---|---|---|
| `tmdb_recommendations` | `plexdb enrich-tmdb-edges` | TMDB's `/recommendations` endpoint — behavioural: "people who engaged with this also engaged with that". |
| `tmdb_similar` | `plexdb enrich-tmdb-edges` | TMDB's `/similar` endpoint — content-derived, not behavioural. Kept as a distinct type from `tmdb_recommendations` because the two measure different things. |

**Measured on the author's library** ([issue #42](https://github.com/McBrideMusings/plex-db-ex/issues/42),
first full run, 11 August 2026): 12,961 of 14,079 walked movies and shows carried a TMDB id and were
fetched; 1,118 had none and were skipped. `tmdb_recommendations` wrote 120,941 edges and dropped
138,029 targets as outside the library; `tmdb_similar` wrote 113,855 and dropped 131,730. So roughly
half of what TMDB names is a title this library owns, and a fetched title averages about nine edges
per type.

**The two types are not of equal quality, and a consumer should not treat them as interchangeable.**
Read back from that run:

| From | `tmdb_recommendations`, ranks 1–4 | `tmdb_similar`, ranks 1–3 |
|---|---|---|
| *Alien* (1979) | *Alien: Covenant*, *Aliens*, *Prometheus*, *Alien: Romulus* | — |
| *Inception* (2010) | *The Dark Tower*, *Solo: A Star Wars Story*, *Push* | *Quantum of Solace*, *The Tuxedo*, *Zootopia* |
| *The Matrix* (1999) | *Teen Titans*, *The Matrix Revolutions*, *Terminator Genisys* | *The Tuxedo*, *The One*, *Logan's Run* |

Both are stored exactly as TMDB ordered them
([ADR-0012](./adr/0012-the-store-records-what-a-source-said-never-a-score-it-computed)) — the store
does not rank sources. **But only one of the two orderings means anything**, which matters to any
reader that sorts by `rank`. Scoring every stored edge by the keyword overlap of its two ends, with
a random pair of titles as the control:

| | rank 1 | rank 5 | rank 20 | vs. a random pair |
|---|---|---|---|---|
| `tmdb_recommendations` | 0.1349 | 0.0745 | 0.0453 | 17× |
| `tmdb_similar` | 0.0416 | 0.0407 | 0.0450 | 12× |
| random pair (control) | — | — | — | 0.0037 |

`tmdb_recommendations` decays threefold across its list: rank 1 genuinely is its best answer.
`tmdb_similar` is flat and drifts slightly upward — its twentieth answer is as good as its first, so
sorting by its rank buys nothing. Both beat random by a wide margin, so `tmdb_similar` is not noise;
it is an unordered bucket of roughly-related titles, useful as a candidate pool to filter and not as
a sequence to walk. Tracked as
[issue #52](https://github.com/McBrideMusings/plex-db-ex/issues/52).

**Plex's own collections are not a source, and there is no `local_collection` edge type**
([issue #48](https://github.com/McBrideMusings/plex-db-ex/issues/48)). Co-membership used to be
stored here as every ordered pair, so a collection of N members wrote N×(N−1) rows — measured at
**17,809,980 edges**, taking the published snapshot from 72 MiB to 3.7 GB.

Size was the visible problem; it was not the reason for dropping it. **A collection someone
assembled by hand states a similarity its author already knows about.** Feeding that back as a
recommendation is circular — it returns the curator's own judgement to them as a discovery. Whether
a collection was hand-built in Plex or synced from an outside list by a tool, the store cannot tell
from Plex alone, so it takes none of them. Outside curation is read from the source that publishes
it, where it arrives with a rank, a size and a follower count that Plex does not carry.

`enrich-tmdb-edges` re-fetches a title's edge set only once it is older than
`TMDB_EDGES_STALE_DAYS` (default 45 days, tracked separately from `TMDB_KEYWORDS_STALE_DAYS`).
`--rewipe` deletes every row of one run's edge types before a sweep, forcing a full re-fetch,
without touching any other edge type. The per-title fetch cursor this staleness check reads lives
in `enrichment_cursor` under the `tmdb_edges` namespace, not in `edges` itself — a title whose
result is empty, or entirely outside the library, still needs a `fetched_at` to check next sweep,
and `edges` alone cannot carry one for a title with no rows.

## Version 4 — the Tautulli history adapter

Two columns added to the existing `plays` table ([issue #9](https://github.com/McBrideMusings/plex-db-ex/issues/9)),
written by `plexdb enrich-tautulli-plays` — an optional adapter, same as `ip` and
`percent_complete` above; the store still runs with neither Tautulli column populated.

```sql
ALTER TABLE plays ADD COLUMN seconds_watched INTEGER;
ALTER TABLE plays ADD COLUMN tautulli_id INTEGER;
CREATE UNIQUE INDEX idx_plays_tautulli_id ON plays(tautulli_id) WHERE tautulli_id IS NOT NULL;
```

**`seconds_watched` is Tautulli's `duration` copied verbatim.** `duration` is already net of
paused time — measured over 151 completed history rows, `stopped - started - duration -
paused_counter` lands within 0-2 seconds of zero, rounding only — so computing `duration -
paused_counter` would subtract pause a second time and roughly halve the footage on exactly the
paused-heavy rows this column exists to measure. There is no stored floor and no stored
completion flag: **the floor a play must clear to "count" is the reader's choice, made at query
time** (`WHERE seconds_watched >= 30`, or whatever threshold that reader wants), never baked into
this schema — a stored `counts_as_signal` boolean would freeze today's threshold into every row,
and changing it later would mean rewriting history. Do not re-add one.

**A Tautulli row finds its play by composite match, not a shared key.** Plex's `historyKey` has
no equivalent in Tautulli's `get_history` response, so `enrich-tautulli-plays` matches on
`rating_key` (resolved through `plex_items`, same as `ingest_plays`) + `plex_account_id` +
`client_identifier` as hard keys, then takes the play whose `viewed_at` is nearest the Tautulli
row's `stopped`, within ±900 seconds (measured against the live server: this window matched 145
of 151 matchable rows that had any candidate play at all; ±300s would have matched only 110). The
Tautulli row's own `id` is stored in `tautulli_id`, guarded by the partial unique index above — a
re-run is idempotent, and a play already carrying a different `tautulli_id` is never re-matched.
A Tautulli row with a null `id` is an in-progress session, not history, and is skipped.

**The server owner's account id differs between Plex and Tautulli, and is resolved before the
match, not inside it** ([issue #26](https://github.com/McBrideMusings/plex-db-ex/issues/26)).
`plays.plex_account_id` and Tautulli's `user_id` are the same id space for every account except
one: Plex's history stores whoever owns the server under the local account id `1`, while Tautulli
reports that same person under their plex.tv account id instead — confirmed against the live
server, where every other account already carries the identical numeric id on both sides.
`enrich-tautulli-plays` fetches Plex's `/accounts` and Tautulli's `get_users` every run and joins
them on account name (the one field both systems report identically) to build a
`tautulli_user_id -> plex_account_id` map; a Tautulli row's `user_id` is translated through that
map before it becomes part of the match key above. See ADR-0010.

## Version 5 — an external id is scoped by media type

[Issue #23](https://github.com/McBrideMusings/plex-db-ex/issues/23). `external_ids` gains `kind`
and moves its primary key to `(ns, value, kind)`; see the table definition and the note under it
above for why. Existing rows take their `kind` from `items.type`.

```sql
ALTER TABLE plays ADD COLUMN rating_key TEXT;
CREATE INDEX idx_plays_rating_key ON plays(rating_key);
```

**`plays.rating_key` records what a play was resolved *from*, not just what it resolved *to*.**
`ingest_plays` reads a Plex history event, looks its `ratingKey` up in `plex_items`, and writes
the resulting `item_id`. Until this column existed the rating key was then discarded, so a play
carried a conclusion with no record of the evidence — and when an identity turned out to be
wrong, no play already written could be re-pointed at the corrected one. That is why repairing
issue #23 costs a re-ingest rather than an `UPDATE`. Backfilled at migration time for every
identity mapping to exactly one rating key, and left null wherever an identity maps to several —
the fused ones, and equally the legitimate merges from
[#19](https://github.com/McBrideMusings/plex-db-ex/issues/19), where one title sits in two library
sections. Null is the honest answer in both: the store recorded no evidence to recover.

**`repair-identities` reads it, and that is what makes a correction cheap.** A play carrying a
rating key is snapshotted before the delete and written back afterwards against whichever identity
that key now resolves to — right title, Tautulli columns intact, no history re-read, and no cursor
rewind on its account. Only the plays that *cannot* be placed force a rewind, and only far enough
to reach them. Measured by reconstructing a fusion on the real store: 32 plays across a film and
an episode, all 32 carried across onto the correct title, total play count unchanged, cursor
unchanged, 30 of them keeping their `tautulli_id` and `seconds_watched`.

The historical fusions this schema version exists to fix are the exception, and permanently so:
they all predate the column, so their plays carry no rating key and are still dropped and
re-ingested. The re-point path is for every correction after this one.

`plexdb repair-identities` is the corrective pass. It deletes every identity whose Plex records
span **more than one media kind**, re-walks Plex so each rating key derives its own id again, and
rewinds `plays_ingest_cursor` to the oldest affected play. A plain re-walk cannot do it — both
rating keys are already in `plex_items` pointing at the fused id, so the walk finds that identity
by rating key and ADR-0008 correctly keeps it. `--dry-run` lists what it would split; both modes
read Plex, because only Plex knows what kind a rating key is.

**The tempting shortcut — "delete every identity holding two different values in one namespace" —
is wrong and does damage.** Plex reports every match it holds, so one record can legitimately
carry several ids in a namespace (*The Animatrix* carries nine TMDB ids on a single rating key),
and two records of the same kind can legitimately merge and still disagree (South Park S28E1 sits
in the library twice; the copies share a TVDB id, so version 5 merges them, but Plex matched them
to different IMDb entries). Neither is a fusion, and deleting either re-walks it into byte-identical
rows, so that test never reaches a fixed point — measured on the author's library it churned 25
identities forever, dropping their plays on every run.

## Collection membership

Which crowd lists a title appears on, and where in them — schema v6, written by
`plexdb harvest-mdblist` ([issue #34](https://github.com/McBrideMusings/plex-db-ex/issues/34)).

```sql
collection(
    collection_id   TEXT PRIMARY KEY,   -- "mdblist:14" — source-namespaced, opaque
    source          TEXT NOT NULL,      -- "plex", "mdblist", "letterboxd", "editorial", "reddit"
    name            TEXT,
    url             TEXT,
    size            INTEGER,            -- entries in the list, NULL where it has no end
    likes           INTEGER,            -- the source's own popularity number, NULL where absent
    observed_at     TEXT NOT NULL
)

collection_membership(
    collection_id   TEXT NOT NULL REFERENCES collection(collection_id) ON DELETE CASCADE,
    item_id         TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    rank            INTEGER,            -- 1-based position, NULL where the source is unordered
    mentions        INTEGER,            -- times named, NULL where not a mention source
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (collection_id, item_id)
)
```

**There is no `weight` column, and there will not be one**
([issue #33](https://github.com/McBrideMusings/plex-db-ex/issues/33)). A single number would have
to mean "third of a hundred" for a ranked list, "on a list eight thousand people follow" for a
popular one, and "named in seven comments" for a subreddit — three different quantities — and once
computed it cannot be inverted back into the facts it came from. That is the same objection this
document already raises against a stored `counts_as_signal` above. The store records what the
source said; the consumer weighs it
([ADR-0012](./adr/0012-the-store-records-what-a-source-said-never-a-score-it-computed)).

**Every column past the keys is nullable on purpose.** A source fills what it genuinely has and
leaves the rest empty rather than inventing a value to fill a slot, so a missing `rank` stays
distinguishable from rank 1 and a missing `likes` from zero likes. A reader that defaults these to
`0` destroys exactly the distinction the shape exists to preserve.

**Two tables, because a list's own facts belong to the list.** `size` and `likes` describe the
collection, not one title's place in it; carrying them per membership would repeat them thousands
of times and let two rows disagree about the same list.

**Sources in use:** `mdblist` (`harvest-mdblist`). Plex's own collections are deliberately not
one — see the edges section above. Each source replaces its own rows wholesale on re-pull.

**Membership is a snapshot, not a fact**, the same as edges. On re-pull the whole
`(collection_id)` membership set is deleted and rewritten inside one transaction, so a title the
list dropped disappears rather than going stale. This is not optional tidiness: MDBList marks many
of its lists `dynamic`, meaning the source regenerates them, and appending to one of those would
accumulate every title that had ever passed through it.

**Only titles this store already walked are representable.** `item_id` references `items`, which
`plexdb walk` alone populates (ADR-0005), so a list entry naming a title nobody walked is dropped
and counted, never stored under an invented id
([ADR-0009](./adr/0009-an-edge-only-connects-two-items-this-store-already-knows)). `size` still
records the list's full length, so a consumer can tell it is seeing a twelfth of a list rather
than all of it. Measured on the author's library: 50 lists produced 11,896 memberships and
dropped 15,237 entries as outside it.

**A list entry resolves through the strongest id it carries** — `imdb`, then `tmdb`, then `tvdb`,
the same priority order `derive_item_id` uses, and always scoped by media type. Resolving without
the type would hand a show a movie's identity, since TMDB and TVDB number the two in separate
lists that both start at 1 (see v5 above).

A `collection` row is the staleness cursor for its own list: a fetched list always leaves one
behind even when none of its entries resolved, so this source needs no `enrichment_cursor` row the
way the TMDB sweeps do.

## Version 8 — external_ids records when an id was last observed

[Issue #57](https://github.com/McBrideMusings/plex-db-ex/issues/57). `external_ids` gains
`last_seen`; see the table definition and the note under it near the top of this document.

The column is `NOT NULL`, and SQLite cannot add one of those to a table that already holds rows, so
the migration rebuilds `external_ids` the way v5 did: create the new shape, copy every row across,
drop the old table, rename.

Existing rows are backfilled with the migration's own timestamp, which means "present when this
store was migrated," not "Plex reported this id then." The column starts meaning what it says on
the very next walk: every id Plex still reports gets `last_seen` moved forward, and every id it
stops reporting is left exactly where it was.

Nothing before this schema version could distinguish a GUID Plex had genuinely dropped from one
that just missed a single fetch — a rate limit, a timeout, a partial index. Two walks now answer
that on their own: an id whose `last_seen` predates the current walk was not in the most recent
report.

## Version 9 — Plex TVX's stored title map

`title_map` and `title_map_state`, written by `plexdb refresh-map`. Both are derived from
`enrichment`, so a consumer that reads the keyword facts has no reason to read them.

```
title_map        (kind, item_id) PK, x REAL, y REAL
title_map_state  kind PK, fingerprint TEXT, unplaced INTEGER, computed_at TEXT
```

`title_map` holds the default map of Plex TVX's Map view: each title of `kind`
(`movie` or `show`) with a position in the unit square. Only distances mean anything. It is the
map with no noise tag excluded, drawn from the `tmdb_keywords` rows by IDF-weighted keyword
vectors, truncated SVD to 50 dimensions and UMAP with cosine distance. `item_id` carries no
foreign key: a title that has left `items` drops out of Plex TVX's join, and the next refresh
redraws the map without it.

`title_map_state` holds one row per kind. `fingerprint` names the drawing recipe and a SHA-256
digest of the kind's keyword rows (`item_id`, source, value, `fetched_at`, in key order), so any
row added, removed, re-stamped or re-valued, or a title changing type, makes the stored map stale.
`unplaced` counts titles with keywords that share no tag with another title and so are off the
map. `computed_at` is UTC.

**Refresh rule.** `plexdb refresh-map` recomputes each kind's fingerprint and redraws only a kind
whose stored fingerprint differs, replacing both tables' rows for the redrawn kinds in one
transaction. It runs as its own step of the sweep, after both TMDB steps and before `publish`, so
the snapshot carries a current map. The
digest is SHA-256 over Python's `repr` of the row tuples, so only the Python package can
recompute it; Plex TVX compares it and treats a mismatch as no map, then draws it live, which
takes tens of seconds for the movies. A reader in another language reads the stored map as the
last sweep left it and never recomputes the fingerprint.

## Version 10 — keywords become one source-agnostic namespace

`enrichment` and `enrichment_cursor` both gain `source` in their primary key, and
`keyword_forms(surface, keyword)` is added
([ADR-0016](./adr/0016-keywords-are-one-source-agnostic-namespace-with-a-source-column)). The
migration renames `tmdb_keywords` to `keywords` on both tables, backfilling `source='tmdb'`; any
other namespace present backfills `source` to its own name, there being no other writer to name a
truer one; every keyword value is re-normalized and re-stemmed through `plexdb/keywords.py`'s
shared function, merging any two `enrichment` rows a stemmer collision now unifies (the newer
`fetched_at` wins); and `keyword_forms` is filled from every pre-migration raw value.
`enrichment_cursor` rows never collide when `source` is added — one row per (item_id, namespace,
key) already meant one row per writer — so only `enrichment` can shrink. `plexdb check` fails if
any `tmdb_keywords` row survives it.

**This is the one migration allowed to shrink a guarded table.** `store.migrate`'s row-count guard
normally rolls back any migration that leaves `items`, `plays`, or `enrichment` with fewer rows
than it found — the right call everywhere except here, where merging colliding keyword spellings
is the point. So this step reports the exact `enrichment` row count it computed as a declared
shrink, and the guard accepts a drop in that table only when the store lands on that exact number;
any further loss, from this migration or a future one, still rolls the store back.

## Version 11 — Plex TVX's stored tag network

`tag_network`, `tag_network_edge` and `tag_network_state`, written by `plexdb
refresh-tagnetwork`. All three are derived from `enrichment`, so a consumer that reads the
keyword facts has no reason to read them.

```
tag_network        (kind, value) PK, df INTEGER, x REAL, y REAL
tag_network_edge   (kind, a, b) PK, shared INTEGER
tag_network_state  kind PK, fingerprint TEXT, computed_at TEXT
```

`tag_network` holds the default network of Plex TVX's Graph view: each tag of `kind`
(`movie` or `show`) that clears the Graph view's document-frequency floor, with its document
frequency and a position in the unit square. Only distances mean anything. It is the network
with no noise tag excluded, drawn the same way `title_map` is: IDF-weighted vectors — a tag's
vector is which titles carry it, the transpose of `title_map`'s — truncated SVD to 50
dimensions and UMAP with cosine distance. `tag_network_edge` holds each tag's strongest
co-tags: `a` and `b` (`a < b`, each pair once), and how many titles carry both. Neither table
carries a foreign key: a tag or title that drops out of `enrichment` or `items` drops out of
the next refresh's network without it.

`tag_network_state` holds one row per kind, the same fingerprint shape as `title_map_state`:
the drawing recipe and a SHA-256 digest of the kind's keyword rows. `computed_at` is UTC.

**Refresh rule.** `plexdb refresh-tagnetwork` recomputes each kind's fingerprint and redraws
only a kind whose stored fingerprint differs, replacing all three tables' rows for the redrawn
kinds in one transaction. It runs as its own step of the sweep, right after `refresh-map`, so
the snapshot carries a current network. The digest is built as for
`title_map_state`, so only the Python package can recompute it; Plex TVX compares it and
treats a mismatch as no stored network, then draws it live, which takes tens of seconds for the movies — and only for the
default (no tag excluded) view, since an excluded-tag combination is never precomputed. A reader
in another language never recomputes the fingerprint.

## Version 12 — a judge's verdicts on keyword pairs

`keyword_pairs`, one row per unordered pair of stored keywords a judge has been asked about.
It is the only place the store holds an interpretation of keyword values, and it holds the
judge's answer, not a conclusion ([ADR-0018](./adr/0018-synonym-keywords-are-a-judges-verdicts-beside-enrichment)).
`enrichment` is never rewritten: every keyword a source wrote is still there under the spelling
it wrote.

```
keyword_pairs  (keyword_a, keyword_b) PK, jev_score REAL, jev_model TEXT, judged_at TEXT,
               decision TEXT, decided_at TEXT, jev_error TEXT
```

`keyword_a` and `keyword_b` are values as `enrichment.value` holds them — normalized and
stemmed — with `keyword_a < keyword_b`, so a pair is stored once whichever way round it was
asked. `jev_score` is Jev's answer to "do these two mean the same thing", 0 to 1, stored as Jev
gave it, with the model that gave it in `jev_model`; a pair Jev said no to is stored too, so the
writer never asks about it twice. `judged_at` is UTC. A pair Jev refused with a 400 or 422 has
`jev_score` and `jev_model` NULL and `jev_error` holding the refusal; see Version 13.

`decision` is what a person said about the pair: `accepted`, `rejected`, or NULL while nobody
has. `decided_at` is NULL exactly when `decision` is. A decision outranks the score.

**Which pairs count as merged is the reader's call, not a column.** A reader that wants the
same rule as the Plex TVX page treats a pair as merged when `decision = 'accepted'`, or when
`decision IS NULL AND jev_score >= 0.9`, and as proposed when `decision IS NULL AND jev_score >=
0.5` and below 0.9. A reader that wants 0.7 writes 0.7 over the same rows. Grouping the merged
pairs into sets of keywords is the reader's too: Jev-confirmed pairs chain (`abusive marriage`
reaches `family feud` in a few hops), so connected components over merged pairs is not safe.

**Refresh rule.** A pair is written once and never re-judged: a writer adds rows for pairs it
has not judged before and leaves every existing row, `decision` included, as it is.

`plexdb judge-keyword-pairs` adds the rows. Every run examines every stored keyword, and each
proposes its 10 nearest neighbours in the whole stored vocabulary
with cosine >= 0.75 between `nomic-embed-text` vectors of their shortest readable surface forms
(`keyword_forms.surface`, no task prefix). Jev is asked about the two surface forms, with a
`score` question and a `noul` question ("Do the two tags mean the same thing?") in one request;
`jev_score` is the `noul` answer and `jev_model` is the model id in the response (for example
`jev-1.13.0`). A proposed pair that already has a row is not asked again. Rows are written in
batches of 200 keywords, each batch in one transaction, so a run killed partway leaves every
finished batch behind and the next run asks only about the pairs still without a row. `--limit N`
stops a run after N pairs have been asked about. The step needs
`LLAMA_SWAP_BASE_URL` and `TYPESAFE_API_KEY`; with either unset the sweep reports it skipped.
Nothing records that a keyword was examined, so a keyword that gains a new neighbour proposes the
pair on the next run.

The vectors are cached in `keyword-embeddings.npz` beside the store (not in it): one unit vector
per surface text and the model that made it. A sweep embeds only the texts the cache lacks, so a
night with no new keyword makes no embedding request. Deleting the file, or a change of model,
costs one re-embed of the vocabulary.

### `merge_decisions.json`

A person's decision reaches `keyword_pairs.decision` only through this file, because Plex TVX
never writes `plexdb.db` (ADR-0007, ADR-0017). It sits beside Plex TVX's saved-queries file:
in the directory of `PLEXDB_EXPLORE_SAVED_PATH` when that is set, else beside the published
snapshot when `PLEXDB_SNAPSHOT_PATH` is set, else beside the store. It is a JSON list; every
entry has four text fields:

```json
[
  {"keyword_a": "bank robberi", "keyword_b": "heist", "decision": "accepted",
   "decided_at": "2026-10-01T12:00:00+00:00"}
]
```

`keyword_a` and `keyword_b` are stored values with `keyword_a < keyword_b`. `decision` is
`accepted`, `rejected` or `cleared`; `cleared` sets `decision` and `decided_at` back to NULL.
`decided_at` is a UTC time, required for `accepted` and `rejected`. `plexdb fold-merge-decisions`
applies the file, as the first sweep step after `migrate`, every run: entries apply in list order,
so the last entry for a pair wins, and an entry naming a pair with no row is counted and ignored.
A missing file means no decisions. A file that is not valid JSON or breaks this shape is refused
whole, and no row changes. The file is not deleted, so it stays the record of the latest
decision per pair.

Plex TVX's Merges tab writes this file (`POST /api/merges`, one pair per request, refused with a
404 for a pair `keyword_pairs` has no row for) and reads it back over the table (`GET /api/merges`),
so a decision shows at once and is marked "applies at next sweep" until the fold writes it into the
table. A write keeps one entry per pair, which the fold's last-entry-wins rule makes equivalent.

## Version 13 — a pair the judge refused

Jev answers HTTP 400 or 422 for some pairs, and asking again gets the same answer. Such a pair is
stored like any other, with `jev_error` set to the refusal (`Jev returned 400 for …`) and
`jev_score` and `jev_model` NULL. `jev_error IS NULL` exactly when `jev_score` and `jev_model`
are present. The row keeps the pair from being asked again, so it no longer holds up the batch
it was proposed in.

A reader that filters on `jev_score >= x` already leaves these rows out, because a comparison with
NULL is never true. A reader that selects `jev_score` must expect NULL. Other Jev failures — 429
answered eight times, a 5xx, a rejected key, no connection — write no row and stop the run, because
they say nothing about the pair. To ask about a refused pair again, delete its row.

Plex TVX's Merges tab lists and accepts only scored pairs, so a refused pair never appears there and a decision for one is refused with a 404.

`plexdb judge-keyword-pairs` reports the count as `unjudgeable`.

## Version 14 — a source's tag rank, and keyword roles

`enrichment.rank` is the rank a source gave that row, stored verbatim — AniList's 0–100 vote share
for a tag, for example. It is NULL wherever the source gives none, which is every row written before
v14 and every TMDB keyword. NULL means "this source does not rank", never rank 0.

`keyword_roles` and `keyword_role_decisions` say what role a stored keyword plays. Like
`keyword_pairs`, they hold verdicts beside `enrichment`, never a conclusion
([ADR-0019](./adr/0019-keyword-roles-are-verdicts-beside-enrichment)).

```
keyword_roles           (keyword, role, source) PK, score REAL, model TEXT, error TEXT,
                        stated_at TEXT
keyword_role_decisions  (keyword, role) PK, decision TEXT, decided_at TEXT
```

`role` is one of a closed set, enforced by a `CHECK` in both tables:

| Role | Means |
|---|---|
| `tone` | how a title feels — `bleak`, `whimsic` |
| `era` | when it is set — `1970s`, `victorian era` |
| `region` | where it is set — `los angel`, `tokyo` |
| `theme` | what it is about — `grief`, `reveng` |
| `character_trait` | what its people are like — `antihero`, `genius` |

`keyword` is a value as `enrichment.value` holds it, normalized and stemmed. A row in
`keyword_roles` is one of three things:

- **A role a source states** — Wikidata's narrative location is a `region`. `score`, `model` and
  `error` are NULL.
- **A judge's answer** — `score` is the judge's own 0–1 answer, stored as it gave it, with the
  model id in `model`. A row with a `score` always has a `model`.
- **A judge's refusal** — `error` holds the refusal and `score` is NULL, so the keyword is not
  asked again. `error` and `score` are never both set.

`stated_at` is UTC. `keyword_role_decisions` holds what a person said about one keyword in one
role: `accepted` or `rejected`, with `decided_at`. A decision is per (keyword, role), never per
source, and a keyword nobody has ruled on has no row.

**Whether a keyword has a role is the reader's call, not a column.** A decision outranks every
score: `accepted` means it has the role, `rejected` means it does not. With no decision, a reader
picks its own rule — the query under [Reading the store to build collections](#reading-the-store-to-build-collections)
counts a source-stated row, or a judge's score at or above a threshold.

**Refresh rule.** `enrich-wikidata` writes `region` and `era` rows with `source = 'wikidata'`. A row
is per keyword, not per title, so a title's re-fetch only moves its `stated_at`; a keyword Wikidata
stops calling a place keeps its row until `enrich-wikidata --rewipe`. Nothing writes
`keyword_role_decisions` yet.

## Reading the store to build collections

Each query below was run against a migrated copy of the real store. Parameters are written as
literals; swap in your own. Keyword values are the stored, stemmed ones — look a spelling up in
`keyword_forms` first (`SELECT keyword FROM keyword_forms WHERE surface = 'Los Angeles'`).

**Titles carrying every keyword in a set.** The `HAVING` count is the set's size.

```sql
SELECT i.item_id, i.title, i.year
FROM items i
JOIN enrichment e ON e.item_id = i.item_id
WHERE e.namespace = 'keywords' AND e.key = 'keyword'
  AND e.value IN ('heist', 'bank robberi')
GROUP BY i.item_id
HAVING count(DISTINCT e.value) = 2
ORDER BY i.title;
```

**Keywords that co-occur with one keyword**, counted in titles, not rows, so a keyword two sources
both list counts once. `tag_network_edge` holds a precomputed subset: only each tag's strongest
co-tags.

```sql
SELECT b.value AS co_keyword, count(DISTINCT a.item_id) AS shared
FROM enrichment a
JOIN enrichment b ON b.item_id = a.item_id
                 AND b.namespace = 'keywords' AND b.key = 'keyword'
                 AND b.value <> a.value
WHERE a.namespace = 'keywords' AND a.key = 'keyword' AND a.value = 'heist'
GROUP BY b.value
ORDER BY shared DESC, co_keyword;
```

**An `item_id`'s Plex rating keys.** One title can sit in two sections, so expect more than one
row.

```sql
SELECT rating_key, section_id
FROM plex_items
WHERE item_id = 'imdb:tt0113277';
```

**Keywords in one role at a threshold, with decisions applied.** A source-stated row or a judge's
score at or above 0.8 counts unless a person rejected it; a person's `accepted` counts whatever the
score. Keep `r.error IS NULL`: a refusal has `score` NULL too, and without the filter it reads as a
source-stated role.

```sql
SELECT r.keyword
FROM keyword_roles r
LEFT JOIN keyword_role_decisions d ON d.keyword = r.keyword AND d.role = r.role
WHERE r.role = 'region'
  AND r.error IS NULL
  AND d.decision IS NULL
  AND (r.score IS NULL OR r.score >= 0.8)
UNION
SELECT keyword FROM keyword_role_decisions WHERE role = 'region' AND decision = 'accepted'
ORDER BY keyword;
```

**A title's ratings**, one row per upstream site, as the source reported them. Returns nothing until
a ratings writer has filled the `ratings` namespace.

```sql
SELECT key, value, source
FROM enrichment
WHERE item_id = 'imdb:tt0113277' AND namespace = 'ratings'
ORDER BY key;
```

## Not yet built

Further sources land as new `source` values rather than as schema changes:
[Letterboxd](https://github.com/McBrideMusings/plex-db-ex/issues/35),
[editorial articles and RSS](https://github.com/McBrideMusings/plex-db-ex/issues/37), and
[subreddit mentions](https://github.com/McBrideMusings/plex-db-ex/issues/38) — the last being the
only one that fills `mentions`.

Trakt was to have been one of them
([#36](https://github.com/McBrideMusings/plex-db-ex/issues/36)) and is dropped: Trakt gated API
application creation behind VIP in August 2026, and MDBList already covers crowd lists. Nothing in
the schema recorded that plan, which is the point of `source` being a plain column — a source
arriving or being abandoned costs no migration either way.

Two rules that are easy to break by accident:

- **Namespaces are hard partitions.** A writer may wipe and rewrite only its own rows. Values
  are opaque — the store indexes and serves, it never interprets.
- **Enrich once**, keyed by external id, with `fetched_at`. Never re-fetch a row that exists
  and is inside its threshold. Roughly 30–60 days for external sources; local edges recompute
  free on every sweep and never expire.

## Layer 2

No schema. A per-user weighted attribute vector is recomputed per generation pass and never
stored, because watch history changes constantly and invalidation is more work than
recomputation. Revisit only if profiling says otherwise.
