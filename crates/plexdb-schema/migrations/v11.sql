-- Version 11 — Plex DB-EX's stored tag network, the Graph view's
-- counterpart to version 9's stored title map.
--
-- `tag_network` holds one row per node: a tag of `kind` with its document
-- frequency and a position in the unit square. `tag_network_edge` holds one
-- row per edge: two tags and the titles they share, kept only when it is
-- among either tag's strongest co-tags. `tag_network_state` holds one row per
-- kind, the same fingerprint shape as `title_map_state`, naming the keyword
-- rows and drawing recipe the stored network came from.
--
-- All three are derived, so they carry no history and no foreign key: a tag
-- or title that drops out of `enrichment` or `items` drops out of the next
-- `plexdb refresh-tagnetwork`'s network without it. Purely additive — no rows
-- in an existing table move, so there is nothing to lose.

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
