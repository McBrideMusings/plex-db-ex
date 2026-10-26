-- Version 5 — an external id is scoped by media type (issue #23). TMDB and
-- TVDB number movies and TV shows in two separate lists that both start at 1,
-- and Plex reports both as a bare `tmdb://1678` with no type attached. Movie
-- 1678 is *Godzilla* (1954); show 1678 is *The Golden Girls* (1985). Under the
-- old `PRIMARY KEY (ns, value)` those two unrelated records claimed the same
-- row, and `walk._resolve_existing` — which looks a title up by external id
-- first (ADR-0008, as amended by #19) — read that as "seen before" and handed
-- the show the movie's identity. 1,414 identities in the author's store had
-- fused that way, 1,326 of them holding two different IMDb ids.
--
-- `kind` is the walk's own `movie` / `show` / `episode`. All three are needed,
-- not just movie-vs-show: TVDB numbers episodes in a list separate from series.
-- IMDb is unaffected (one shared list for everything) and so is Plex's own
-- `plex://` id (already type-qualified), but the key covers every namespace
-- rather than special-casing the two that collide — a rule with an exception
-- list is a rule waiting for the next source to be added to it.
--
-- Existing rows take their `kind` from `items.type`. That is wrong for the
-- fused identities by construction — they hold one type for two titles — which
-- is why `plexdb repair-identities` re-derives them from Plex rather than the
-- migration trying to guess which rating key was which title.
--
-- `plays.rating_key` is the second half. `plays.ingest` resolves a rating key
-- to an `item_id` and then discards it, so a play records a conclusion with no
-- record of the evidence, and no identity correction can be applied to rows
-- already written — which is why repairing #23 costs a re-ingest instead of an
-- UPDATE. Nothing reads this column yet: `repair-identities` still deletes and
-- re-ingests. It buys the *next* correction, not this one, and it is added now
-- because the schema version is already moving.
--
-- Backfilled for every identity mapping to exactly one rating key, and left
-- NULL wherever an identity maps to several — the fused ones, and equally the
-- legitimate merges from #19 where one title sits in two library sections.
-- NULL is the honest answer in both cases: no evidence was ever recorded.

CREATE TABLE external_ids_v5 (
    item_id TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    ns      TEXT NOT NULL,
    value   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    PRIMARY KEY (ns, value, kind)
);
INSERT INTO external_ids_v5 (item_id, ns, value, kind)
SELECT e.item_id, e.ns, e.value, i.type
FROM external_ids e
JOIN items i ON i.item_id = e.item_id;
DROP TABLE external_ids;
ALTER TABLE external_ids_v5 RENAME TO external_ids;
CREATE INDEX idx_external_ids_item ON external_ids(item_id);

ALTER TABLE plays ADD COLUMN rating_key TEXT;
UPDATE plays SET rating_key = (
    SELECT p.rating_key FROM plex_items p
    WHERE p.item_id = plays.item_id
      AND (SELECT COUNT(*) FROM plex_items p2 WHERE p2.item_id = plays.item_id) = 1
);
CREATE INDEX idx_plays_rating_key ON plays(rating_key);
