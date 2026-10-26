"""`plexdb check` counts the guarded tables a rewrite must not lose rows from.
The copies a migration or `repair-fs-identities` takes, and their rollback, are
tested in Rust (`crates/plexdb/tests/migrate.rs`, `crates/plexdb/tests/repair_fs.rs`)."""

from __future__ import annotations

from pathlib import Path

from fixture_store import init

from plexdb import backup, store


def test_a_table_the_migration_creates_is_not_reported_as_row_loss(tmp_path: Path) -> None:
    """`guarded_counts` leaves out a table that does not exist yet. Counting it
    as zero would make the step that creates it look like it destroyed
    something."""
    path = tmp_path / "plexdb.db"
    init(path)
    with store.open_store(path) as conn:
        conn.execute("DROP TABLE plays")
        conn.commit()
        counts = backup.guarded_counts(conn)

    assert "plays" not in counts
    assert "items" in counts
