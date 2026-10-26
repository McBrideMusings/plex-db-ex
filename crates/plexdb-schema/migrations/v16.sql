-- Version 16 — which title states a source's role, per title.
--
-- `keyword_role_statements` holds one row per (title, keyword, role, source) a
-- source stated, so a role row no title states any more can be pruned. It sits
-- outside `enrichment` because a reader rolls every enrichment row into a
-- title's taste. No stored title has a row yet, so this deletes the Wikidata
-- cursors: the next sweep fetches every title again and writes them, rather
-- than the first prune deleting every Wikidata role. Its keywords stay until
-- each title's re-fetch replaces them.

CREATE TABLE keyword_role_statements (
    item_id   TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    keyword   TEXT NOT NULL,
    role      TEXT NOT NULL
              CHECK (role IN ('tone', 'era', 'region', 'theme', 'character_trait')),
    source    TEXT NOT NULL,
    stated_at TEXT NOT NULL,
    PRIMARY KEY (item_id, keyword, role, source)
);
CREATE INDEX idx_keyword_role_statements_keyword
    ON keyword_role_statements(keyword, role, source);

DELETE FROM enrichment_cursor WHERE namespace = 'keywords' AND source = 'wikidata';
