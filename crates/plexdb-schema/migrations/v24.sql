-- Version 24 — the verdict of checking each file.
--
-- `media_check` holds one row per `(part_id, kind)`: `header` is `plexdb check-files`'s
-- ffprobe pass against the file's rules, `decode` is the full-decode pass. A re-check
-- replaces the row. A part Plex drops or re-reads (`media_walk` replaces a changed
-- record's parts whole) takes its checks with it, so a part with no `header` row is new or
-- changed and is the next one probed. `error` is NULL when `ok` is 1.

CREATE TABLE media_check (
    part_id    INTEGER NOT NULL REFERENCES media_part(part_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('header', 'decode')),
    checked_at TEXT NOT NULL,
    ok         INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error      TEXT,
    PRIMARY KEY (part_id, kind)
);
