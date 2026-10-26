
-- One row per title the harvester has attempted. `error` is the last failure's
-- reason, and NULL once the title indexed cleanly. `file_hash` is the part's
-- size + mtime + first MiB; a different hash means the file was replaced and
-- the title is indexed again.
CREATE TABLE frame_index_state (
    item_id       TEXT PRIMARY KEY,
    part_id       INTEGER,
    model         TEXT,
    model_version TEXT,
    dims          INTEGER,
    quant         TEXT,
    duration_ms   INTEGER,
    bytes         INTEGER,
    file_hash     TEXT,
    shots         INTEGER,
    vectors       INTEGER,
    indexed_at    TEXT NOT NULL,
    error         TEXT
);

-- One vector per Shot, plus one every 2 s inside a longer Shot. Partitioned by
-- item_id, so a search over a pool of titles reads only those titles' chunks.
CREATE VIRTUAL TABLE frame_vec USING vec0(
    item_id TEXT PARTITION KEY,
    t_ms    INTEGER,
    shot_ix INTEGER,
    vec     int8[768]
);

-- Shot boundaries: scdet's scene changes, and the file's chapter marks.
CREATE TABLE cuts (
    item_id TEXT NOT NULL,
    t_ms    INTEGER NOT NULL,
    score   REAL,
    source  TEXT NOT NULL CHECK (source IN ('scdet', 'chapter')),
    PRIMARY KEY (item_id, source, t_ms)
);

-- Titles a person asked for ahead of the backlog, served oldest first.
CREATE TABLE frame_queue (
    item_id   TEXT PRIMARY KEY,
    queued_at TEXT NOT NULL,
    queued_by TEXT NOT NULL
);
