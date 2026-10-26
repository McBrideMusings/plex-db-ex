-- Version 18 — a pair only a person decided (ADR-0020).
--
-- No reader reads `keyword_pairs` through `plexdb-reader`, so `READER_SHAPE` stays.
--
-- Plex DB-EX's tag doctor lets a person merge two stored keywords no judge was
-- asked about. The fold writes such a pair as a row with `judged_at`,
-- `jev_score`, `jev_model` and `jev_error` all NULL and `decision` set, so
-- `merge_map` and the Merges tab read it from the one table every other merge
-- is in. A row says what a judge did (a score, or a refusal), what a person
-- did, or both; a row with neither is no row. Every existing row and its
-- decision carry over unchanged.

CREATE TABLE keyword_pairs_new (
    keyword_a  TEXT NOT NULL,
    keyword_b  TEXT NOT NULL,
    jev_score  REAL CHECK (jev_score BETWEEN 0 AND 1),
    jev_model  TEXT,
    judged_at  TEXT,
    decision   TEXT CHECK (decision IN ('accepted', 'rejected')),
    decided_at TEXT,
    jev_error  TEXT,
    PRIMARY KEY (keyword_a, keyword_b),
    CHECK (keyword_a < keyword_b),
    CHECK ((decision IS NULL) = (decided_at IS NULL)),
    CHECK ((jev_score IS NULL) = (jev_model IS NULL)),
    CHECK (jev_score IS NULL OR jev_error IS NULL),
    CHECK ((judged_at IS NULL) = (jev_score IS NULL AND jev_error IS NULL)),
    CHECK (judged_at IS NOT NULL OR decision IS NOT NULL)
);

INSERT INTO keyword_pairs_new SELECT keyword_a, keyword_b, jev_score, jev_model, judged_at,
                                     decision, decided_at, jev_error
    FROM keyword_pairs;

DROP TABLE keyword_pairs;
ALTER TABLE keyword_pairs_new RENAME TO keyword_pairs;
CREATE INDEX idx_keyword_pairs_b ON keyword_pairs(keyword_b);
