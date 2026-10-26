-- Version 4 — the Tautulli history adapter (issue #9). Two columns on the
-- existing `plays` table, nothing else: `seconds_watched` is Tautulli's
-- `duration` copied verbatim (already net of paused time — computing
-- `duration - paused_counter` would subtract pause a second time), and
-- `tautulli_id` is the Tautulli history row's own `id`, stored so a re-run
-- of `plexdb enrich-tautulli-plays` is idempotent: the partial unique index
-- below means the same Tautulli row can never land on two different plays.
-- No flag, no threshold, no boolean verdict — a reader that wants a floor
-- writes `WHERE seconds_watched >= <n>` itself.

ALTER TABLE plays ADD COLUMN seconds_watched INTEGER;
ALTER TABLE plays ADD COLUMN tautulli_id INTEGER;
CREATE UNIQUE INDEX idx_plays_tautulli_id ON plays(tautulli_id) WHERE tautulli_id IS NOT NULL;
