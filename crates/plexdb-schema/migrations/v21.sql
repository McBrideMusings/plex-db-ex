-- Version 21 — every entry of a crowd list, and how the list arrived (ADR-0027).
--
-- `collection_entry` holds one row per entry the source listed, owned or not.
-- It carries no `item_id`: a reader resolves an entry through `external_ids`
-- when it reads, so a title walked later joins its lists with nothing
-- rewritten. `(ns, value, kind)` is the strongest external id the source gave,
-- in `identity.PRIORITY` order, scoped by media type as `external_ids` is.
-- `title` and `year` are the source's own words, kept so an unowned entry can
-- be named. `rank` is the 1-based position, NULL where the source is unordered.
--
-- `collection.provenance` says how the list arrived: `feed` (a source's own
-- top lists), `added` (a person named it), `search` (found from a seed of
-- titles, with `seed_count` the number of seed titles). Every list stored
-- before this version came from the MDBList feed. Only adds, so
-- `READER_SHAPE` stays.

CREATE TABLE collection_entry (
    collection_id TEXT NOT NULL REFERENCES collection(collection_id) ON DELETE CASCADE,
    rank          INTEGER,
    ns            TEXT NOT NULL,
    value         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT,
    year          INTEGER,
    PRIMARY KEY (collection_id, ns, value, kind)
);
CREATE INDEX idx_collection_entry_id ON collection_entry(ns, value, kind);

ALTER TABLE collection ADD COLUMN provenance TEXT NOT NULL DEFAULT 'feed'
    CHECK (provenance IN ('feed', 'added', 'search'));
ALTER TABLE collection ADD COLUMN seed_count INTEGER;
