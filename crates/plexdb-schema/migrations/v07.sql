-- Version 7 — bookkeeping leaves the facts table (issue #41, ADR-0013).
-- `enrichment` now holds facts about titles and nothing else; every writer's
-- per-title fetch cursor moves to `enrichment_cursor`.
--
-- Both TMDB writers recorded progress as rows in `enrichment` itself —
-- `tmdb_keywords/_fetched`, `tmdb_edges/_fetched_recommendations`,
-- `tmdb_edges/_fetched_similar`, all with the literal value `'1'`. A leading
-- `_` on the key meant "not a fact, skip me", and `plexdb-reader`'s taste
-- rollup was the one place that knew it.
--
-- Measured on the author's store before this migration: a pooled keyword
-- profile over 25,837 plays put the string `1` at the top at 287.2, against
-- 33.0 for the real leader. Removing it also moved that leader to 40.5 — an
-- 18% shift, because the sentinel inflated every title's attribute count and
-- so dragged every genuine keyword down at the same time. The divisor error
-- reaches 41% on a title carrying one real keyword, which is why it does not
-- cancel across the library.
--
-- **A separate namespace would not have fixed it.** The rollup scans every
-- namespace; only the key prefix hid these rows. Renaming them into a
-- `*_cursor` namespace and dropping the prefix would have turned one phantom
-- attribute per title into three the moment the edge sweeps ran. A separate
-- table is what actually removes the condition: a query against `enrichment`
-- cannot see bookkeeping, so no reader needs a convention — including a
-- consumer reading the published snapshot with plain SQLite, which ADR-0007
-- makes an expected thing to do.
--
-- There is no `value` column, because a cursor never had a value. `'1'` was
-- filler; `fetched_at` was always the payload.
--
-- A local `INSERT ... SELECT` and `DELETE`. No re-fetch, no TMDB request, no
-- rate limit — `fetched_at` rides across, so a sweep after this migration
-- still skips everything already fetched. The `_` prefix is stripped on the
-- way (`substr(key, 2)`): it meant "skip me", and in a table nothing else
-- reads there is nothing left for it to do.

CREATE TABLE enrichment_cursor (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, namespace, key)
);

INSERT INTO enrichment_cursor (item_id, namespace, key, fetched_at)
SELECT item_id, namespace, substr(key, 2), fetched_at
FROM enrichment
WHERE key LIKE '\_%' ESCAPE '\';

DELETE FROM enrichment WHERE key LIKE '\_%' ESCAPE '\';
