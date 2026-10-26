-- Version 20 — the stored title map's region labels.
--
-- `title_map_label` holds the region labels `plexdb refresh-map` computes with
-- each kind's default map, written in the same transaction as `title_map`, so
-- Plex DB-EX serves a stored map without labelling 11,000 points per request. A
-- label holds the stored keyword, never display text: Plex DB-EX applies display
-- names (ADR-0021) as it serves. Every map stored before this version is stale
-- under the new drawing recipe, and the next refresh redraws it with labels.
-- Only adds, so `READER_SHAPE` stays.

CREATE TABLE title_map_label (
    kind   TEXT NOT NULL,
    node   INTEGER NOT NULL,
    x      REAL NOT NULL,
    y      REAL NOT NULL,
    width  REAL NOT NULL,
    height REAL NOT NULL,
    titles INTEGER NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (kind, node)
);
