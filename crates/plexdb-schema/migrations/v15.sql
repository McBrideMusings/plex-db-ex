-- Version 15 — the store names the read shape it carries (`READER_SHAPE`).
--
-- A reader gates on `reader_shape`, not `schema_version`, so a migration that
-- only adds tables or columns no reader depends on leaves every deployed reader
-- working. Version 10 is the last migration that changed what a reader reads.

CREATE TABLE reader_shape (version INTEGER NOT NULL);
INSERT INTO reader_shape (version) VALUES (10);
