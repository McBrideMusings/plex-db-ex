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

#: Append-only. Index i takes the store from version i to version i+1.
MIGRATIONS: tuple[str, ...] = (_V1,)

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
