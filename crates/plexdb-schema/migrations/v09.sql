-- Version 9 — Plex DB-EX's default title map, drawn by the writer and
-- stored, so Plex DB-EX serves it without drawing anything.
--
-- `title_map` holds one row per placed title and kind: a position in the unit
-- square, where only distances mean anything. `title_map_state` holds one row
-- per kind: the fingerprint of the keyword rows the map was drawn from, how
-- many keyword-carrying titles it left off, and when. Nothing interprets the
-- fingerprint except Plex DB-EX's freshness check, which recomputes it from
-- the current keyword rows and serves the stored map only when the two match.
--
-- Both tables are derived, so they carry no history and no foreign key: a
-- title that leaves `items` drops out of Plex DB-EX's join, and the next
-- `plexdb refresh-map` redraws the map without it. Purely additive — no rows
-- in an existing table move, so there is nothing to lose.

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
