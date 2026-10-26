-- Version 8 — `external_ids` gains `last_seen`, stamped by every walk that
-- observes an id (issue #57). Without it, "Plex stopped reporting this id"
-- and "Plex reported it once, a year ago, and we kept it" were the same row.
-- `plex_items.last_seen` is the precedent this follows, one level up: which
-- rating key was last seen.
--
-- SQLite can't add a `NOT NULL` column with no default to a table that
-- already has rows, so this rebuilds the table the same way `_V5` did:
-- create the new shape, copy every row across with a backfilled value, drop
-- the old table, rename. There is no walk timestamp to backfill from — this
-- runs once, outside any walk — so every existing row gets `datetime('now')`
-- at migration time. That backfilled value means "present when this store
-- was migrated", not "Plex reported this id then"; the next walk is what
-- starts making the column mean what it is supposed to mean, correctly
-- refreshing every id Plex still reports and leaving stale the ones it no
-- longer does.

CREATE TABLE external_ids_v8 (
    item_id   TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns        TEXT NOT NULL,
    value     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (ns, value, kind)
);
INSERT INTO external_ids_v8 (item_id, ns, value, kind, last_seen)
SELECT item_id, ns, value, kind, datetime('now')
FROM external_ids;
DROP TABLE external_ids;
ALTER TABLE external_ids_v8 RENAME TO external_ids;
CREATE INDEX idx_external_ids_item ON external_ids(item_id);
