---
outline: deep
---

# Schema

The SQLite schema **is** the public API of this project. One process writes it; every consumer
opens the same file read-only ([ADR-0001](./adr/0001-one-writer-many-readers-sqlite-file-is-the-interface)).
A migration that changes what a reader reads breaks every reader at once; one that only adds
breaks none of them (see [Reader shape](#reader-shape)).

## Versioning

The store carries its version in a `schema_version` table. Migrations are an append-only list
in `plexdb/schema.py`: index 0 takes an empty database to version 1, index 1 takes it to
version 2, and so on. A shipped migration is never edited — another is added.

Opening a store whose version is **higher** than the running build understands is refused
outright, because a newer writer may have added rows this build cannot see. A store carrying a
`schema_version` table with no row is reported as damaged rather than treated as empty.

### Reader shape

The store also carries a `reader_shape` table (version 15 on): one row naming the last schema
version that changed what a reader reads. It is 10 today. `plexdb-reader` opens a store only when
`reader_shape` equals its `SUPPORTED_READER_SHAPE`, whatever `schema_version` says, so a deployed
reader keeps reading a snapshot from a newer writer that has only added tables or columns.

A migration that renames or drops something a reader selects, moves rows between namespaces, or
changes what a row means ends with `UPDATE reader_shape SET version = <its version>;` and sets
`READER_SHAPE` in `plexdb/schema.py` to match, in the same change that updates `plexdb-reader`.
Every reader built before it then refuses the store instead of misreading it. A migration that
only adds leaves both alone. `tests/test_schema.py` fails when `READER_SHAPE` and the reader's
constant disagree.

**Versions 1 through 16 are live.** Everything under "Not yet built" is the target for later
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
-- normalized value stored above it.
CREATE TABLE keyword_forms (
    surface TEXT PRIMARY KEY,
    keyword TEXT NOT NULL
);

-- Each keyword exactly as a source sent it for a title (v17). The keyword
-- rows in `enrichment` are derived from these.
CREATE TABLE keyword_surfaces (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    source     TEXT NOT NULL,
    surface    TEXT NOT NULL,
    rank       INTEGER,
    spoiler    INTEGER NOT NULL CHECK (spoiler IN (0, 1)),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, source, surface)
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

**Every keyword is normalized before it lands here** (`plexdb/keywords.py`): lowercase;
`’` becomes `'`; whitespace and every Unicode dash or connector character (categories `Pd` and `Pc`:
`-`, `‑`, `–`, `—`, `_`) split words; each word loses the punctuation at its start and end (any
Unicode `P*` character except `#` and `%`, so `quirky,`, `(soccer)` and `st.` become `quirky`,
`soccer` and `st`, `c#` and `100%` stay, and a lone `&` or `/` drops out), together with any
invisible format character (category `Cf`, such as a zero-width space) at those edges and any
combining mark (category `M*`) sitting on that punctuation or leading the word with no character to
sit on — so `heists` followed by a zero-width space and a comma becomes `heist`, and `café.` with an
acute accent on the full stop becomes `café`. A format character inside a word stays, such as the
zero-width non-joiner in Persian `کتاب‌ها`; words are joined with one
space, and a word ending in "s" becomes its singular — simplemma's English lemma, taken only when it
is shorter than the word — unless it is in `keywords.NEVER_FOLD` (`mars`, `alps`, `wales`, `las`,
`arms`, `states`, …). No other inflection changes: `christmas`, `boxing`, `murderer` and `racing`
stay themselves. Punctuation inside a word stays (`9/11`, `u.s`), and a symbol such as `+` is not
punctuation. A surface left with no word (`&`, `...`) is no keyword: no writer stores a row for it
and `keyword_forms` gets no row for it. `Heists`, `heist`, `bank-heist`/`bank heist` and
Letterboxd's `Dreamlike, quirky` beside a plain `dreamlike quirky` each resolve to one stored
value, so a consumer scoring by keyword-cosine sees one confirmed signal instead of unrelated
near-misses. `keyword_forms` is what lets a reader still show the spelling a source actually used —
every keyword write upserts a row mapping its raw `surface` to the `keyword` value stored in
`enrichment`.

**The raw spellings are the facts; the keyword rows are derived from them.** A writer replaces a
title's `keyword_surfaces` rows for its source, one per distinct spelling (two copies keep the
higher rank, and are a spoiler if either is), and the `enrichment` rows follow: one per stored
value, with the highest rank, the newest `fetched_at`, and key `spoiler_keyword` when any spelling
that folded into it is a spoiler. A change to this rule is a migration that re-derives every
keyword row from `keyword_surfaces` (`keywords.rederive_keywords`) — nothing is fetched. `keyword_pairs`,
`keyword_roles` and `keyword_role_decisions` rows are keyed on the stored value. `plexdb
prune-keyword-verdicts` deletes every such row keyed on a value no `enrichment` row in namespace
`keywords` carries under key `keyword` or `spoiler_keyword`, after the keyword writers and before the judges in each sweep, so a value a
re-fetch moved loses its rows the same night and the new value is judged in the steps that follow.
A pair goes when either of its keywords is gone. A value absent only for a while — a `--rewipe`
whose re-fetch has not finished — loses its rows too, and Jev is asked about it again once it
returns. A person's decision is deleted with its rows and stays in its decisions file
([`merge_decisions.json`](#merge-decisions-json), [`role_decisions.json`](#role-decisions-json)),
which the fold re-applies on every sweep: a role decision comes back once the value is judged
again, a pair decision once `judge-keyword-pairs` proposes and judges that pair again.

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
| `keywords` | `tmdb` | `plexdb enrich-tmdb-keywords` | `keyword`, one row per normalized keyword, `value` is the stored keyword text (see `keyword_forms` above for the raw spelling). Nothing else. Only `items.type` `movie` and `show` are enriched — TMDB has no keywords endpoint for an episode. |
| `keywords` | `wikidata` | `plexdb enrich-wikidata` | `keyword`, stored the same way: the English labels of the title's Wikidata narrative location (P840), set in period (P2408), main subject (P921) and genre (P136). Movies and shows with an `imdb` external id only. A P840 keyword also gets a `region` row in `keyword_roles`, a P2408 keyword an `era` row, and the title a matching row in `keyword_role_statements`. |
| `keywords` | `anilist` | `plexdb enrich-anilist` | `keyword`, stored the same way: the title's AniList tags, with AniList's 0–100 rank (the share of voters who agree the tag applies) in `rank`, verbatim. `spoiler_keyword`, the same for a tag AniList flags `isMediaSpoiler` or `isGeneralSpoiler`, so a reader of key `keyword` never sees a spoiler. Anime movies and shows only, found through Fribb's mapping (see [AniList](#anilist-tags-and-roles) below). |
| `keywords` | `letterboxd` | `plexdb enrich-letterboxd` | `keyword`, stored the same way: every theme and mini-theme label in the Themes section of the film's Letterboxd page (up to about seven — the page shows a subset), with `rank` NULL, because Letterboxd ranks nothing. No `keyword_roles` row. Movies with a `tmdb` external id only (see [Letterboxd themes](#letterboxd-themes) below). |
| `awards` | `wikidata` | `plexdb enrich-wikidata` | `award`, one row per English label of the title's award received (P166), stored verbatim — not normalized, and not a keyword. |
| `ratings` | `mdblist` | `plexdb enrich-mdblist-ratings` | One key per site MDBList relays — `imdb`, `tmdb`, `trakt`, `letterboxd`, `tomatoes` (Rotten Tomatoes critics), `popcorn` (Rotten Tomatoes audience), `metacritic`, `metacriticuser`, `rogerebert`, `myanimelist` — whose `value` is the site's own number on the site's own scale, as MDBList's JSON carried it (`7.9` from IMDb, `84` from Metacritic, `4.0` from RogerEbert). `<site>_votes` beside it holds the vote count where one is given. A site with a null value writes no row, its votes included. Nothing is converted to one scale or combined (ADR-0012); MDBList's own 0–100 `score` is not stored. `rank` NULL. Movies and shows with an `imdb` external id only (see [MDBList ratings](#mdblist-ratings) below). |

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
| `keywords` | `anilist` | `fetched` | `enrich-anilist`, one per anime title it asked about, written even when AniList had no tags; a title that is not anime gets none |
| `keywords` | `letterboxd` | `fetched` | `enrich-letterboxd`, one per movie whose film page parsed, written even when it showed no themes, and one per movie Letterboxd does not list; a film page without the film marker gets none |
| `keywords` | `letterboxd` | `attempted` | `enrich-letterboxd`, when a movie's film page had no film marker or its request failed: scheduling only, never freshness. The next run asks never-attempted movies first, then these, oldest attempt first, so a movie that keeps failing never holds the per-run cap. A successful fetch deletes it |
| `ratings` | `mdblist` | `fetched` | `enrich-mdblist-ratings`, one per title in a batch that answered, written even when MDBList left the title out of its answer or had no non-null rating |
| `ratings` | `mdblist` | `attempted` | `enrich-mdblist-ratings`, for every title in a batch whose request failed: scheduling only, never freshness, ordered and cleared the same way as Letterboxd's |
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

`enrich-anilist` asks again about an anime title only once its cursor is older than
`ANILIST_STALE_DAYS` (default 45 days), and replaces that title's `anilist` rows when it does. A
title that drops out of the mapping keeps its rows, and a mapping with no AniList ids at all fails
the run. `--rewipe` deletes every `source = 'anilist'` row in `keywords`, `keyword_roles` and
`enrichment_cursor`, and nothing else.

`enrich-letterboxd` asks again about a movie only once its cursor is older than
`LETTERBOXD_STALE_DAYS` (default 45 days), and replaces that movie's `letterboxd` rows when it does.
One run fetches at most `LETTERBOXD_MAX_TITLES` movies (default 1,000; `--limit N` overrides):
never-attempted movies first in `item_id` order, then those with an `attempted` cursor, oldest
first. The first pass over the library therefore spreads across several sweeps. `--rewipe`
deletes every `source = 'letterboxd'` row in `keywords` and `enrichment_cursor`, and nothing else.

`enrich-mdblist-ratings` asks again about a title only once its cursor is older than
`MDBLIST_RATINGS_STALE_DAYS` (default 45 days), and replaces that title's `ratings` rows when it
does. One run sends at most `MDBLIST_RATINGS_MAX_REQUESTS` requests (default 100; `--limit N`
overrides), because the daily quota counts requests and `harvest-mdblist` draws on the same one.
Titles are ordered as Letterboxd's are, then cut into batches per media type. `--rewipe` deletes
every `source = 'mdblist'` row in `ratings` and `enrichment_cursor`, and nothing else —
`harvest-mdblist`'s collections are untouched.

### MDBList ratings

`enrich-mdblist-ratings` sends `POST /imdb/movie/` or `/imdb/show/` with up to 200 IMDb ids, and
one request costs one against the daily quota whatever its size (1,000 a day on MDBList's free
tier). MDBList leaves an id it does not know out of the answer, and that title is cached with no
rows. On a pulled copy (2026-10-05) 13,312 of the 14,429 movies and shows had an IMDb id; 68
requests covered all of them in about 30 seconds, writing 94,844 ratings and 87,652 vote counts.
A failed request writes nothing for its batch, and three in a row abort the run. A 429 means the
day's quota is spent: the run stops at once, marks nothing, prints why, and exits non-zero.

```sql
-- Every rating one title has, on each site's own scale
SELECT key, value FROM enrichment
WHERE item_id = ? AND namespace = 'ratings' AND source = 'mdblist';

-- Titles IMDb rates 8 or higher on at least 10,000 votes
SELECT r.item_id, CAST(r.value AS REAL) AS imdb
FROM enrichment r
JOIN enrichment v ON v.item_id = r.item_id AND v.namespace = 'ratings'
                 AND v.source = 'mdblist' AND v.key = 'imdb_votes'
WHERE r.namespace = 'ratings' AND r.source = 'mdblist' AND r.key = 'imdb'
  AND CAST(r.value AS REAL) >= 8 AND CAST(v.value AS INTEGER) >= 10000;
```

### Letterboxd themes

Letterboxd has no public API for individuals, so `enrich-letterboxd` reads the film page.
`letterboxd.com/tmdb/<tmdb id>/` redirects to the film's page (`/tmdb/27205/` → `/film/inception/`);
an id Letterboxd has no film for answers a 200 "TMDB Import Result" page instead, and that movie
is cached as not listed, with no keywords. The film page's `<body>` carries `data-tmdb-id`. A page
where it is missing or names another id, or that carries theme links but none under the `Themes` heading (a renamed heading), is a **parse failure**: no keyword and no `fetched`
cursor is written, only an `attempted` one, so the next run asks again, and a run whose parse failures exceed 10% of the film pages
it loaded exits non-zero after printing the count. The Themes section mixes Letterboxd's broad
themes (`/films/theme/…`) and its narrower mini-themes (`/films/mini-theme/…`); both are stored. A film page with no theme links at all is listed with no themes.
The full list behind "Show All…" sits behind a Cloudflare challenge, so it is not read. Every
request is checked against the `User-agent: *` rules in robots.txt, fetched once per run.

### AniList tags and roles

AniList carries no TMDB, IMDb or TVDB id. `enrich-anilist` reaches it through Fribb's
[`anime-list-full.json`](https://github.com/Fribb/anime-lists), fetched whole on each run: a title
maps to every AniList id its TMDB id names (matched on movie vs. show), else its IMDb id, else — for
a show — its TVDB id. AniList lists each season as its own entry, so a show often maps to several;
their tags merge into the one title. A tag keeps its **highest** rank across those entries, and it
is stored under `spoiler_keyword` if **any** entry flags it as a spoiler, so a spoiler for one
season never reaches key `keyword`.

A tag's AniList category states a role, written to `keyword_roles` with `source = 'anilist'` and
score, model and error NULL. A category matches its own row or, failing that, its nearest
`-`-separated parent — `Theme-Other-Organisations` falls back to `Theme-Other`, then `Theme`:

| AniList category | Role |
|---|---|
| `Theme` and every `Theme-*` | `theme` |
| `Setting-Time` | `era` |
| `Cast-Traits` | `character_trait` |

Every other category — `Cast-Main Cast`, `Setting-Scene`, `Setting-Universe`, `Demographic`,
`Technical`, `Sexual Content` — states no role. A spoiler tag states its role like any other: the
role describes the keyword, not the title.

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
Schema v21 adds `collection_entry`, every entry of every list, and `collection.provenance`.

```sql
collection(
    collection_id   TEXT PRIMARY KEY,   -- "mdblist:14" — source-namespaced, opaque
    source          TEXT NOT NULL,      -- "plex", "mdblist", "letterboxd", "editorial", "reddit"
    name            TEXT,
    url             TEXT,
    size            INTEGER,            -- entries in the list, NULL where it has no end
    likes           INTEGER,            -- the source's own popularity number, NULL where absent
    observed_at     TEXT NOT NULL,
    provenance      TEXT NOT NULL DEFAULT 'feed',  -- how the list arrived: 'feed', 'added', 'search'
    seed_count      INTEGER             -- seed titles a 'search' list was found from, else NULL
)

collection_entry(                       -- v21: every entry of the list, owned or not (ADR-0027)
    collection_id   TEXT NOT NULL REFERENCES collection(collection_id) ON DELETE CASCADE,
    rank            INTEGER,            -- 1-based position, NULL where the source is unordered
    ns              TEXT NOT NULL,      -- the strongest external id the source gave: "imdb"
    value           TEXT NOT NULL,      --   "tt1375666"
    kind            TEXT NOT NULL,      --   "movie" or "show", scoping the id as in external_ids
    title           TEXT,               -- the source's own title, NULL where it sent none
    year            INTEGER,            -- the source's own release year, NULL where it sent none
    PRIMARY KEY (collection_id, ns, value, kind)
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

**A reviewer refuses a proposed score, confidence, strength, relevance or normalised 0–1 column
whose value cannot be traced back to a single thing a source said.** A value like that is computed
from inputs, and either the inputs are in the store or they are not. If they are, the reader
computes the value at read time, and a stored copy goes stale the moment the formula changes. A
`weight` column would fail this way, because `rank`, `likes` and `mentions` are all stored. If the
inputs are not in the store, storing the output hides that they are missing: the reader sees a
number and cannot tell which facts produced it, or that the store holds none of them. A score the
source publishes passes, because it is one thing the source said. MDBList's `imdb` rating of `7.9`
is IMDb's own number. A judge's answer passes too: ADR-0012 counts a judge such as Jev as a
source, so `keyword_pairs.jev_score` and `keyword_roles.score` are each one thing the judge said,
stored with its model.

**Every column past the keys is nullable on purpose.** A source fills what it genuinely has and
leaves the rest empty rather than inventing a value to fill a slot, so a missing `rank` stays
distinguishable from rank 1 and a missing `likes` from zero likes. A reader that defaults these to
`0` destroys exactly the distinction the shape exists to preserve.

**Two tables, because a list's own facts belong to the list.** `size` and `likes` describe the
collection, not one title's place in it; carrying them per membership would repeat them thousands
of times and let two rows disagree about the same list.

**Sources in use:** `mdblist` (`harvest-mdblist`) and `letterboxd` (`harvest-letterboxd-lists`). Plex's own collections are deliberately not
one — see the edges section above. Each source replaces its own rows wholesale on re-pull.

**Membership is a snapshot, not a fact**, the same as edges. On re-pull the whole
`(collection_id)` membership set is deleted and rewritten inside one transaction, so a title the
list dropped disappears rather than going stale. This is not optional tidiness: MDBList marks many
of its lists `dynamic`, meaning the source regenerates them, and appending to one of those would
accumulate every title that had ever passed through it.

**Membership holds only titles this store already walked.** `item_id` references `items`, which
`plexdb walk` alone populates (ADR-0005), so a list entry naming a title nobody walked has no
membership row and is never stored under an invented id
([ADR-0009](./adr/0009-an-edge-only-connects-two-items-this-store-already-knows)). Measured on the
author's library: 50 lists produced 11,896 memberships and left 15,237 entries outside the library.

**`collection_entry` keeps every entry, owned or not, and carries no `item_id`**
([ADR-0027](./adr/0027-an-unowned-list-entry-is-stored-by-external-id)). A reader resolves an entry
to a title through `external_ids` on `(ns, value, kind)` when it reads, so a title walked after the
harvest joins its lists with nothing rewritten. `(ns, value, kind)` is the first id the source gave in
`identity.PRIORITY` order. An id listed twice on one list is stored once, at its first position.
`size` records the list's full length, so `size` against the stored entry count shows whether the
source returned every entry.

**A list's entries and memberships are replaced together.** `harvest-mdblist` and `harvest-letterboxd-lists` delete both for the
list and write both, with the `collection` row, in one transaction, so the three never describe two
different fetches. A list counts as cached only when `observed_at` is inside the staleness window
*and* it has `collection_entry` rows; a list stored before v21 has none, so the first harvest after
the migration fetches it again.

**`provenance` says how a list arrived**: `feed` for a source's own top lists (everything
`harvest-mdblist` writes from MDBList's feed, and every list stored before v21), `added` for a list a person named
([Version 22](#version-22-a-person-s-decisions-about-crowd-lists)), and `search` for a list found
by `harvest-mdblist-search` from watched titles, with `seed_count` the number of the list's library titles that
have been watched. All three are written.

**Nothing derived is stored** (ADR-0012). `plexdb lists` and `plexdb list` (`plexdb/lists.py`, the
functions the explorer's Lists tab calls) work out the owned count, owned share, watched count (owned
entries with at least one play, a show by a play of any of its episodes) and a list's over-represented keywords from these tables at read
time. An entry is owned when its id resolves through `external_ids`, or failing that when the
harvest wrote a membership at the same rank, because the harvest tries every id an entry carries
and the entry row keeps only the strongest. A keyword is named for a list when at least 3 of its
owned titles carry it and its share of them is at least 3 times its share of the library's titles of
the list's most common type. `plexdb check` prints, per source, the lists, the entries stored and the
owned share, and the lists whose fetch is failing. The list table also carries each list's last
`decision`, its `failure` (`attempts`, `attempted_at`, `reason`, `stopped`) and a `state` of `ok`,
`failed` or `waiting`; a list added or failing before it was ever stored has a row of its own with no
`collection_id`.

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

## Version 9 — Plex DB-EX's stored title map

`title_map` and `title_map_state`, written by `plexdb refresh-map` together with the map's region
labels ([Version 20](#version-20-the-stored-title-map-s-region-labels)). All are derived from
`enrichment`, so a consumer that reads the keyword facts has no reason to read them.

```
title_map        (kind, item_id) PK, x REAL, y REAL
title_map_state  kind PK, fingerprint TEXT, unplaced INTEGER, computed_at TEXT
```

`title_map` holds the default map of Plex DB-EX's Map view: each title of `kind`
(`movie` or `show`) with a position in the unit square. Only distances mean anything. It is the
map with no noise tag excluded, drawn from the `keywords` rows, merges folded, by IDF-weighted keyword
vectors laid out whole by UMAP with cosine distance (`min_dist` 0.1). `item_id` carries no
foreign key: a title that has left `items` drops out of Plex DB-EX's join, and the next refresh
redraws the map without it.

`title_map_state` holds one row per kind. `fingerprint` names the drawing recipe, a SHA-256
digest of the kind's keyword rows (`item_id`, source, value, `fetched_at`, in key order), and a
SHA-256 digest of the merge fold, so any row added, removed, re-stamped or re-valued, a title
changing type, or a new merge verdict makes the stored map stale.
`unplaced` counts titles with keywords that share no tag with another title and so are off the
map. `computed_at` is UTC.

**Refresh rule.** `plexdb refresh-map` recomputes each kind's fingerprint and redraws only a kind
whose stored fingerprint differs, replacing the redrawn kinds' rows in both tables and in
`title_map_label` in one transaction. It runs as its own step of the sweep, after every keyword writer, the film-suffix rule
and both judges, and before `publish`, so the snapshot carries a map of that night's merges. The
digest is SHA-256 over Python's `repr` of the row tuples, so only the Python package can
recompute it; Plex DB-EX compares it and treats a mismatch as no current map, then draws it live,
which takes tens of seconds for the movies, showing the stale stored map dimmed until the live
one lands. A reader in another language reads the stored map as the
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

## Version 11 — Plex DB-EX's stored tag network

`tag_network`, `tag_network_edge` and `tag_network_state`, written by `plexdb
refresh-tagnetwork`. All three are derived from `enrichment`, so a consumer that reads the
keyword facts has no reason to read them.

```
tag_network        (kind, value) PK, df INTEGER, x REAL, y REAL
tag_network_edge   (kind, a, b) PK, shared INTEGER
tag_network_state  kind PK, fingerprint TEXT, computed_at TEXT
```

`tag_network` holds the default network of Plex DB-EX's Graph view: each tag of `kind`
(`movie` or `show`) that clears the Graph view's document-frequency floor, with its document
frequency and a position in the unit square. Only distances mean anything. It is the network
with no noise tag excluded, drawn the same way `title_map` is: IDF-weighted vectors — a tag's
vector is which titles carry it, the transpose of `title_map`'s — laid out whole by UMAP with
cosine distance. `tag_network_edge` holds each tag's strongest
co-tags: `a` and `b` (`a < b`, each pair once), and how many titles carry both. Neither table
carries a foreign key: a tag or title that drops out of `enrichment` or `items` drops out of
the next refresh's network without it.

`tag_network_state` holds one row per kind, the same fingerprint shape as `title_map_state`:
the drawing recipe and a SHA-256 digest of the kind's keyword rows. `computed_at` is UTC.

**Refresh rule.** `plexdb refresh-tagnetwork` recomputes each kind's fingerprint and redraws
only a kind whose stored fingerprint differs, replacing all three tables' rows for the redrawn
kinds in one transaction. It runs as its own step of the sweep, right after `refresh-map`, so
the snapshot carries a current network. The digest is built as for
`title_map_state`, so only the Python package can recompute it; Plex DB-EX compares it and
treats a mismatch as no stored network, then draws it live, which takes tens of seconds for the movies — and only for the
default (no tag excluded) view, since an excluded-tag combination is never precomputed. A reader
in another language never recomputes the fingerprint.

## Version 12 — a judge's verdicts on keyword pairs

`keyword_pairs`, one row per unordered pair of stored keywords a judge has been asked about or a
person has decided (Version 18).
It is the only place the store holds an interpretation of keyword values, and it holds the
judge's answer, not a conclusion ([ADR-0018](./adr/0018-synonym-keywords-are-a-judges-verdicts-beside-enrichment)).
`enrichment` is never rewritten: every keyword a source wrote is still there under the spelling
it wrote.

```
keyword_pairs  (keyword_a, keyword_b) PK, jev_score REAL, jev_model TEXT, judged_at TEXT,
               decision TEXT, decided_at TEXT, jev_error TEXT
```

`keyword_a` and `keyword_b` are values as `enrichment.value` holds them — normalized — with
`keyword_a < keyword_b`, so a pair is stored once whichever way round it was
asked. `jev_score` is Jev's answer to "do these two mean the same thing", 0 to 1, stored as Jev
gave it, with the model that gave it in `jev_model`; a pair Jev said no to is stored too, so the
writer never asks about it twice. `judged_at` is UTC. A pair Jev refused with a 400 or 422 has
`jev_score` and `jev_model` NULL and `jev_error` holding the refusal; see Version 13. A pair only a
person decided has `judged_at`, `jev_score`, `jev_model` and `jev_error` all NULL; see Version 18.

`decision` is what a person said about the pair: `accepted`, `rejected`, or NULL while nobody
has. `decided_at` is NULL exactly when `decision` is. A decision outranks the score.

**Which pairs count as merged is the reader's call, not a column.** A reader that wants the
same rule as the Plex DB-EX page treats a pair as merged when `decision = 'accepted'`, or when
`decision IS NULL AND jev_score >= 0.9`, and as proposed when `decision IS NULL AND jev_score >=
0.5` and below 0.9. A reader that wants 0.7 writes 0.7 over the same rows. Grouping the merged
pairs into sets of keywords is the reader's too: Jev-confirmed pairs chain (`abusive marriage`
reaches `family feud` in a few hops), so connected components over merged pairs is not safe.

**Refresh rule.** A pair is written once and never re-judged: a writer adds rows for pairs it
has not judged before and leaves every existing row, `decision` included, as it is. `plexdb
prune-keyword-verdicts` deletes a pair, `decision` included, once either keyword is a value no
title carries (the normalization rule under Version 1 says when that happens).

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
`LLAMA_BROKER_BASE_URL` and `TYPESAFE_API_KEY`; with either unset the sweep reports it skipped.
Nothing records that a keyword was examined, so a keyword that gains a new neighbour proposes the
pair on the next run.

`plexdb film-suffix-verdicts` runs just before the judge and adds rule rows: for each stored value
"X film" whose bare "X" is also stored, a pair with `jev_score` 1.0 and `jev_model`
`rule:film-suffix` (ADR-0018), replacing any score Jev gave it and keeping any `decision`. Bare
values in `keywords.FILM_SUFFIX_KEEP` (`art`, `essay`, `race`, `student`) are never paired, because
their "film" form means something else. A rule row the rule no longer holds for is deleted unless a person has decided it, so the
judge asks about it again.

**What counts as merged, and who applies it.** A pair is merged when `decision = 'accepted'`, or
`decision` is NULL and `jev_score >= 0.9`. Merged pairs chain into groups, and each group folds into
the member the most titles carry (`merge_review.merge_map`). Plex DB-EX's tag table, a tag's
titles, the title map, its region labels and the tag network all read keywords through that fold
(`explore.keyword_rows`); `enrichment` itself never changes.

The vectors are cached in `keyword-embeddings.npz` beside the store (not in it): one unit vector
per surface text and the model that made it. A sweep embeds only the texts the cache lacks, so a
night with no new keyword makes no embedding request. Deleting the file, or a change of model,
costs one re-embed of the vocabulary.

### `merge_decisions.json`

A person's decision reaches `keyword_pairs.decision` only through this file, because Plex DB-EX
never writes `plexdb.db` (ADR-0007, ADR-0017). It sits beside Plex DB-EX's saved-queries file:
in the directory of `PLEXDB_EXPLORE_SAVED_PATH` when that is set, else beside the published
snapshot when `PLEXDB_SNAPSHOT_PATH` is set, else beside the store. It is a JSON list; every
entry has four text fields:

```json
[
  {"surface_a": "Bank Robbery", "surface_b": "heists", "decision": "accepted",
   "decided_at": "2026-10-01T12:00:00+00:00"}
]
```

`surface_a` and `surface_b` are two different raw spellings (`keyword_forms.surface`), one of each
keyword, never stored values. Every reader resolves an entry with the current `normalize_keyword`:
the pair is the two values its spellings normalize to now, in `keyword_a < keyword_b` order, so
the two spellings may sit in either order. A
migration that changes normalization renames stored values but never a spelling, so a decision
follows its keyword to the new value and nothing rewrites the file. An entry whose two spellings now
normalize to one value names no pair and is unmatched. `decision` is
`accepted`, `rejected` or `cleared`; `cleared` sets `decision` and `decided_at` back to NULL.
`decided_at` is a UTC time, required for `accepted` and `rejected`. `plexdb fold-merge-decisions`
applies the file, as the first sweep step after `migrate`, every run: entries apply in list order,
so the last entry for a pair wins. An `accepted` or `rejected` entry for a pair with no row, whose
two keywords are both stored values, inserts a row holding only the decision (Version 18); `cleared`
deletes such a row. An entry naming a value no title carries is counted as unmatched and ignored.
A missing file means no decisions. A file that is not valid JSON or breaks this shape is refused
whole, and no row changes. The file is not deleted, so it stays the record of the latest
decision per pair: a decision whose row `prune-keyword-verdicts` deleted comes back on the first
fold after the pair is judged again, which happens only if one of its keywords proposes the other
among its nearest neighbours.

Plex DB-EX's Merges tab and tag doctor write this file (`POST /api/merges`, one pair per request,
refused with a 404 for a pair that has no row and names a value no title carries; the request
names stored values, and the server writes each one's shortest spelling that normalizes back to it, trying
the longer spellings only when the shortest does not, and refusing with a 400 when none does) and read it back
over the table (`GET /api/merges`, `GET /api/doctor?item_id=`),
so a decision shows at once and is marked "applies at next sweep" until the fold writes it into the
table. A write keeps one entry per resolved pair, which the fold's last-entry-wins rule makes
equivalent. The Roles tab's [`role_decisions.json`](#role-decisions-json) has the same shape, keyed
by (spelling, role).

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

Plex DB-EX's Merges tab lists a refused pair only once a person has decided it, with no score.

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

`keyword` is a value as `enrichment.value` holds it, normalized. A row in
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
is per keyword, not per title, so a title's re-fetch only moves its `stated_at`. Every run then
deletes each `wikidata` role row that no `keyword_role_statements` row backs, meaning no stored
title states that keyword in that role any more. It judges every stored title, fetched this run or
not, and runs even when the sweep aborts. A keyword Wikidata stops calling a place or a period on
every title loses its row, even while some title still lists it as a main subject or genre.

`enrich-anilist` writes `theme`, `era` and `character_trait` rows with `source = 'anilist'` from
tag categories ([AniList tags and roles](#anilist-tags-and-roles)). It writes no
`keyword_role_statements` rows and prunes nothing on a run: an `anilist` role row stays until
`enrich-anilist --rewipe` or `plexdb prune-keyword-verdicts` removes it.

Only `plexdb fold-role-decisions` writes `keyword_role_decisions`, from
[`role_decisions.json`](#role-decisions-json). `plexdb prune-keyword-verdicts` deletes every row in
both tables, whatever its source, keyed on a value no title carries.

`plexdb judge-keyword-roles` writes judge rows with `source = 'jev'`. It asks Jev about every
distinct stored keyword that has no `jev` row: one request per keyword, about its shortest readable
surface form (`keyword_forms.surface`), carrying one `noul` question per role ("The tag names a
place a title is set in, such as …"). Each answer becomes one row, five per keyword, with `score`
exactly as Jev gave it and `model` the model id in the response (for example `jev-1.13.0`). A
keyword with a `jev` row is never asked again, so a run with no new keyword sends nothing; another
source's row does not count. A keyword Jev refuses with a 400 or 422 gets five rows with `error`
holding the refusal and `score` and `model` NULL, and is not asked again; delete its rows to ask
again. Other Jev failures write nothing for the batch and stop the run. Rows are written in batches
of 200 keywords, each in one transaction, so a killed run keeps every finished batch. `--limit N`
asks about at most N keywords. The step needs `TYPESAFE_API_KEY`; with it unset the sweep reports it
skipped.

### `role_decisions.json`

A person's decision reaches `keyword_role_decisions` only through this file, for the same reason as
[`merge_decisions.json`](#merge-decisions-json), and it sits in the same directory. It is a JSON
list; every entry has four text fields:

```json
[
  {"surface": "Los Angeles", "role": "region", "decision": "accepted",
   "decided_at": "2026-10-05T12:00:00+00:00"}
]
```

`surface` is a raw spelling of the keyword, resolved with the current `normalize_keyword` exactly as
in `merge_decisions.json`, and `role` one of the five roles. `decision` is `accepted`, `rejected`
or `cleared`; `decided_at` is a UTC time, required for `accepted` and `rejected`. `plexdb
fold-role-decisions` applies the file as the sweep step right after `fold-merge-decisions`, every
run: entries apply in list order, so the last entry for a (keyword, role) wins. `accepted` and
`rejected` set the row's `decision` and `decided_at`; `cleared` deletes the row, so both read NULL.
An entry naming a (keyword, role) with no verdict in `keyword_roles` (a refusal does not count) is
counted and ignored. A missing file means no decisions. A file that is not valid JSON or breaks this
shape is refused whole, and no row changes. The file is not deleted, so a decision whose row
`prune-keyword-verdicts` deleted comes back on the first fold after the keyword is judged again.

Plex DB-EX's Roles tab writes this file (`POST /api/roles`, one (keyword, role) per request,
refused with a 404 when `keyword_roles` has no verdict for it) and reads it back over the table
(`GET /api/roles`), so a decision shows at once — the table outlines the pressed button, the
decisions list marks it "applies at next sweep" — until the
fold writes it. A write keeps one entry per resolved (keyword, role).

## Version 16 — which title states a role

```
keyword_role_statements  (item_id, keyword, role, source) PK, stated_at TEXT
```

One row per role a source states for one title: `enrich-wikidata` writes `(title, 'pari',
'region', 'wikidata')` for a title whose narrative location (P840) is Paris, and `era` for a set in
period (P2408). `keyword` is stored as `enrichment.value` holds it, and `role` is the closed set
`keyword_roles` uses. A source's fetch of a title replaces that title's rows, so the table says
which titles state a role today; `keyword_roles` keeps one row per keyword, and the writer's prune
deletes a role row no statement backs (see the refresh rule under Version 14).

It sits outside `enrichment` on purpose. A reader rolls every `enrichment` row into a title's
taste, so a second row per place there would count the place twice and shrink every other
attribute of the title. No reader reads this table, so `reader_shape` stays 10.

No stored title had a row before v16, so the migration also deletes every `enrichment_cursor` row
with `namespace = 'keywords'` and `source = 'wikidata'`: the next run fetches every title again and
writes them, instead of its prune deleting every Wikidata role. A title's Wikidata keywords stay in
place until that re-fetch replaces them. A run that fails partway leaves the roles of the titles it
did not reach pruned until a later run fetches them.

## Version 17 — keywords are folded plurals derived from raw spellings

`keyword_surfaces` is added (see Version 1), and every keyword value changes from a Snowball stem to
the rule above: `christma` becomes `christmas`, `documentari film` becomes `documentary film`.
`enrichment` held only stems, so the migration recovers each row's spelling from `keyword_forms`.
A stem whose spellings all normalize to one value takes that value's shortest spelling. A stem
whose spellings now normalize apart (`anim` ← animal, animation, anime) cannot say which one each
row came from, so its rows keep the stem as their surface and value, and the source's
`enrichment_cursor` row for that title is deleted: the next sweep fetches the title again and
replaces them with what the source sent. On the 2026-10-09 store that is 8,309 TMDB titles and 407
AniList titles. `keyword_pairs`, `keyword_roles`, `keyword_role_decisions` and
`keyword_role_statements` rows move to the new value where the stem maps to exactly one; the rest
stay until `prune-keyword-verdicts` finds their value gone. Two stems that land on one value on the
same title merge, so `enrichment` may shrink by the count the migration declares. Values are still
looked up by spelling through `keyword_forms`, so `reader_shape` stays 10. `idx_enrichment_ns_key`
widens from `(namespace, key)` to `(namespace, key, value)`, so the titles carrying a keyword are
found, and counted, from the index.

## Version 18 — a pair only a person decided

A person can merge two stored keywords no judge was asked about, from Plex DB-EX's tag doctor
([ADR-0020](./adr/0020-a-person-may-decide-a-pair-no-judge-saw)). `plexdb fold-merge-decisions`
writes such a pair as a `keyword_pairs` row with `decision` and `decided_at` set and `judged_at`,
`jev_score`, `jev_model` and `jev_error` all NULL. The table is rebuilt to allow it, every row and
its decision carried over. Its checks now say:

- `jev_score` and `jev_model` are both set or both NULL, and never set beside `jev_error`;
- `judged_at` is NULL exactly when neither a score nor a refusal is;
- a row has a judge's verdict, a person's decision, or both.

`judged_at IS NULL` is how a reader tells a person-only row apart. Such a row counts as merged by
the same rule as any other (`decision = 'accepted'`), so `merge_map` folds it with no change. The
judge never asks about a pair that has a row, so a person's merge is never sent to Jev. Clearing a
person-only decision deletes the row rather than leaving one with nothing in it. No reader reads
`keyword_pairs` through `plexdb-reader`, so `reader_shape` stays 10.

## Version 19 — a person's display name for a keyword

```
keyword_names  keyword TEXT PK, name TEXT NOT NULL, decided_at TEXT NOT NULL
```

At most one display name per stored keyword, set by a person in Plex DB-EX
([ADR-0021](./adr/0021-a-keyword-display-name-is-a-persons-label-beside-enrichment)). `keyword` is
a value as `enrichment.value` holds it; `name` is free text with its whitespace runs collapsed, 1
to 200 characters, and can be wording no source sent; `decided_at` is UTC.

**A name is for showing, never for computing.** A reader that shows a keyword to a person shows
its name when it has one, and its readable spelling (the shortest `keyword_forms.surface`, ties by
name) otherwise. Nothing that computes reads this table: the judges embed and ask about readable
spellings, and similarity, the map's vectors and the merge fold all use `enrichment` values. A
merge group whose kept keyword has no name shows the newest name any of its members has, so a name
stays on screen when title counts make another member the kept one. A view of two keywords side by
side, such as a proposed merge pair, labels each with its own name or spelling instead, since both
can sit in one group.

**Refresh rule.** Only `plexdb fold-name-decisions` writes the table, from
[`name_decisions.json`](#name-decisions-json). `prune-keyword-verdicts` deletes a name once its
keyword is a value no title carries as a keyword or a spoiler keyword; the file keeps it, and the
fold restores it when the value returns. The migration only adds, so `reader_shape` stays 10, and
`plexdb-reader` reads a name with `display_name(keyword)` — the stored name only, since a merge
group's label depends on the reader's own merge threshold. A reader built with that accessor
expects a v19 store; deploy the writer first.

### `name_decisions.json`

A name reaches `keyword_names` only through this file, for the same reason as
[`merge_decisions.json`](#merge-decisions-json), and it sits in the same directory. It is a JSON
list:

```json
[
  {"surface": "heists", "decision": "named", "name": "Heist",
   "decided_at": "2026-10-12T12:00:00+00:00"},
  {"surface": "Bank Robbery", "decision": "cleared", "decided_at": "2026-10-12T12:05:00+00:00"}
]
```

`surface` is a raw spelling of the keyword, resolved with the current `normalize_keyword` exactly
as in `merge_decisions.json`. `decision` is `named`, which needs `name`, or `cleared`;
`decided_at` is a UTC time, required for `named`. `plexdb fold-name-decisions` applies the file as
the sweep step right after `fold-role-decisions`, every run: entries apply in list order, so the
last entry for a keyword wins. `named` writes the row and `cleared` deletes it. An entry naming a
keyword no title carries is counted and ignored. A missing file means no names. A file that is not
valid JSON or breaks this shape is refused whole, and no row changes.

Plex DB-EX's Doctor tab ("Rename…" on a group) and Merges tab ("Rename…" beside each side of a pair, which then reloads the pair) write this file through one editor (`POST /api/names` with `{keyword,
name}`; a blank or null name clears it; a keyword no title carries is a 404) and lays it over the
table on every page that labels a keyword, so a name shows at once, marked "applies at next sweep"
on the Doctor tab, until the fold writes it. A write keeps one entry per resolved keyword.

## Version 20 — the stored title map's region labels

```
title_map_label  (kind, node) PK, x REAL, y REAL, width REAL, height REAL, titles INTEGER, value TEXT
```

The region labels of the default map in [`title_map`](#version-9-plex-db-ex-s-stored-title-map),
one row per labelled region of a k-d split of that map's titles. `node` is the region's heap number
(the children of `i` are `2i+1` and `2i+2`, so depth is `floor(log2(node + 1))`); `x` and `y` are
the median position of its titles and `width` and `height` the span of the middle 80% of them, in
map units; `titles` counts them. `value` is the keyword that names the region, as `enrichment.value`
holds it after the merge fold: the region's highest count-times-IDF tag that names none of its
ancestors. Like `title_map`, it is derived from `enrichment` and no consumer has a reason to read it.

**A label holds a keyword, never display text.** Plex DB-EX shows each label's display name or
readable spelling ([Version 19](#version-19-a-person-s-display-name-for-a-keyword)) as it serves
the map, so a rename shows on the next request without redrawing anything.

**Refresh rule.** `plexdb refresh-map` labels each map it redraws and replaces that kind's
`title_map_label` rows in the same transaction as its `title_map` rows, so a stored map, current or
stale, always carries the labels drawn with it. The drawing recipe in `title_map_state.fingerprint`
names the labelling too, so every map stored before this version reads as stale until the next
`refresh-map` redraws it with labels; until then Plex DB-EX shows it dimmed with no labels and draws
the fresh one live. The migration only adds, so `reader_shape` stays 10.

## Version 22 — a person's decisions about crowd lists

```
list_decision  url TEXT PK, decision TEXT NOT NULL, decided_at TEXT NOT NULL, added INTEGER NOT NULL
list_attempt   list_key TEXT PK, attempts INTEGER NOT NULL, attempted_at TEXT NOT NULL, reason TEXT NOT NULL
```

Which lists the store holds is no longer decided only by MDBList's top-lists feed. A person can add
a list by URL, keep one out, force a refetch, retry a failed fetch, or remove an added list, from
Plex DB-EX's Lists tab or `plexdb list-decide <url> <added|excluded|refetch|retry|removed>`.

`list_decision` holds the last decision per list. `url` is the canonical MDBList list URL
(`https://mdblist.com/lists/<user>/<list>`, lowercase, no trailing slash); `decision` is one of the
five above; `added` is 1 while a person's `added` decision stands. The file keeps one entry per
list, so a later `refetch` replaces the `added` entry; `added` keeps the fact that the list is a
person's until an `excluded` or `removed` decision ends it.

`list_attempt` is a failing list's fetch bookkeeping, keyed by the list's `url`, or by its
`collection_id` where the source gave no URL: how many fetches in a row failed, when the last one
did, and the reason. A fetch that succeeds deletes the row. It is bookkeeping, never a fact
([ADR-0013](./adr/0013-bookkeeping-never-shares-a-table-with-facts)), and it is not an
`enrichment_cursor` row because that table is keyed by `items`, and a list is not an item
([ADR-0023](./adr/0023-the-store-holds-facts-about-plex-library-items-and-nothing-else),
[ADR-0028](./adr/0028-list-decisions-and-fetch-attempts-are-keyed-by-list-url)).

**Refresh rule.** Only `plexdb fold-list-decisions` writes `list_decision`, from
[`list_decisions.json`](#list-decisions-json), as the sweep step after `fold-name-decisions`. Only
`harvest-mdblist` (MDBList URLs) and `harvest-letterboxd-lists` (Letterboxd URLs) act on it and write
`list_attempt`, each ignoring the other's lists:

- `excluded` — every stored list with that URL is wiped (entries, memberships, row) whatever its
  provenance, and the list is never fetched again, whether it comes from the feed or was added.
- `added` — the URL is fetched even when the feed does not carry it, with provenance `added`. A list
  that is also in the feed is fetched once, as `added`.
- `refetch` — ignores the staleness window for one fetch: it is pending while the list's
  `observed_at` is before the decision's `decided_at`, and the fetch moves `observed_at` past it.
- `retry` — clears the failure count of a list whose last attempt is before the decision's
  `decided_at`, then fetches it.
- `removed` — drops a stored list a person added and forgets its failures. On a feed list it does
  nothing: the feed names it again at the next harvest, and `excluded` is the decision that keeps it
  out.

A fetch that fails because MDBList answered something other than the list (a 404 for a URL that
names no list, a 5xx, a body that is not JSON) adds one to the list's `attempts` and the harvest goes
on to the next list. After 3 failed fetches in a row the harvest leaves the list alone until a
`retry` decision. A failure that says nothing about one list (the daily quota spent, a refused key,
no answer at all) ends the run instead and is recorded against no list. `plexdb check` and the list
table report a list in a failed state, with the reason.

### Lists found by search

`harvest-mdblist-search` (sweep step 62, best-effort, needs `MDBLIST_API_KEY`) searches outward from
what the household watched. Each watched movie, and each show with a watched episode, is a seed,
searched by title through MDBList's `/lists/search` (one request, matching list *names* only: no
endpoint answers which lists contain a title). Every list a search returns is a candidate. A candidate
is fetched like any MDBList list and kept as `provenance = 'search'` only when it holds at least
`MDBLIST_SEARCH_MIN_TITLES` (default 5) library titles; `seed_count` is how many of those are watched.
A fetched candidate holding fewer is written nowhere and is not fetched again until it is stale.

A run asks at most `MDBLIST_SEARCH_MAX_SEEDS` seeds (default 25, most-watched first) and fetches at most
`MDBLIST_SEARCH_MAX_LISTS` candidates (default 10, those surfaced by the most seeds first), so a first
pass spreads over several sweeps. A seed is asked again only after `MDBLIST_SEARCH_STALE_DAYS` (default
45). Seed cursors, the lists each seed surfaced (`key = hit:<url>`) and rejected candidates
(`key = rejected:<url>`) are `enrichment_cursor` rows under namespace `list_search`, source `mdblist`
(bookkeeping, ADR-0013). Decisions steer it as they do `harvest-mdblist`: an excluded list is never
fetched, a refetch ignores staleness once, three failed fetches stop a list until a retry. A list
the feed or a person already placed keeps its provenance.

A name match is a lead, not evidence: short titles ("FROM", "24", "House") surface lists named for
something else, and the library-title threshold is the only filter. Exclude the noise from the Lists tab.
The migration is not involved: `provenance` and `seed_count` exist since v21.

### Letterboxd lists

`harvest-letterboxd-lists` (sweep step 61, keyless, best-effort) fetches only the Letterboxd lists a
person added, and the Letterboxd lists already stored; Letterboxd has no list feed. A list is
`collection_id = letterboxd:<user>/<list>`, `source = 'letterboxd'`, `url` its canonical URL, `likes`
`NULL`, `size` the count the list's description states. It reads `/<user>/list/<list>/` and each
`/page/N/` (100 entries a page), then every entry's film page, whose `<body>` carries the TMDB id and
`data-tmdb-type`: `movie` stays `movie`, `tv` becomes `show`, and the entry is stored as
`('tmdb', <id>, <kind>)`. A film Letterboxd has not matched to TMDB is stored as
`('letterboxd', <slug>, 'movie')` and never resolves to a library title. A film shared between
lists is read once per run. The staleness window is `LETTERBOXD_LISTS_STALE_DAYS` (default 45).

A fetch that fails writes nothing and adds one to the list's `attempts`: a Cloudflare challenge
(403), a page without the `list-page` body class and `title-1` heading, a film page that does not
load, fewer entries read than the description states, or a list over 2,000 entries. The stored list,
if any, stays as it was.

The migration only adds, so `reader_shape` stays 10.

### `list_decisions.json`

A decision reaches `list_decision` only through this file, for the same reason as
[`merge_decisions.json`](#merge-decisions-json), and it sits in the same directory. It is a JSON
list:

```json
[
  {"url": "https://mdblist.com/lists/pierce/horror-picks", "decision": "added",
   "decided_at": "2026-10-15T12:00:00+00:00"},
  {"url": "https://mdblist.com/lists/lina/top-watched", "decision": "excluded",
   "decided_at": "2026-10-15T12:05:00+00:00"}
]
```

`url` is any spelling of an MDBList list URL (`https://mdblist.com/lists/<user>/<list>`) or a Letterboxd
list URL (`https://letterboxd.com/<user>/list/<list>/`); the store keys on its canonical form,
`https://letterboxd.com/<user>/list/<list>` for Letterboxd. `decision` is
`added`, `excluded`, `refetch`, `retry` or `removed`; `decided_at` is a UTC time. Entries apply in
list order, so the last entry for a list wins, and a write keeps one entry per list. A file with a
URL that is neither is refused whole.

Plex DB-EX writes the file with `POST /api/list-decisions` (`{url, decision}`; `added` and `excluded`
take any MDBList or Letterboxd list URL, the other three need a list the store, its decisions or its failures
already name, else 404) and lays it over the list table, so a decision shows at once with a dashed
outline and a badge (`Waiting for sweep`, `Excluded · next sweep removes it`) until the sweep acts.

## frames.db — the Frame index

A second SQLite file, `frames.db`, in the directory `PLEXDB_FRAMES_DIR` names (`/frames` in the
container) ([ADR-0024](./adr/0024-the-frame-index-is-a-second-file-readers-attach-in-place)). The
same `plexdb` package is its only writer. It is a public interface like `plexdb.db`: a reader opens
it read-only where it lies and `ATTACH`es it to the Snapshot.

- **Its own version.** `frames.db` carries its own `schema_version` row and its own append-only
  migration chain (`plexdb/frames.py`), applied by the same `schema.apply`. Versions 1 and 2 are below.
  `plexdb frames migrate` applies it, as does every frames command and the container's start.
  `plexdb migrate` does not: it is a required sweep step, and the optional Frame index must not
  be able to stop a sweep.
- **Rollback-journal mode, not WAL.** No `-wal` or `-shm` sidecar, so a reader with a read-only
  mount can open it. A writer commits once per title.
- **Never snapshotted or backed up.** It is derived from the media files and runs to tens of
  gigabytes at full library; a lost file is rebuilt by the frame harvester. No migration copy is
  taken.
- **No transaction touches both files.** Whatever a frames write needs from `plexdb.db` is read
  first, over a separate read-only connection.
- **No foreign keys.** `item_id` is the store's `items.item_id`, but SQLite cannot enforce a key
  across files. `plexdb prune-frames` (sweep step 27, best-effort, skipped when
  `PLEXDB_FRAMES_DIR` is unset) deletes every row whose `item_id` is no longer in `items`, one
  transaction per title, and prints the count per table.
- **Loading sqlite-vec.** `frame_vec` is a [sqlite-vec](https://github.com/asg017/sqlite-vec)
  `vec0` virtual table (version 0.1.9). Reading it needs the extension loaded; the ordinary tables,
  `PRAGMA quick_check` and the version row do not.

A title is **indexed** when its `frame_index_state` row has `error IS NULL`, and **failed** when
it has one with an error. `frame_vec`'s width is fixed when the table is created, so a model of
another width is a migration.

### Version 1 — tables

```sql
CREATE TABLE frame_index_state (
    item_id       TEXT PRIMARY KEY,
    part_id       INTEGER,     -- the Plex media part read
    model         TEXT,        -- the encoder model that produced frame_vec
    model_version TEXT,
    dims          INTEGER,     -- vector width
    quant         TEXT,        -- how a vector is stored, e.g. int8
    duration_ms   INTEGER,     -- the last video frame's offset, as the harvester decoded it
    bytes         INTEGER,     -- the part's size, as Plex reports it
    file_hash     TEXT,        -- size + mtime + first MiB; a change re-indexes the title
    shots         INTEGER,
    vectors       INTEGER,
    indexed_at    TEXT NOT NULL,  -- ISO-8601, when the last attempt finished
    error         TEXT         -- the last attempt's failure; NULL once it indexed cleanly
);

CREATE VIRTUAL TABLE frame_vec USING vec0(
    item_id TEXT PARTITION KEY,
    t_ms    INTEGER,           -- the sampled frame's offset into the title
    shot_ix INTEGER,           -- which Shot it belongs to, counted from 0
    vec     int8[768]          -- CLIP ViT-L/14 (ADR-0025)
);

CREATE TABLE cuts (
    item_id TEXT NOT NULL,
    t_ms    INTEGER NOT NULL,
    score   REAL,              -- scdet's scene-change score; NULL for a chapter mark
    source  TEXT NOT NULL CHECK (source IN ('scdet', 'chapter')),
    PRIMARY KEY (item_id, source, t_ms)
);

CREATE TABLE frame_queue (
    item_id   TEXT PRIMARY KEY,
    queued_at TEXT NOT NULL,   -- ISO-8601; the harvester takes the oldest first
    queued_by TEXT NOT NULL    -- who asked; `plexdb frames queue` writes `cli` unless --by says otherwise
);
```

### Version 2 — the title in hand

```sql
CREATE TABLE frame_harvest (
    id       INTEGER PRIMARY KEY CHECK (id = 1),  -- at most one row
    item_id  TEXT NOT NULL,                       -- the title the harvester is indexing now
    since    TEXT NOT NULL                        -- ISO-8601, when it started that title
);
```

`plexdb frames status` reads it, so a second process can say which title is being indexed.

### Version 3 — each Shot's boundaries

```sql
CREATE TABLE shots (
    item_id TEXT NOT NULL,
    shot_ix INTEGER NOT NULL,  -- the number `frame_vec.shot_ix` carries, from 0
    in_ms   INTEGER NOT NULL,
    out_ms  INTEGER NOT NULL,
    PRIMARY KEY (item_id, shot_ix)
) WITHOUT ROWID;
```

The harvester writes a title's rows in the same transaction as its vectors, and it is the only
writer of the boundaries, by one rule (`frame_harvest.shots`): a Cut closer than 500 ms
to the boundary before it makes no Shot. Readers read `in_ms` and `out_ms`; none re-derives them.
The migration numbered the titles already indexed by that same rule, from `cuts` and
`frame_index_state.duration_ms`.

### Version 4 — a failed title's reason

```sql
ALTER TABLE frame_index_state ADD COLUMN reason TEXT;
```

`reason` is the short phrase for a failed title, such as `file not under PLEX_SOURCE_ROOTS` or
`ffmpeg scene scan exited -9`, and holds no path and no ffmpeg output. `error` keeps the full
message. Every `MediaError` carries one, and the harvester writes both when it records a failure.
A title that failed before version 4 has `reason` NULL; a successful index clears both columns.

### `plexdb frames status`

Prints one JSON object on stdout and exits 0. It creates `frames.db` at the current version if
the directory holds none, so on a fresh directory every count is zero.

```json
{
  "frames_db": "/frames/frames.db",
  "schema_version": 2,
  "bytes": 77824,
  "sections": [
    {"section_id": "1", "indexed": 0, "total": 11754},
    {"section_id": "2", "indexed": 0, "total": 75951}
  ],
  "queued": 0,
  "frame_hours": "01:00-07:00",
  "inside_hours": false,
  "indexing": {"item_id": "imdb:tt0113277", "since": "2026-10-15T02:11:40-04:00"},
  "encoder_reachable": true,
  "ffmpeg_found": true,
  "decoder": "cpu",
  "failed_total": 1,
  "failed": [
    {"item_id": "imdb:tt0113277", "error": "/media/Heat.mkv: ffmpeg frame cut exited 1: …", "reason": "ffmpeg frame cut exited 1", "at": "2026-10-15T02:14:09-04:00"}
  ]
}
```

| Key | Meaning |
|---|---|
| `frames_db` | Absolute path of the file read. |
| `schema_version` | `frames.db`'s own version. |
| `bytes` | The file's size on disk. |
| `sections` | One entry per Plex section, ordered by section id as a number: `total` is the movies and episodes `plex_items` places there, `indexed` how many of those are indexed. A show has no file and is not counted. |
| `queued` | Rows in `frame_queue`. |
| `frame_hours` | `PLEXDB_FRAME_HOURS` as set — `HH:MM-HH:MM` in the process's TZ, an end earlier than the start running past midnight — or `null` when unset. |
| `inside_hours` | Whether the current time is inside that window; `false` when it is unset. |
| `indexing` | The title the harvester is indexing now — `item_id` and `since` — or `null` when none is. Reported only while a harvester holds `harvest.lock`; a row left by a killed harvester reads as `null`, and the next harvester start deletes it. |
| `encoder_reachable` | Whether `GET {PLEXDB_FRAME_ENCODER_URL}/v1/models` answered 2xx within 10 s; `null` when `PLEXDB_FRAME_ENCODER_URL` is unset. Listing models loads none, so this never waits out a cold start. |
| `ffmpeg_found` | Whether `PLEXDB_FFMPEG` and `PLEXDB_FFPROBE` (default `ffmpeg`, `ffprobe`) are both found. The harvester needs them; the container image ships both. |
| `decoder` | `PLEXDB_FRAME_DECODE`: `cpu` (the default) or `cuda`. After the harvester asked for CUDA and fell back, `cpu (cuda requested: <ffmpeg's last line>)`; the note lives in `decoder.json` beside `frames.db` until the next harvester run starts. |
| `failed_total` | How many titles' last attempt failed. |
| `failed` | Those titles, newest first, each with its `item_id`, `error` (the full message, which can name file paths and carry ffmpeg's stderr), `reason` (the short path-free phrase, `null` for a title that failed before version 4) and `at`. The command lists all of them with both; the explorer's Frame index panel keeps the newest 20, returns `reason` only (`failed (see harvester log)` where it is `null`), and shows `failed_total` for the rest. |

`plexdb frames queue <item_id>` adds a `frame_queue` row and exits non-zero for an item_id not in
`items`; a title already queued keeps its place.

### The harvester (`plexdb harvest-frames`)

`plexdb harvest-frames` fills `frames.db` while the clock is inside `PLEXDB_FRAME_HOURS`; `plexdb
schedule` starts it when the window opens, and it is a step of no Sweep. `--ignore-hours` runs it
now and `--limit N` stops after N titles. It writes `frames.db` alone.

- **Order.** Queued titles, oldest `queued_at` first; then titles with no `frame_index_state` row,
  movies before episodes, newest Plex `addedAt` first; then titles whose `file_hash` differs from
  the stored one, least recently indexed first. A failed title waits for a new `file_hash` or a
  new queue row. A title with several Plex copies uses the newest copy's first part.
- **Reading.** Plex names each part's file; `ffmpeg` and `ffprobe` read that file, read-only,
  under `PLEXDB_MEDIA_DIR`, after stripping whichever `PLEX_SOURCE_ROOTS` entry the path sits
  under. A path under no root, or one that would resolve outside `PLEXDB_MEDIA_DIR`, fails the
  title. Nothing streams from Plex: Plex makes each `/library/parts/<id>/file` read a playback
  session and ends the account's previous one, so two readers of different titles would cut each
  other off. `file_hash` is the sha256 of the part's size, the file's modification time as an
  HTTP date, and its first MiB. `bytes` is the part's size.
- **Cuts.** `scdet` (threshold 10) writes `cuts` rows with `source = 'scdet'` and its score;
  `ffprobe` chapter marks write `source = 'chapter'` with a NULL score.
- **The end.** A title ends at its last video frame, found by the same decode as the Cuts, and
  `duration_ms` stores that offset. Plex's duration is not used: a container can carry seconds of
  audio past the last video frame, and a frame asked for there does not exist.
- **Shots and frames.** Boundaries are the Cuts, dropping any within 500 ms of the one before. A
  Shot of 2 s or less gets one frame at its midpoint; a longer Shot gets one every 2 s, the first
  1 s in. Frames are 224 px tall, JPEG. `shot_ix` counts Shots from 0 and `shots` is their number.
  A time with no decodable frame is skipped; a title with none fails.
- **Vectors.** The encoder's unit vectors are stored with sqlite-vec's `vec_quantize_int8(…, 'unit')`;
  `frame_index_state.quant` is `int8`. Frames go to the encoder 32 per request.
  `harvest-frames --keep-float` also writes the encoder's float32 vectors to `frames-float.db`
  beside `frames.db`, for `plexdb frames quant-agreement` to rank int8 against fp16. That file
  is a measurement: no reader opens it, nothing migrates it, and deleting it loses nothing.
- **One transaction per title** replaces the title's `frame_vec`, `cuts` and `frame_index_state`
  rows and deletes its `frame_queue` row.
- **Failures.** An unreadable part or an ffmpeg error records `error` and `reason` in `frame_index_state`,
  keeps the title's earlier rows, drops its queue row, and the run moves on. An encoder that is
  refused, times out, answers 5xx or answers 429 stops the run with the title untouched; the next
  window resumes. Three titles in a row refused with another 4xx also stop it.
- **Stalls.** A decode that runs past six hours fails the title. The scan for changed titles
  reads 1 MiB per indexed title and stops when the window closes.
- **Memory.** The scene scan is one filter chain: `scdet` with `s=0` marks the Cuts and passes
  every frame on to `framecrc`, so ffmpeg holds about 820 MB however long the title is. Frame
  extraction opens up to 24 seeks into the file in one ffmpeg, each with one decoder thread: a 4K
  10-bit batch peaks at about 4.2 GB, a 1080p batch at about 1.6 GB.
- **One harvester at a time.** A harvest takes an `flock` on `harvest.lock` in `PLEXDB_FRAMES_DIR`
  and writes its pid there. A second `plexdb harvest-frames` exits 1 with `another harvester is
  running (pid N, indexing <item_id>)`; `plexdb schedule`'s harvester logs the same refusal and
  retries in 15 minutes. The operating system drops the lock when its holder exits, so a crash
  leaves nothing to clear.
- **The log line**, one per title, to stdout: `frames: <ISO time> item_id=… part=… bytes=…
  decode_s=… encode_s=… shots=… vectors=… reason=queued|new|changed` and `error='…'` on a failure.

### Measured on 20 films

Twenty films indexed on Tower — fast-cut action, long takes, animation and old film stock, 35.9
hours of video — measured what the whole library costs before its backlog starts.

| Measure | Result |
|---|---|
| Shots per hour of video | 447 on average; 7 (*Russian Ark*) to 1,116 (*Mad Max: Fury Road*) |
| Vectors per hour | 1,853 |
| `frames.db` per hour | 1.78 MB (int8) |
| Whole library, 63,293 hours | 112.9 GB of int8 vectors, 117 million vectors; fp16 would be 242.5 GB |
| int8 against fp16 | Over 20 phrases, the int8 and fp16 top 10 share 9.05 Shots on average (8 at worst), the top 50 share 45.2 (41–49), and the first Shot is the same for 19 of 20; the largest similarity difference is 0.024 |
| Pace | About 3.9× real time, 2 titles an hour, decoding on the CPU |
| What sets the pace | Decoding: 12,080 s of decode against 476 s of encode over eight titles |
| Whole library at that pace | About 16,200 hours |
| Tautulli | Logs no harvester read: none of the 20 films appears in its history over the runs |

`quant` stays `int8`. The backlog (`PLEXDB_FRAME_HOURS`) waits for GPU decoding to be measured
(plex-db-ex-8nb.1.21).

### The encoder request (ADR-0025)

Every frame and phrase is embedded by `POST {PLEXDB_FRAME_ENCODER_URL}/v1/embeddings` with
`PLEXDB_FRAME_MODEL` as `model` (`plexdb/frame_encoder.py`). The client waits up to 300 s for an
answer, so a server holding the request through a cold start is waited out. A refused connection,
a timeout or a 5xx is `EncoderUnavailable`, naming the URL and the status. Each vector is scaled
to unit length before it is used.

OpenAI's API defines text input only. This image shape is the one a real Infinity 0.0.77 server
serving `openai/clip-vit-large-patch14` accepted on 2026-10-16:

```json
{
  "model": "openai/clip-vit-large-patch14",
  "input": ["data:image/jpeg;base64,/9j/4AAQSkZJRg…"],
  "modality": "image",
  "encoding_format": "float"
}
```

A phrase is the same request with `"modality": "text"` and the phrase itself in `input`. The
answer is OpenAI's: `data[]` of `{index, embedding}`, 768 floats each.

- **`modality` is required.** Without it, Infinity answers 200 with a 768-wide vector of the data
  URI read as *text* (cosine 0.206 with the real image vector), so a missing field passes every
  width check.
- **Each image is a `data:image/jpeg;base64,` URI.** Bare base64 is a 422.
- **Infinity needs `--url-prefix /v1`.** Infinity 0.0.77 serves `/embeddings` with no prefix, so
  without the flag `/v1/embeddings` is a 404 `{"detail":"Not Found"}`; `/health` stays unprefixed
  either way.

`plexdb frames probe-encoder` embeds the phrase `a red square`, then a bundled 224 px red square,
then the phrase again, and prints:

```json
{
  "encoder_url": "http://127.0.0.1:7997",
  "model": "openai/clip-vit-large-patch14",
  "served_by": "openai/clip-vit-large-patch14",
  "image": {"dims": 768, "first_byte_ms": 1041, "latency_ms": 1041},
  "text": {"dims": 768, "first_byte_ms": 45, "latency_ms": 45, "phrase": "a red square"},
  "cold_start_wait_ms": 11,
  "frame_dims": 768
}
```

`model` is what the request named, and `served_by` is what the answer named. A server holding
one model can ignore the name: Infinity answered a request for `no-such-model` from
`openai/clip-vit-large-patch14`. `text` is the second, warm phrase call. `cold_start_wait_ms` is
the first phrase call's time to first byte minus the second's, floored at 0: the first call pays
any model load, and the two ask for the same work, so the image's own encode time stays out of
it. The
probe exits non-zero when the encoder is unavailable, when the two widths differ, when they are
not `frame_dims`, or when the answer names a model other than the one requested. It
writes one `encoder:` line per request on stderr. Against that server the red square scored
cosine 0.277 with `a red square` and 0.151 with `a dog in a park`. Both files are untouched.

`plexdb check` also reads `frames.db` read-only when `PLEXDB_FRAMES_DIR` is set: it runs
`quick_check` and compares the version with this build's. A damaged file, or one at another
version, makes the check exit non-zero. A missing file is reported and does not.
`check --wait` polls the version rows only and runs `frames.db`'s `quick_check` once, on the
report it prints.

## Searching the Frame index

`Reader::open(snapshot, frames, phrases)` in `plexdb-reader` opens the Snapshot read-only and, when
given a path, `ATTACH`es `frames.db` as the schema `frames`, also read-only. Both files' versions
are checked at open: the Snapshot's `reader_shape`, and `frames.db`'s `schema_version`, which must
be exactly 3, the version this build's search was written against. A missing, foreign or other-version `frames.db` fails the open; no file is created.
`phrases` is a `PhraseCache { path, encoder_url, request_model }`. The Reader sends `request_model` (`PLEXDB_FRAME_MODEL`) as the embeddings request's `model`; an answer naming a model other than the one `frame_index_state` recorded fails the search with `EncoderFailed`, and an answer naming none is accepted, as `probe-encoder` does. `capabilities().search` is true when both
`frames` and `phrases` were given.

```text
search_shots(Search { pool, phrase, negatives, weight, temperature }) -> ShotSearch
draw(&ShotSearch, n, seed) -> Vec<ShotHit>
cuts(item_id) -> Vec<u64>        chapters(item_id) -> Vec<u64>
```

**Shots.** A title's Shots are numbered from 0 in `frame_vec.shot_ix`, and `shots` holds each
one's `in_ms` and `out_ms` (the harvester's rule is described under Version 3 of `frames.db`). Shot
`k` spans the `shots` row `(item_id, k)`. A `shot_ix` with no `shots` row fails the search with
`InconsistentIndex`, as does a frame whose width is not `frame_index_state.dims`. `cuts()` returns
the stored times and `chapters()` the ones whose `source` is `chapter`.

**Similarity.** The Phrase's vector is unit length. A stored frame is an int8 vector of whatever
uniform scale the harvester chose, so a frame's similarity is `dot(phrase, frame) / |frame|`, the
cosine for any scale. A Shot's `phrase_sim` is its best frame's; its `negative_sims` hold its best
frame against each negative, in the order given.

**Score.** `score = phrase_sim - weight * max(negative_sims)`; with no negatives it is `phrase_sim`.
`weight` defaults to 0.5, Midjourney's `--no`. Shots are ordered by `score` descending, ties by
`(item_id, in_ms)`, so identical inputs give identical output.

**Probabilities and draw.** With a temperature `T > 0`, each returned Shot carries
`p = exp(score / T) / sum`, summing to 1 over the Shots returned. `draw` takes `n` Shots without
replacement, weighted by `p`, from a SplitMix64 generator seeded by `seed`: the same results, `n`
and seed give the same picks, in pick order. Without a temperature `draw` returns the top `n`.

**Counts.** `pool` is the number of distinct item_ids searched. `indexed` is how many of them have a
`frame_index_state` row that recorded a `model`; a title whose every attempt failed has none and is searched as if absent, while one that failed a re-index keeps its earlier rows and is still searched. `model` is the one model those rows record; a pool whose rows record two
models fails with `MixedModels`, and a pool with none returns no Shots, `model` empty, and calls
no encoder.

**The Phrase cache** (ADR-0026) is a SQLite file at the caller's path, the only file the reader
writes:

```sql
CREATE TABLE phrase_cache (
    phrase        TEXT NOT NULL,   -- lowercased, trimmed, whitespace collapsed
    model         TEXT NOT NULL,   -- frame_index_state.model
    model_version TEXT NOT NULL,   -- frame_index_state.model_version
    vec           BLOB NOT NULL,   -- float32, little-endian, unit length
    created_at    TEXT NOT NULL,   -- UTC, ISO-8601
    last_used_at  TEXT NOT NULL,
    PRIMARY KEY (phrase, model, model_version)
) WITHOUT ROWID;
```

Before each search, rows unused for 90 days are deleted. A phrase or negative missing from the cache
goes in one `POST {encoder_url}/v1/embeddings` (the request in "The encoder request" above, with
`modality: "text"` and the model the index records), and its answer is stored. A hit sets
`last_used_at`. A vector under another model, model version or width is never used. A refused or
unreachable encoder, or a 5xx, is `EncoderUnavailable`; any other non-200 or an unusable body is
`EncoderFailed`. The reader's HTTP client has no TLS, so an `https://` encoder URL is
`EncoderUnavailable`.

**`plexdb-read`** is the crate's command line: one subcommand per accessor, JSON on stdout, a
message on stderr and a non-zero exit on error (2 for a usage error). It reads `--snapshot`,
`--frames`, `--phrase-cache` and `--encoder-url`, or `PLEXDB_SNAPSHOT_PATH`, `PLEXDB_FRAMES_DIR`,
`PLEXDB_PHRASE_CACHE` and `PLEXDB_FRAME_ENCODER_URL`. `search` prints a `ShotSearch`:

```text
plexdb-read search <phrase> (--pool <item_id> [--pool ...] | --pool-file <path|->)
                   [--not <phrase> [--not ...]] [--weight w] [--temperature T] [--draw n --seed s]
```

`--pool` and `--not` take one value each and repeat (`--pool a --pool b`), so flags and the phrase
go in any order. `plexdb-read --help` prints the usage and exits 0.

```json
{"shots": [{"item_id": "imdb:tt0113277", "in_ms": 0, "out_ms": 3000, "frames": [[0, 0.91]],
            "phrase_sim": 0.91, "negative_sims": [0.12], "score": 0.85, "p": 0.62}],
 "indexed": 1, "pool": 2, "model": "openai/clip-vit-large-patch14"}
```

`p` is absent without a temperature. With `--draw`, `shots` holds the drawn Shots.

**`plexdb search <phrase>`** runs `plexdb-read search` with the files from the environment: the
Snapshot (`PLEXDB_SNAPSHOT_PATH`, else `PLEXDB_PATH`), `frames.db`, the Phrase cache
(`PLEXDB_PHRASE_CACHE`, else `phrases.db` in `PLEXDB_FRAMES_DIR`) and `PLEXDB_FRAME_ENCODER_URL`.
It takes `--pool` or `--pool-file` (item_ids; `-` is standard input), `--not` (both repeat, one value per flag), `--weight`,
`--temperature`, `--draw` and `--seed`, and prints what the binary prints. The binary is
`PLEXDB_READ_BIN`, default `plexdb-read` on `PATH`; the container image carries it. The command
exits 1 with the build command when it cannot find the binary.

## Reading the store to build collections

Each query below was run against a migrated copy of the real store. Parameters are written as
literals; swap in your own. Keyword values are the stored, normalized ones — look a spelling up in
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
