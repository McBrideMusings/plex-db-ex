-- Version 3 — affinity edges (issue #6). Directed, typed, ranked relationships
-- between two items, first proven with TMDB's `recommendations` (behavioural)
-- and `similar` (content-based) endpoints, kept as distinct `edge_type`
-- values, never merged — they measure different things.
--
-- `from_id` and `to_id` both reference `items`, which is populated only by
-- `plexdb walk` (ADR-0005). A title TMDB recommends that this library has
-- never walked has no `item_id` to point at, so a writer can only record an
-- edge between two titles this store already knows — the schema has no
-- stub-item or candidate-title table for something nobody owns, so that is
-- not a policy choice, it is the only thing representable here.
--
-- `PRIMARY KEY (from_id, to_id, edge_type)` matches the uniqueness rule
-- verbatim; the two secondary indexes below carry the actual query traffic —
-- "edges from X of type Y" and "edges into X of type Y" — in both directions
-- without a table scan.

CREATE TABLE edges (
    from_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    to_id      TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    edge_type  TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, edge_type)
);
CREATE INDEX idx_edges_from_type ON edges(from_id, edge_type);
CREATE INDEX idx_edges_to_type ON edges(to_id, edge_type);
