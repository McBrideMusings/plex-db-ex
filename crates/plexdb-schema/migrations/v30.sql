-- Version 30 — audio facts for extras, reader shape 30 (plex-db-ex-9ak.5).
--
-- An extra has no Plex media part, so its audio facts cannot hang off `audio_part`.
-- `audio_extra` says which analyzed content (`audio_facts.file_hash`) an extra file has;
-- `audio_extra_state` is the harvester's own bookkeeping for it, the same columns as
-- `audio_harvest_state`. Both are keyed by `extra_file.path` with ON UPDATE CASCADE, so a
-- move that renames the path in place (extras-organize, extras-walk) keeps the rows, and
-- ON DELETE CASCADE drops them with the file. `audio_extra.item_id` is a convenience copy
-- with no foreign key, like `audio_part.item_id`: a reader resolves the item through
-- `extra_file`. `audio_facts` is shared with `audio_part`, keyed by content hash.
-- `plexdb-reader`'s audio accessors now also read `extra_file` and `audio_extra`.

CREATE TABLE audio_extra (
    path      TEXT PRIMARY KEY REFERENCES extra_file(path) ON UPDATE CASCADE ON DELETE CASCADE,
    item_id   TEXT NOT NULL,
    file_hash TEXT NOT NULL
);
CREATE INDEX idx_audio_extra_item ON audio_extra(item_id);

CREATE TABLE audio_extra_state (
    path        TEXT PRIMARY KEY REFERENCES extra_file(path) ON UPDATE CASCADE ON DELETE CASCADE,
    item_id     TEXT NOT NULL,
    size        INTEGER,
    file_hash   TEXT,
    outcome     TEXT NOT NULL CHECK (outcome IN ('done', 'failed', 'unreachable', 'excluded')),
    error       TEXT,
    attempts    INTEGER NOT NULL,
    next_try_at TEXT,
    updated_at  TEXT NOT NULL
);

UPDATE reader_shape SET version = 30;
