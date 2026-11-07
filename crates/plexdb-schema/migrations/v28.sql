-- Version 28 — audio facts per file (plex-db-ex-9ak.1).
--
-- `audio_facts` is what the analyzer said about one file's content: its JSON object
-- verbatim, keyed by the file hash (size, modification time and first MiB), so a file
-- Plex lists twice, or moves, is analyzed once. `audio_part` says which analyzed
-- content a Plex media part has. It carries no foreign key to `media_part`: media-walk
-- deletes and re-inserts a record's parts, and a cascade would wipe these rows.
-- `audio_harvest_state` is the harvester's own bookkeeping, one row per part, and never
-- a fact about a title (ADR-0013): its outcome, attempts and next retry. Additive;
-- `reader_shape` stays.

CREATE TABLE audio_facts (
    file_hash   TEXT PRIMARY KEY,
    facts       TEXT NOT NULL,
    analyzed_at TEXT NOT NULL
);

CREATE TABLE audio_part (
    part_id   INTEGER PRIMARY KEY,
    item_id   TEXT NOT NULL,
    file_hash TEXT NOT NULL
);
CREATE INDEX idx_audio_part_item ON audio_part(item_id);

CREATE TABLE audio_harvest_state (
    part_id     INTEGER PRIMARY KEY,
    item_id     TEXT NOT NULL,
    size        INTEGER,
    file_hash   TEXT,
    outcome     TEXT NOT NULL CHECK (outcome IN ('done', 'failed', 'unreachable', 'excluded')),
    error       TEXT,
    attempts    INTEGER NOT NULL,
    next_try_at TEXT,
    updated_at  TEXT NOT NULL
);
