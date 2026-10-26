-- Version 6 — collection membership (issue #34). The crowd lists a title
-- appears on, and where in them. Two tables rather than one because a list's
-- own facts — how long it is, how many people follow it — belong to the list,
-- not to one title's place in it; carrying them per membership would repeat
-- them thousands of times and let two rows disagree about the same list.
--
-- **There is no `weight` column, and there will not be one** (issue #33,
-- ADR-0012). One number would have to mean "third of a hundred" for a ranked
-- list, "on a list eight thousand people follow" for a popular one, and "named
-- in seven comments" for a subreddit — three different measurements — and once
-- computed it cannot be inverted back into the facts it came from. That is the
-- same objection `_V4` records against a stored `counts_as_signal`. The store
-- records what the source said; the consumer weighs it.
--
-- Every column past the keys is nullable on purpose. A source fills what it
-- genuinely has and leaves the rest empty rather than inventing a value, so a
-- missing `rank` stays distinguishable from rank 1. MDBList fills `rank`,
-- `size` and `likes`; a subreddit would fill only `mentions`.
--
-- `item_id` references `items`, populated only by `plexdb walk` (ADR-0005), so
-- a list entry naming a title this library has never walked has nothing to
-- point at. It is dropped and counted, never stored under an invented id —
-- the same rule ADR-0009 sets for edges. `size` still records the list's full
-- length, so a consumer can see how much of it was missed.

CREATE TABLE collection (
    collection_id TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    name          TEXT,
    url           TEXT,
    size          INTEGER,
    likes         INTEGER,
    observed_at   TEXT NOT NULL
);
CREATE INDEX idx_collection_source ON collection(source);

CREATE TABLE collection_membership (
    collection_id TEXT NOT NULL REFERENCES collection(collection_id) ON DELETE CASCADE,
    item_id       TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    rank          INTEGER,
    mentions      INTEGER,
    observed_at   TEXT NOT NULL,
    PRIMARY KEY (collection_id, item_id)
);
CREATE INDEX idx_collection_membership_item ON collection_membership(item_id);
