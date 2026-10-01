"""The store's schema, and the code that applies it.

The schema **is** the public API of this project (ADR-0001). One process writes
this file; every consumer opens it read-only, and there is no version
negotiation — so a change here is a breaking change for every consumer at once.

Migrations are an append-only list: index 0 is the step that takes an empty
database to version 1, index 1 takes it to version 2, and so on. Never edit a
step that has shipped; add another. Most steps are a batch of SQL, run inside
one transaction; a step needing logic no SQL statement can express — version
10's keyword re-stemming — is a plain function taking the connection instead,
run inside the same kind of transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from . import keywords
from .errors import StoreError

#: A migration is either a batch of SQL (every version through 9) or a Python
#: step (version 10 on) — stemming needs the `snowballstemmer` package, which
#: no SQL batch can call. `apply` below is what runs either kind.
#:
#: A Python step may return a mapping of guarded-table name -> the exact row
#: count it expects that table to hold once the step lands, for a table it
#: intentionally shrinks (`store._verify` reads this — see `_V10`). `None` (or
#: an empty mapping) means "this step never shrinks a guarded table on
#: purpose," which is true of every step that isn't `_V10` today.
DeclaredShrinks = dict[str, int]
Migration = str | Callable[[sqlite3.Connection], DeclaredShrinks | None]

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

#: Version 9 — the tag explorer's default title map, drawn by the writer and
#: stored, so the explorer serves it without drawing anything.
#:
#: `title_map` holds one row per placed title and kind: a position in the unit
#: square, where only distances mean anything. `title_map_state` holds one row
#: per kind: the fingerprint of the keyword rows the map was drawn from, how
#: many keyword-carrying titles it left off, and when. Nothing interprets the
#: fingerprint except the explorer's freshness check, which recomputes it from
#: the current keyword rows and serves the stored map only when the two match.
#:
#: Both tables are derived, so they carry no history and no foreign key: a
#: title that leaves `items` drops out of the explorer's join, and the next
#: `plexdb refresh-map` redraws the map without it. Purely additive — no rows
#: in an existing table move, so there is nothing to lose.
_V9 = """
CREATE TABLE title_map (
    kind    TEXT NOT NULL,
    item_id TEXT NOT NULL,
    x       REAL NOT NULL,
    y       REAL NOT NULL,
    PRIMARY KEY (kind, item_id)
);

CREATE TABLE title_map_state (
    kind        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    unplaced    INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);
"""


#: Version 10 — one keyword namespace, per-source, normalized and stemmed
#: (ADR-0016). `tmdb_keywords` renames to `keywords`; every existing keyword row
#: backfills `source='tmdb'`, any other namespace present backfills `source`
#: to its own namespace name (there being no other writer of `enrichment` yet
#: to name a truer one); every keyword value is re-normalized and re-stemmed
#: through `keywords.normalize_keyword`, merging rows that collide once
#: `Heists` and `heist` land on the same stemmed value; and `keyword_forms`
#: is filled from every pre-migration raw value so a reader can still show the
#: spelling a source actually used. `enrichment_cursor` gets the same `source`
#: column, backfilled the same way, so a per-title fetch cursor is scoped to
#: one source exactly like the facts it vouches for.
#:
#: A plain function, not a SQL batch: stemming needs `snowballstemmer`, which
#: no `CREATE TABLE`/`INSERT` can call. It rebuilds `enrichment` the same way
#: `_V5` and `_V8` rebuilt `external_ids` — SQLite cannot add a column to an
#: existing primary key — but has to do the column backfill and the value
#: normalization in Python because the second one needs the stemmer.
#:
#: Where two pre-migration rows collide after normalization (`heists` and
#: `heist` on the same item, from the same source), the newer `fetched_at`
#: wins — the same "the newest fact stands" rule every write path already
#: follows when it replaces a title's row set on a fresh fetch.
#:
#: **This is the one migration that can legitimately shrink `enrichment`.**
#: `store._verify` rolls a migration back if a guarded table (`enrichment`
#: among them) holds fewer rows afterwards than before — the right call for
#: every migration except this one, whose whole point is to collapse rows
#: that normalize to the same fact. Comparing row counts alone cannot tell
#: "two pre-migration spellings landed on one fact, on purpose" apart from
#: "this migration silently dropped something" — both look like a shrink. So
#: this function returns the exact post-migration `enrichment` row count it
#: computed (`len(merged)`) as a declared shrink: `_verify` accepts a drop in
#: a guarded table only when the actual after-count matches the declared one
#: exactly, so a `_V10` that *also* dropped an unrelated row — a real bug —
#: still lands on a different number than declared and still trips the guard.
def _V10(conn: sqlite3.Connection) -> DeclaredShrinks:
    conn.execute("CREATE TABLE keyword_forms (surface TEXT PRIMARY KEY, keyword TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE enrichment_v10 (
            item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
            namespace  TEXT NOT NULL,
            source     TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (item_id, namespace, source, key, value)
        )
        """
    )

    rows = conn.execute(
        "SELECT item_id, namespace, key, value, fetched_at FROM enrichment"
    ).fetchall()

    # Keyed on the *post*-migration primary key, so two pre-migration rows that
    # normalize to the same value merge here rather than raising on insert.
    merged: dict[tuple[str, str, str, str, str], str] = {}
    for item_id, namespace, key, value, fetched_at in rows:
        if namespace == "tmdb_keywords":
            new_namespace = keywords.NAMESPACE
            source = "tmdb"
            stored_value = keywords.upsert_keyword_form(conn, value)
        else:
            # No other writer of `enrichment` exists yet (docs/schema.md's
            # "Namespaces in use" lists exactly one), so the namespace itself
            # is the only fitting source a migration run against a real store
            # could name without guessing.
            new_namespace = namespace
            source = namespace
            stored_value = value

        row_key = (item_id, new_namespace, source, key, stored_value)
        if row_key not in merged or fetched_at > merged[row_key]:
            merged[row_key] = fetched_at

    conn.executemany(
        "INSERT INTO enrichment_v10 (item_id, namespace, source, key, value, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (item_id, ns, source, key, value, fetched_at)
            for (item_id, ns, source, key, value), fetched_at in merged.items()
        ],
    )

    conn.execute("DROP TABLE enrichment")
    conn.execute("ALTER TABLE enrichment_v10 RENAME TO enrichment")
    conn.execute("CREATE INDEX idx_enrichment_ns_key ON enrichment(namespace, key)")
    conn.execute("CREATE INDEX idx_enrichment_item_ns ON enrichment(item_id, namespace)")

    # `enrichment_cursor` gains the same `source` column as `enrichment`
    # above, but its fallback rule cannot be "the namespace names its own
    # source" — unlike `enrichment` (whose one known writer already collapsed
    # namespace and source to the same string, `tmdb_keywords`), this table
    # already has a *second* real writer whose namespace and source differ:
    # `tmdb_edges.py` writes cursor rows under `namespace='tmdb_edges'` but
    # has always meant `source='tmdb'` — both of this table's edge-fetch cache
    # entries and the keyword-fetch one come from the one enrichment source
    # this store has ever had. So both known cursor namespaces backfill to
    # `source='tmdb'` explicitly; only a namespace this migration cannot name
    # falls back to using itself as source, the same guess `enrichment` makes
    # for a hypothetical unknown writer.
    #
    # No two pre-migration cursor rows ever collide once `source` is added: a
    # cursor's key is per (item_id, namespace, key) today, one row per title
    # per writer, and every writer keeps its own key — so this is a plain
    # column-add-and-copy, never a merge, and never a guarded-table shrink.
    _CURSOR_NAMESPACE_SOURCES = {"tmdb_keywords": "tmdb", "tmdb_edges": "tmdb"}
    conn.execute(
        """
        CREATE TABLE enrichment_cursor_v10 (
            item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
            namespace  TEXT NOT NULL,
            source     TEXT NOT NULL,
            key        TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (item_id, namespace, source, key)
        )
        """
    )
    cursor_rows = conn.execute(
        "SELECT item_id, namespace, key, fetched_at FROM enrichment_cursor"
    ).fetchall()
    conn.executemany(
        "INSERT INTO enrichment_cursor_v10 (item_id, namespace, source, key, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                item_id,
                keywords.NAMESPACE if namespace == "tmdb_keywords" else namespace,
                _CURSOR_NAMESPACE_SOURCES.get(namespace, namespace),
                key,
                fetched_at,
            )
            for item_id, namespace, key, fetched_at in cursor_rows
        ],
    )
    conn.execute("DROP TABLE enrichment_cursor")
    conn.execute("ALTER TABLE enrichment_cursor_v10 RENAME TO enrichment_cursor")

    return {"enrichment": len(merged)}


#: Version 11 — the tag explorer's stored tag network, the Graph view's
#: counterpart to version 9's stored title map.
#:
#: `tag_network` holds one row per node: a tag of `kind` with its document
#: frequency and a position in the unit square. `tag_network_edge` holds one
#: row per edge: two tags and the titles they share, kept only when it is
#: among either tag's strongest co-tags. `tag_network_state` holds one row per
#: kind, the same fingerprint shape as `title_map_state`, naming the keyword
#: rows and drawing recipe the stored network came from.
#:
#: All three are derived, so they carry no history and no foreign key: a tag
#: or title that drops out of `enrichment` or `items` drops out of the next
#: `plexdb refresh-tagnetwork`'s network without it. Purely additive — no rows
#: in an existing table move, so there is nothing to lose.
_V11 = """
CREATE TABLE tag_network (
    kind  TEXT NOT NULL,
    value TEXT NOT NULL,
    df    INTEGER NOT NULL,
    x     REAL NOT NULL,
    y     REAL NOT NULL,
    PRIMARY KEY (kind, value)
);

CREATE TABLE tag_network_edge (
    kind   TEXT NOT NULL,
    a      TEXT NOT NULL,
    b      TEXT NOT NULL,
    shared INTEGER NOT NULL,
    PRIMARY KEY (kind, a, b)
);

CREATE TABLE tag_network_state (
    kind        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
"""


#: Version 12 — what Jev said about whether two keywords mean the same thing
#: (ADR-0018).
#:
#: `keyword_pairs` holds one row per unordered pair of stored keywords a judge
#: has been asked about, `keyword_a < keyword_b`, both values as they stand in
#: `enrichment` (normalized and stemmed). `jev_score` is the judge's own answer
#: to "do these two mean the same thing", 0 to 1, stored verbatim with the
#: model that gave it; the pair stays in the table even when the answer is
#: "no", so the writer never asks twice. `decision` is what a person said about
#: the pair — `accepted` or `rejected`, NULL while nobody has — and `decided_at`
#: says when.
#:
#: Nothing here records whether a pair counts as a merge: that threshold is
#: the reader's (ADR-0012), and `enrichment` is never rewritten, so a merge is
#: undone by changing one `decision`, and no keyword a source wrote is lost.
#: Purely additive — no rows in an existing table move.
_V12 = """
CREATE TABLE keyword_pairs (
    keyword_a  TEXT NOT NULL,
    keyword_b  TEXT NOT NULL,
    jev_score  REAL NOT NULL CHECK (jev_score BETWEEN 0 AND 1),
    jev_model  TEXT NOT NULL,
    judged_at  TEXT NOT NULL,
    decision   TEXT CHECK (decision IN ('accepted', 'rejected')),
    decided_at TEXT,
    PRIMARY KEY (keyword_a, keyword_b),
    CHECK (keyword_a < keyword_b),
    CHECK ((decision IS NULL) = (decided_at IS NULL))
);

CREATE INDEX idx_keyword_pairs_b ON keyword_pairs(keyword_b);
"""


#: Append-only. Index i takes the store from version i to version i+1.
MIGRATIONS: tuple[Migration, ...] = (
    _V1,
    _V2,
    _V3,
    _V4,
    _V5,
    _V6,
    _V7,
    _V8,
    _V9,
    _V10,
    _V11,
    _V12,
)

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
            "Restore it from a backup, or delete the file and re-run `plexdb migrate` "
            "to rebuild it from Plex."
        )
    return int(version[0])


def apply(conn: sqlite3.Connection) -> tuple[int, int, DeclaredShrinks]:
    """Bring a store up to `SCHEMA_VERSION`, and report where it started and
    ended, plus any guarded-table shrink a step declared as intended.

    Applying an already-current store is a no-op, so this is safe to run on
    every startup — which is what makes `plexdb migrate` re-runnable.

    The third element is every `{table: expected_row_count}` a Python step
    returned along the way (see `Migration`'s docstring and `_V10`) — empty
    for a run that touched no such step, which is every run today except one
    crossing v9 -> v10. `store._verify` is the only reader.

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
        return start, start, {}

    conn.executescript(
        "BEGIN;\nCREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);\nCOMMIT;\n"
    )

    declared_shrinks: DeclaredShrinks = {}

    # One transaction per step, around its DDL/data *and* the version row, so a
    # process killed mid-migration leaves the store at the last step that fully
    # landed rather than half-built. A SQL batch gets this via `executescript`
    # (whose BEGIN/COMMIT has to live inside the script text — `executescript`
    # commits any open transaction before it runs, so wrapping the call in
    # `with conn:` would not cover the DDL); a Python step gets it via an
    # explicit BEGIN/COMMIT around the call, rolled back on any exception.
    for index in range(start, SCHEMA_VERSION):
        migration = MIGRATIONS[index]
        version = index + 1
        if isinstance(migration, str):
            conn.executescript(
                "BEGIN;\n"
                + migration
                + "\nDELETE FROM schema_version;\n"
                + f"INSERT INTO schema_version (version) VALUES ({version});\n"
                + "COMMIT;\n"
            )
        else:
            conn.execute("BEGIN")
            try:
                result = migration(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if result:
                declared_shrinks.update(result)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")

    return start, SCHEMA_VERSION, declared_shrinks
