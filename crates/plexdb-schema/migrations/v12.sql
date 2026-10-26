-- Version 12 — what Jev said about whether two keywords mean the same thing
-- (ADR-0018).
--
-- `keyword_pairs` holds one row per unordered pair of stored keywords a judge
-- has been asked about, `keyword_a < keyword_b`, both values as they stand in
-- `enrichment` (normalized and stemmed). `jev_score` is the judge's own answer
-- to "do these two mean the same thing", 0 to 1, stored verbatim with the
-- model that gave it; the pair stays in the table even when the answer is
-- "no", so the writer never asks twice. `decision` is what a person said about
-- the pair — `accepted` or `rejected`, NULL while nobody has — and `decided_at`
-- says when.
--
-- Nothing here records whether a pair counts as a merge: that threshold is
-- the reader's (ADR-0012), and `enrichment` is never rewritten, so a merge is
-- undone by changing one `decision`, and no keyword a source wrote is lost.
-- Purely additive — no rows in an existing table move.

CREATE TABLE keyword_pairs (
    keyword_a  TEXT NOT NULL,
    keyword_b  TEXT NOT NULL,
    jev_score  REAL NOT NULL CHECK (jev_score BETWEEN 0 AND 1),
    jev_model  TEXT NOT NULL,
    judged_at  TEXT NOT NULL,
    decision   TEXT CHECK (decision IN ('accepted', 'rejected')),
    decided_at TEXT,
    PRIMARY KEY (keyword_a, keyword_b),
    CHECK (keyword_a < keyword_b),
    CHECK ((decision IS NULL) = (decided_at IS NULL))
);

CREATE INDEX idx_keyword_pairs_b ON keyword_pairs(keyword_b);
