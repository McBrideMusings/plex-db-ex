-- Version 29 — reader shape 29 (plex-db-ex-9ak.2).
--
-- Adds no table or column. `plexdb-reader` now reads `audio_facts`, `audio_part` and
-- `media_part` (`audio_facts_for`, `audio_facts_for_many`), so this bumps the read shape:
-- a store older than version 29 lacks the audio tables, and the reader refuses it at
-- open instead of failing at the accessor.

UPDATE reader_shape SET version = 29;
