-- Version 27 — extras scraper results (ADR-0030).
--
-- `extra_scrape` is the last run of one scraper on one extra: when it ran, whether it
-- produced output, how many attempts it took, why it failed, and which fetch decision
-- (by its `decided_at`) it answered, NULL for an automatic run. The values a scraper
-- proposes live in `enrichment` under the scraper's name as `source`. Additive;
-- `reader_shape` stays.

CREATE TABLE extra_scrape (
    item_id  TEXT NOT NULL REFERENCES extra_item(item_id) ON DELETE CASCADE,
    scraper  TEXT NOT NULL,
    url      TEXT NOT NULL,
    ran_at   TEXT NOT NULL,
    ok       INTEGER NOT NULL CHECK (ok IN (0, 1)),
    attempts INTEGER NOT NULL,
    error    TEXT,
    answered TEXT,
    PRIMARY KEY (item_id, scraper)
);
CREATE INDEX idx_extra_scrape_ran ON extra_scrape(scraper, ran_at);
