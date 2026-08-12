"""The store's schema, and the code that applies it.

The schema **is** the public API of this project (ADR-0001). One process writes
this file; every consumer opens it read-only, and there is no version
negotiation — so a change here is a breaking change for every consumer at once.

Migrations are an append-only list: index 0 is the statement batch that takes an
empty database to version 1, index 1 takes it to version 2, and so on. Never
edit a batch that has shipped; add another.
"""

from __future__ import annotations

import sqlite3

from .errors import StoreError

#: Version 1 — identity and enrichment. The four tables the first slice needs
#: and nothing more: what a title is, every id it is known by, where Plex keeps
#: it, and what has been learned about it.
_V1 = """
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
CREATE INDEX idx_items_show ON items(show_item_id);

-- Every other id a title is known by. The enrichment fetchers need a TMDb id
-- that item_id is not guaranteed to be, and this is where a Trakt slug, a
-- Letterboxd URL, and a resolved free-text title land too (ADR-0002).
CREATE TABLE external_ids (
    item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns      TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (ns, value)
);
CREATE INDEX idx_external_ids_item ON external_ids(item_id);

-- Where Plex keeps a title. Watch history identifies a title only by rating
-- key, so this is the join that makes a play resolvable to an item.
CREATE TABLE plex_items (
    rating_key TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    section_id TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE INDEX idx_plex_items_item ON plex_items(item_id);

-- Namespaced, opaque facts about a title. A writer may wipe and rewrite only
-- rows in its own namespace, and the store never interprets a value.
-- fetched_at drives staleness, not correctness.
CREATE TABLE enrichment (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, namespace, key, value)
);
CREATE INDEX idx_enrichment_ns_key ON enrichment(namespace, key);
CREATE INDEX idx_enrichment_item_ns ON enrichment(item_id, namespace);
"""

#: Version 2 — watch history (ADR-0004, issue #8). One row per Plex history
#: event. `history_key` is Plex's own identity for the *viewing event* — not
#: `plex_items.rating_key`, which is the *title's* identity — and is what
#: makes a re-ingest idempotent: `INSERT ... ON CONFLICT(history_key) DO
#: NOTHING` skips whatever a prior ingest already wrote.
#:
#: `ip`, `percent_complete`, and `paused_counter` are populated only by the
#: Tautulli adapter (issue #9); null here is the normal state of a Plex-only
#: deployment, not a degraded one.
_V2 = """
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
CREATE INDEX idx_plays_item ON plays(item_id);
CREATE INDEX idx_plays_viewed_at ON plays(viewed_at);

-- The incremental-fetch watermark `ingest_plays` reads at the start of a run
-- and advances at the end. Deliberately NOT derived from MAX(plays.viewed_at):
-- an event skipped as unresolved is never written to `plays`, so a watermark
-- built only from written rows would silently advance past it the moment any
-- newer event succeeds — and it would never be asked for again, even after a
-- later walk resolves it. This single row instead tracks the oldest event
-- still outstanding (or the newest event fully accounted for, once nothing is
-- outstanding), so the next run always asks Plex for everything from there
-- forward.
CREATE TABLE plays_ingest_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    since_viewed_at INTEGER NOT NULL
);
"""

#: Version 3 — affinity edges (issue #6). Directed, typed, ranked relationships
#: between two items, first proven with TMDB's `recommendations` (behavioural)
#: and `similar` (content-based) endpoints, kept as distinct `edge_type`
#: values, never merged — they measure different things.
#:
#: `from_id` and `to_id` both reference `items`, which is populated only by
#: `plexdb walk` (ADR-0005). A title TMDB recommends that this library has
#: never walked has no `item_id` to point at, so a writer can only record an
#: edge between two titles this store already knows — the schema has no
#: stub-item or candidate-title table for something nobody owns, so that is
#: not a policy choice, it is the only thing representable here.
#:
#: `PRIMARY KEY (from_id, to_id, edge_type)` matches the uniqueness rule
#: verbatim; the two secondary indexes below carry the actual query traffic —
#: "edges from X of type Y" and "edges into X of type Y" — in both directions
#: without a table scan.
_V3 = """
CREATE TABLE edges (
    from_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    to_id      TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    edge_type  TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, edge_type)
);
CREATE INDEX idx_edges_from_type ON edges(from_id, edge_type);
CREATE INDEX idx_edges_to_type ON edges(to_id, edge_type);
"""

#: Version 4 — the Tautulli history adapter (issue #9). Two columns on the
#: existing `plays` table, nothing else: `seconds_watched` is Tautulli's
#: `duration` copied verbatim (already net of paused time — computing
#: `duration - paused_counter` would subtract pause a second time), and
#: `tautulli_id` is the Tautulli history row's own `id`, stored so a re-run
#: of `plexdb enrich-tautulli-plays` is idempotent: the partial unique index
#: below means the same Tautulli row can never land on two different plays.
#: No flag, no threshold, no boolean verdict — a reader that wants a floor
#: writes `WHERE seconds_watched >= <n>` itself.
_V4 = """
ALTER TABLE plays ADD COLUMN seconds_watched INTEGER;
ALTER TABLE plays ADD COLUMN tautulli_id INTEGER;
CREATE UNIQUE INDEX idx_plays_tautulli_id ON plays(tautulli_id) WHERE tautulli_id IS NOT NULL;
"""

#: Version 5 — an external id is scoped by media type (issue #23). TMDB and
#: TVDB number movies and TV shows in two separate lists that both start at 1,
#: and Plex reports both as a bare `tmdb://1678` with no type attached. Movie
#: 1678 is *Godzilla* (1954); show 1678 is *The Golden Girls* (1985). Under the
#: old `PRIMARY KEY (ns, value)` those two unrelated records claimed the same
#: row, and `walk._resolve_existing` — which looks a title up by external id
#: first (ADR-0008, as amended by #19) — read that as "seen before" and handed
#: the show the movie's identity. 1,414 identities in the author's store had
#: fused that way, 1,326 of them holding two different IMDb ids.
#:
#: `kind` is the walk's own `movie` / `show` / `episode`. All three are needed,
#: not just movie-vs-show: TVDB numbers episodes in a list separate from series.
#: IMDb is unaffected (one shared list for everything) and so is Plex's own
#: `plex://` id (already type-qualified), but the key covers every namespace
#: rather than special-casing the two that collide — a rule with an exception
#: list is a rule waiting for the next source to be added to it.
#:
#: Existing rows take their `kind` from `items.type`. That is wrong for the
#: fused identities by construction — they hold one type for two titles — which
#: is why `plexdb repair-identities` re-derives them from Plex rather than the
#: migration trying to guess which rating key was which title.
#:
#: `plays.rating_key` is the second half. `plays.ingest` resolves a rating key
#: to an `item_id` and then discards it, so a play records a conclusion with no
#: record of the evidence, and no identity correction can be applied to rows
#: already written — which is why repairing #23 costs a re-ingest instead of an
#: UPDATE. Nothing reads this column yet: `repair-identities` still deletes and
#: re-ingests. It buys the *next* correction, not this one, and it is added now
#: because the schema version is already moving.
#:
#: Backfilled for every identity mapping to exactly one rating key, and left
#: NULL wherever an identity maps to several — the fused ones, and equally the
#: legitimate merges from #19 where one title sits in two library sections.
#: NULL is the honest answer in both cases: no evidence was ever recorded.
_V5 = """
CREATE TABLE external_ids_v5 (
    item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns      TEXT NOT NULL,
    value   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    PRIMARY KEY (ns, value, kind)
);
INSERT INTO external_ids_v5 (item_id, ns, value, kind)
SELECT e.item_id, e.ns, e.value, i.type
FROM external_ids e
JOIN items i ON i.item_id = e.item_id;
DROP TABLE external_ids;
ALTER TABLE external_ids_v5 RENAME TO external_ids;
CREATE INDEX idx_external_ids_item ON external_ids(item_id);

ALTER TABLE plays ADD COLUMN rating_key TEXT;
UPDATE plays SET rating_key = (
    SELECT p.rating_key FROM plex_items p
    WHERE p.item_id = plays.item_id
      AND (SELECT COUNT(*) FROM plex_items p2 WHERE p2.item_id = plays.item_id) = 1
);
CREATE INDEX idx_plays_rating_key ON plays(rating_key);
"""

#: Version 6 — collection membership (issue #34). The crowd lists a title
#: appears on, and where in them. Two tables rather than one because a list's
#: own facts — how long it is, how many people follow it — belong to the list,
#: not to one title's place in it; carrying them per membership would repeat
#: them thousands of times and let two rows disagree about the same list.
#:
#: **There is no `weight` column, and there will not be one** (issue #33,
#: ADR-0012). One number would have to mean "third of a hundred" for a ranked
#: list, "on a list eight thousand people follow" for a popular one, and "named
#: in seven comments" for a subreddit — three different measurements — and once
#: computed it cannot be inverted back into the facts it came from. That is the
#: same objection `_V4` records against a stored `counts_as_signal`. The store
#: records what the source said; the consumer weighs it.
#:
#: Every column past the keys is nullable on purpose. A source fills what it
#: genuinely has and leaves the rest empty rather than inventing a value, so a
#: missing `rank` stays distinguishable from rank 1. MDBList fills `rank`,
#: `size` and `likes`; a subreddit would fill only `mentions`.
#:
#: `item_id` references `items`, populated only by `plexdb walk` (ADR-0005), so
#: a list entry naming a title this library has never walked has nothing to
#: point at. It is dropped and counted, never stored under an invented id —
#: the same rule ADR-0009 sets for edges. `size` still records the list's full
#: length, so a consumer can see how much of it was missed.
_V6 = """
CREATE TABLE collection (
    collection_id TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    name          TEXT,
    url           TEXT,
    size          INTEGER,
    likes         INTEGER,
    observed_at   TEXT NOT NULL
);
CREATE INDEX idx_collection_source ON collection(source);

CREATE TABLE collection_membership (
    collection_id TEXT NOT NULL REFERENCES collection(collection_id) ON DELETE CASCADE,
    item_id       TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    rank          INTEGER,
    mentions      INTEGER,
    observed_at   TEXT NOT NULL,
    PRIMARY KEY (collection_id, item_id)
);
CREATE INDEX idx_collection_membership_item ON collection_membership(item_id);
"""

#: Version 7 — bookkeeping leaves the facts table (issue #41, ADR-0013).
#: `enrichment` now holds facts about titles and nothing else; every writer's
#: per-title fetch cursor moves to `enrichment_cursor`.
#:
#: Both TMDB writers recorded progress as rows in `enrichment` itself —
#: `tmdb_keywords/_fetched`, `tmdb_edges/_fetched_recommendations`,
#: `tmdb_edges/_fetched_similar`, all with the literal value `'1'`. A leading
#: `_` on the key meant "not a fact, skip me", and `plexdb-reader`'s taste
#: rollup was the one place that knew it.
#:
#: Measured on the author's store before this migration: a pooled keyword
#: profile over 25,837 plays put the string `1` at the top at 287.2, against
#: 33.0 for the real leader. Removing it also moved that leader to 40.5 — an
#: 18% shift, because the sentinel inflated every title's attribute count and
#: so dragged every genuine keyword down at the same time. The divisor error
#: reaches 41% on a title carrying one real keyword, which is why it does not
#: cancel across the library.
#:
#: **A separate namespace would not have fixed it.** The rollup scans every
#: namespace; only the key prefix hid these rows. Renaming them into a
#: `*_cursor` namespace and dropping the prefix would have turned one phantom
#: attribute per title into three the moment the edge sweeps ran. A separate
#: table is what actually removes the condition: a query against `enrichment`
#: cannot see bookkeeping, so no reader needs a convention — including a
#: consumer reading the published snapshot with plain SQLite, which ADR-0007
#: makes an expected thing to do.
#:
#: There is no `value` column, because a cursor never had a value. `'1'` was
#: filler; `fetched_at` was always the payload.
#:
#: A local `INSERT ... SELECT` and `DELETE`. No re-fetch, no TMDB request, no
#: rate limit — `fetched_at` rides across, so a sweep after this migration
#: still skips everything already fetched. The `_` prefix is stripped on the
#: way (`substr(key, 2)`): it meant "skip me", and in a table nothing else
#: reads there is nothing left for it to do.
_V7 = """
CREATE TABLE enrichment_cursor (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, namespace, key)
);

INSERT INTO enrichment_cursor (item_id, namespace, key, fetched_at)
SELECT item_id, namespace, substr(key, 2), fetched_at
FROM enrichment
WHERE key LIKE '\\_%' ESCAPE '\\';

DELETE FROM enrichment WHERE key LIKE '\\_%' ESCAPE '\\';
"""

#: Version 8 — `external_ids` gains `last_seen`, stamped by every walk that
#: observes an id (issue #57). Without it, "Plex stopped reporting this id"
#: and "Plex reported it once, a year ago, and we kept it" were the same row.
#: `plex_items.last_seen` is the precedent this follows, one level up: which
#: rating key was last seen.
#:
#: SQLite can't add a `NOT NULL` column with no default to a table that
#: already has rows, so this rebuilds the table the same way `_V5` did:
#: create the new shape, copy every row across with a backfilled value, drop
#: the old table, rename. There is no walk timestamp to backfill from — this
#: runs once, outside any walk — so every existing row gets `datetime('now')`
#: at migration time. That backfilled value means "present when this store
#: was migrated", not "Plex reported this id then"; the next walk is what
#: starts making the column mean what it is supposed to mean, correctly
#: refreshing every id Plex still reports and leaving stale the ones it no
#: longer does.
_V8 = """
CREATE TABLE external_ids_v8 (
    item_id   TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns        TEXT NOT NULL,
    value     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (ns, value, kind)
);
INSERT INTO external_ids_v8 (item_id, ns, value, kind, last_seen)
SELECT item_id, ns, value, kind, datetime('now')
FROM external_ids;
DROP TABLE external_ids;
ALTER TABLE external_ids_v8 RENAME TO external_ids;
CREATE INDEX idx_external_ids_item ON external_ids(item_id);
"""

#: Append-only. Index i takes the store from version i to version i+1.
MIGRATIONS: tuple[str, ...] = (_V1, _V2, _V3, _V4, _V5, _V6, _V7, _V8)

#: The version a store is at once every migration has been applied.
SCHEMA_VERSION = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    """The version this store is at. Zero means empty — nothing applied yet.

    Raises:
        StoreError: the store carries a `schema_version` table with no row. That
            is not an empty store — it is a damaged one, and treating it as
            empty would re-run migration 1 over tables that already exist and
            fail with a confusing "table already exists".
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    version = conn.execute("SELECT version FROM schema_version").fetchone()
    if version is None:
        raise StoreError(
            "store has a schema_version table but no version row — it is damaged. "
            "Restore it from a backup, or delete the file and re-run `plexdb init` "
            "to rebuild it from Plex."
        )
    return int(version[0])


def apply(conn: sqlite3.Connection) -> tuple[int, int]:
    """Bring a store up to `SCHEMA_VERSION`, and report where it started and ended.

    Applying an already-current store is a no-op, so this is safe to run on
    every startup — which is what makes `plexdb init` re-runnable.

    Raises:
        StoreError: the store is newer than this code understands. That means a
            newer writer has touched it, and proceeding could corrupt rows this
            version cannot see.
    """
    start = current_version(conn)
    if start > SCHEMA_VERSION:
        raise StoreError(
            f"store is at schema version {start}, but this build only understands "
            f"{SCHEMA_VERSION} — upgrade plex-db-ex rather than writing with an older one"
        )
    if start == SCHEMA_VERSION:
        return start, start

    # One transaction around the DDL *and* the version row, so a process killed
    # mid-migration leaves the store untouched rather than half-built.
    #
    # The BEGIN/COMMIT has to live inside the script text: `executescript`
    # commits any open transaction before it runs, so wrapping the call in
    # `with conn:` would not cover the DDL — and a crash between the tables
    # landing and the version row being written would leave a store that is
    # neither empty nor current, which no later run can repair.
    script = "BEGIN;\n"
    script += "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);\n"
    script += "".join(MIGRATIONS[start:])
    script += "DELETE FROM schema_version;\n"
    script += f"INSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION});\n"
    script += "COMMIT;\n"
    conn.executescript(script)
    return start, SCHEMA_VERSION
