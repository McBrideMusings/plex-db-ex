-- Version 19 — a person's display name for a keyword (ADR-0021).
--
-- `keyword_names` holds at most one name per stored keyword, written only by
-- `plexdb fold-name-decisions` from Plex DB-EX's `name_decisions.json`. A reader
-- shows it in place of the keyword's readable spelling; nothing that computes
-- reads it. Only adds, so `READER_SHAPE` stays.

CREATE TABLE keyword_names (
    keyword    TEXT NOT NULL PRIMARY KEY,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    decided_at TEXT NOT NULL
);
