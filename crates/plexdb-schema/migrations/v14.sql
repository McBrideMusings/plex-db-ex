-- Version 14 — a source's own tag rank, and what role a keyword plays
-- (ADR-0019).
--
-- `enrichment.rank` is the rank a source gave that row, stored verbatim —
-- AniList's 0–100 vote share for a tag — and NULL wherever the source gives
-- none, which is every row already in the store.
--
-- `keyword_roles` holds one row per (keyword, role, source): a role a source
-- states (Wikidata's narrative location is a region) with `score` NULL, or a
-- judge's answer with its own 0–1 `score` and `model`. A judge that refuses a
-- keyword outright leaves `error` set and `score` NULL, so the keyword is not
-- asked again. `keyword` holds values as `enrichment.value` holds them,
-- normalized and stemmed.
--
-- `keyword_role_decisions` holds a person's decision per (keyword, role) —
-- never per source — and no row while nobody has ruled. Nothing stores whether
-- a keyword *has* a role: that threshold is the reader's (ADR-0012), and a
-- decision outranks any score. `role` is a closed set in both tables.

ALTER TABLE enrichment ADD COLUMN rank INTEGER;

CREATE TABLE keyword_roles (
    keyword   TEXT NOT NULL,
    role      TEXT NOT NULL
              CHECK (role IN ('tone', 'era', 'region', 'theme', 'character_trait')),
    source    TEXT NOT NULL,
    score     REAL CHECK (score BETWEEN 0 AND 1),
    model     TEXT,
    error     TEXT,
    stated_at TEXT NOT NULL,
    PRIMARY KEY (keyword, role, source),
    CHECK (error IS NULL OR score IS NULL),
    CHECK (score IS NULL OR model IS NOT NULL)
);
CREATE INDEX idx_keyword_roles_role ON keyword_roles(role, keyword);

CREATE TABLE keyword_role_decisions (
    keyword    TEXT NOT NULL,
    role       TEXT NOT NULL
               CHECK (role IN ('tone', 'era', 'region', 'theme', 'character_trait')),
    decision   TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (keyword, role)
);
