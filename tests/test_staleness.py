"""`plexdb.staleness` — the one "is this row due a re-fetch" rule.

Three sweeps cache against a window now: TMDB keywords, TMDB edges, and
crowd-list harvesting (issue #34). The rule is identical for all three, so it
lives in one module and each of them binds the same function object. The `is`
identity checks below are the direct proof of that: a sweep that grew its own
copy would bind a distinct object and fail here, which is what stops the
copies drifting apart the way issue #22's did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from plexdb import enrich_tmdb, staleness, tmdb_edges


def test_every_sweep_imports_the_same_is_stale_function() -> None:
    assert enrich_tmdb.is_stale is staleness.is_stale
    assert tmdb_edges.is_stale is staleness.is_stale


def test_every_sweep_reads_the_same_default_stale_days() -> None:
    assert enrich_tmdb.DEFAULT_STALE_DAYS == staleness.DEFAULT_STALE_DAYS
    assert tmdb_edges.DEFAULT_STALE_DAYS == staleness.DEFAULT_STALE_DAYS


def test_is_stale_compares_the_stored_timestamp_against_the_cutoff() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = (cutoff + timedelta(days=1)).isoformat(timespec="seconds")
    stale = (cutoff - timedelta(days=1)).isoformat(timespec="seconds")
    assert staleness.is_stale(stale, cutoff) is True
    assert staleness.is_stale(fresh, cutoff) is False


def test_the_rust_crowd_list_harvest_keeps_the_same_defaults() -> None:
    """The harvest in `crates/plexdb/src/collections.rs` holds its own copy of the staleness
    window, and `lists.py` its own copy of the failure limit the harvest stops at."""
    from pathlib import Path

    from plexdb import lists

    source = Path(__file__).parent.parent / "crates" / "plexdb" / "src" / "collections.rs"
    text = source.read_text(encoding="utf-8")
    assert f"DEFAULT_STALE_DAYS: i64 = {staleness.DEFAULT_STALE_DAYS};" in text
    assert f"MAX_FAILURES: i64 = {lists.MAX_FAILURES};" in text
