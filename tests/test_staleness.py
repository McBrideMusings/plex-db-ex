"""`plexdb.staleness` — the one "is this row due a re-fetch" rule the Python Gated Sources
share."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from plexdb import lists, staleness

CRATE = Path(__file__).parent.parent / "crates" / "plexdb" / "src"


def test_is_stale_compares_the_stored_timestamp_against_the_cutoff() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = (cutoff + timedelta(days=1)).isoformat(timespec="seconds")
    stale = (cutoff - timedelta(days=1)).isoformat(timespec="seconds")
    assert staleness.is_stale(stale, cutoff) is True
    assert staleness.is_stale(fresh, cutoff) is False


def test_the_rust_writer_keeps_the_same_staleness_default_and_failure_limit() -> None:
    """The Rust writer holds its own copy of the staleness window
    (`crates/plexdb/src/staleness.rs`), and `lists.py` its own copy of the failure limit the
    list harvest stops at (`crates/plexdb/src/collections.rs`)."""
    window = (CRATE / "staleness.rs").read_text(encoding="utf-8")
    limit = (CRATE / "collections.rs").read_text(encoding="utf-8")
    assert f"DEFAULT_STALE_DAYS: i64 = {staleness.DEFAULT_STALE_DAYS};" in window
    assert f"MAX_FAILURES: i64 = {lists.MAX_FAILURES};" in limit
