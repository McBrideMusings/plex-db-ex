-- Version 1 — identity and enrichment. The four tables the first slice needs
-- and nothing more: what a title is, every id it is known by, where Plex keeps
-- it, and what has been learned about it.

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
