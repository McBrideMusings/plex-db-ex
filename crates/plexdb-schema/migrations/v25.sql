-- Version 25 — extras libraries (ADR-0030).
--
-- A library is a folder Plex does not index, declared by a person in `extras.json`.
-- `extra_library` and `extra_field` hold the declaration as last folded; `extra_item` is one
-- row per extra beside its `items` row (type `extra`); `extra_file` is one row per file on
-- disk, several for an item whose exact copies or near duplicates were folded into it.
-- `extra_merge`, `extra_moved` and `extra_check` are written by the steps that fold near
-- duplicates, organize files and check them. Every table is additive; `reader_shape` stays.

CREATE TABLE extra_library (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL,
    type        TEXT NOT NULL,
    template    TEXT,
    declared_at TEXT NOT NULL
);

CREATE TABLE extra_field (
    library  TEXT NOT NULL REFERENCES extra_library(name) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('text', 'int', 'year', 'list')),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    PRIMARY KEY (library, name)
);

CREATE TABLE extra_item (
    item_id      TEXT PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    library      TEXT NOT NULL REFERENCES extra_library(name),
    organized    INTEGER NOT NULL CHECK (organized IN (0, 1)),
    source_url   TEXT,
    first_seen   TEXT NOT NULL,
    organized_at TEXT
);
CREATE INDEX idx_extra_item_library ON extra_item(library, organized);

CREATE TABLE extra_file (
    path          TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL REFERENCES extra_item(item_id) ON DELETE CASCADE,
    primary_file  INTEGER NOT NULL CHECK (primary_file IN (0, 1)),
    size          INTEGER,
    mtime         INTEGER,
    sha256        TEXT,
    height        INTEGER,
    duration_ms   INTEGER,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    missing_since TEXT
);
CREATE INDEX idx_extra_file_item ON extra_file(item_id);
CREATE INDEX idx_extra_file_sha ON extra_file(sha256);

CREATE TABLE extra_merge (
    merged_item_id TEXT PRIMARY KEY,
    kept_item_id   TEXT NOT NULL REFERENCES extra_item(item_id) ON DELETE CASCADE,
    score          REAL NOT NULL,
    merged_at      TEXT NOT NULL
);

CREATE TABLE extra_moved (
    move_id   INTEGER PRIMARY KEY,
    from_path TEXT NOT NULL,
    to_path   TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    moved_at  TEXT NOT NULL
);

CREATE TABLE extra_check (
    path       TEXT NOT NULL REFERENCES extra_file(path) ON UPDATE CASCADE ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('header', 'decode')),
    checked_at TEXT NOT NULL,
    ok         INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error      TEXT,
    PRIMARY KEY (path, kind)
);
