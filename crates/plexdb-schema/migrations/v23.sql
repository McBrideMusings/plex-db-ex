-- Version 23 — the files and tracks Plex lists for each title.
--
-- `media_part` holds one row per file Plex lists for a movie or an episode, keyed by
-- Plex's own part id. `rating_key` names the Plex record the part was read from: a title
-- Plex lists twice has two rating keys under one `item_id`, and each record's parts are
-- replaced on their own. `plex_updated_at` is the record's `updatedAt` (epoch seconds) when
-- the part was read; `plexdb media-walk` fetches a record again only when Plex reports a
-- different one. `media_stream` holds the video, audio and subtitle tracks of one part;
-- `idx` is Plex's own stream index. Facts about titles, so neither table is bookkeeping
-- (ADR-0013). Only adds, so `READER_SHAPE` stays.

CREATE TABLE media_part (
    part_id         INTEGER PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    rating_key      TEXT NOT NULL,
    file            TEXT NOT NULL,
    size            INTEGER,
    container       TEXT,
    video_codec     TEXT,
    audio_codec     TEXT,
    height          INTEGER,
    bitrate         INTEGER,
    duration_ms     INTEGER,
    plex_updated_at INTEGER,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX idx_media_part_item ON media_part(item_id);
CREATE INDEX idx_media_part_rating_key ON media_part(rating_key);

CREATE TABLE media_stream (
    part_id INTEGER NOT NULL REFERENCES media_part(part_id) ON DELETE CASCADE,
    idx     INTEGER NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('video', 'audio', 'subtitle')),
    codec   TEXT,
    lang    TEXT,
    forced  INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0, 1)),
    title   TEXT,
    PRIMARY KEY (part_id, idx)
);
