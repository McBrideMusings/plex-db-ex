-- Version 13 — a pair the judge refused.
--
-- Jev answers 400 or 422 for some pairs, and asking again gets the same answer.
-- Such a pair is stored with `jev_error` set and `jev_score` and `jev_model`
-- NULL, so it is not asked again and no longer holds up the batch it was in.
-- A readable score is exactly when `jev_error` is NULL. SQLite cannot relax a
-- NOT NULL in place, so the table is rebuilt; every row and its decision carry
-- over unchanged.

CREATE TABLE keyword_pairs_new (
    keyword_a  TEXT NOT NULL,
    keyword_b  TEXT NOT NULL,
    jev_score  REAL CHECK (jev_score BETWEEN 0 AND 1),
    jev_model  TEXT,
    judged_at  TEXT NOT NULL,
    decision   TEXT CHECK (decision IN ('accepted', 'rejected')),
    decided_at TEXT,
    jev_error  TEXT,
    PRIMARY KEY (keyword_a, keyword_b),
    CHECK (keyword_a < keyword_b),
    CHECK ((decision IS NULL) = (decided_at IS NULL)),
    CHECK ((jev_error IS NULL) = (jev_score IS NOT NULL)),
    CHECK ((jev_error IS NULL) = (jev_model IS NOT NULL))
);

INSERT INTO keyword_pairs_new (keyword_a, keyword_b, jev_score, jev_model, judged_at,
                               decision, decided_at)
    SELECT keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at
    FROM keyword_pairs;

DROP TABLE keyword_pairs;
ALTER TABLE keyword_pairs_new RENAME TO keyword_pairs;
CREATE INDEX idx_keyword_pairs_b ON keyword_pairs(keyword_b);
