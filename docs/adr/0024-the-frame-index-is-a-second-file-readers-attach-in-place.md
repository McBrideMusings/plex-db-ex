# ADR-0024: The frame index is a second file that readers attach in place

`frame_vec`, `cuts`, `frame_index_state` and `frame_queue` live in `frames.db`, a second SQLite file written by the same `plexdb` package and kept in the directory `PLEXDB_FRAMES_DIR` names. It runs in rollback-journal mode and commits once per title. Readers open it read-only where it lies and `ATTACH` it to the Snapshot; it is never snapshotted or backed up, and a lost file is rebuilt by the frame harvester. No write touches `frames.db` and `plexdb.db` in one transaction.
