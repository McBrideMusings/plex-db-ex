-- Version 31 — every extra gets its length from its header probe (plex-db-ex-at2).
--
-- Adds no table or column, and leaves the reader shape at 30. `check-files` now writes the
-- duration its `header` probe reads into `extra_file.duration_ms`. A file probed before
-- this version has a `header` verdict and no duration, and `check-files` probes only files
-- with no verdict, so those verdicts are dropped here: the next run probes each such file
-- once and records both.

DELETE FROM extra_check
WHERE kind = 'header'
  AND path IN (SELECT path FROM extra_file WHERE duration_ms IS NULL);
