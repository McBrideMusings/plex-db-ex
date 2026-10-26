-- Version 22 — a person's decisions about crowd lists, and the lists whose fetch failed.
--
-- `list_decision` holds the last decision per list URL, written only by `plexdb
-- fold-list-decisions` from Plex DB-EX's `list_decisions.json`: `added`,
-- `excluded`, `refetch`, `retry` or `removed`. `added` says whether a person added the
-- list, since a later `refetch` replaces the decision in the file but does not make the
-- list any less added. `list_attempt` holds a failing list's fetch bookkeeping, keyed by
-- the list's URL (its `collection_id` where the source gave no URL): how many fetches in
-- a row failed, when the last one did, and why. `enrichment_cursor` cannot hold it — it
-- is keyed by `items`, and a list is not an item (ADR-0023). Both are bookkeeping, never
-- facts (ADR-0013). Only adds, so `READER_SHAPE` stays.

CREATE TABLE list_decision (
    url        TEXT NOT NULL PRIMARY KEY,
    decision   TEXT NOT NULL
        CHECK (decision IN ('added', 'excluded', 'refetch', 'retry', 'removed')),
    decided_at TEXT NOT NULL,
    added      INTEGER NOT NULL CHECK (added IN (0, 1))
);

CREATE TABLE list_attempt (
    list_key     TEXT NOT NULL PRIMARY KEY,
    attempts     INTEGER NOT NULL CHECK (attempts > 0),
    attempted_at TEXT NOT NULL,
    reason       TEXT NOT NULL
);
