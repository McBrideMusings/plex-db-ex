-- Version 2 — watch history (ADR-0004, issue #8). One row per Plex history
-- event. `history_key` is Plex's own identity for the *viewing event* — not
-- `plex_items.rating_key`, which is the *title's* identity — and is what
-- makes a re-ingest idempotent: `INSERT ... ON CONFLICT(history_key) DO
-- NOTHING` skips whatever a prior ingest already wrote.
--
-- `ip`, `percent_complete`, and `paused_counter` are populated only by the
-- Tautulli adapter (issue #9); null here is the normal state of a Plex-only
-- deployment, not a degraded one.

CREATE TABLE plays (
    history_key       TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    plex_account_id   INTEGER NOT NULL,
    client_identifier TEXT,
    platform          TEXT,
    viewed_at         INTEGER NOT NULL,
    ip                TEXT,
    percent_complete  INTEGER,
    paused_counter    INTEGER
);
CREATE INDEX idx_plays_item ON plays(item_id);
CREATE INDEX idx_plays_viewed_at ON plays(viewed_at);

-- The incremental-fetch watermark `ingest_plays` reads at the start of a run
-- and advances at the end. Deliberately NOT derived from MAX(plays.viewed_at):
-- an event skipped as unresolved is never written to `plays`, so a watermark
-- built only from written rows would silently advance past it the moment any
-- newer event succeeds — and it would never be asked for again, even after a
-- later walk resolves it. This single row instead tracks the oldest event
-- still outstanding (or the newest event fully accounted for, once nothing is
-- outstanding), so the next run always asks Plex for everything from there
-- forward.
CREATE TABLE plays_ingest_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    since_viewed_at INTEGER NOT NULL
);
