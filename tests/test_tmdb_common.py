"""`plexdb.tmdb_common` — the media-type, staleness, and failure-threshold
rules shared by `enrich_tmdb.py` and `tmdb_edges.py`.

Issue #22: those four pieces used to be copied verbatim into each sweep, so
the two modules could drift — a title shape that changed one copy's answer
would leave the other copy's answer unchanged, with nothing to say so.

For the two functions, an `is` identity check against the same-named
attribute on each sweep module is the direct proof that drift can no longer
happen: `from .tmdb_common import media_type_for` binds the sweep module's
name to the *exact* function object in `tmdb_common`, so any title shape that
changes what this one function returns changes it for both sweeps at once. A
sweep that kept its own `def media_type_for(...)` instead would bind a
distinct function object, and these identity assertions would fail. The two
constants are ints, which Python binds by value rather than by reference, so
equality is the strongest check available for them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from plexdb import enrich_tmdb, tmdb_common, tmdb_edges


def test_both_sweeps_import_the_same_media_type_for_function() -> None:
    assert enrich_tmdb.media_type_for is tmdb_common.media_type_for
    assert tmdb_edges.media_type_for is tmdb_common.media_type_for


def test_both_sweeps_import_the_same_is_stale_function() -> None:
    assert enrich_tmdb.is_stale is tmdb_common.is_stale
    assert tmdb_edges.is_stale is tmdb_common.is_stale


def test_both_sweeps_read_the_same_max_consecutive_failures() -> None:
    assert enrich_tmdb.MAX_CONSECUTIVE_FAILURES == tmdb_common.MAX_CONSECUTIVE_FAILURES
    assert tmdb_edges.MAX_CONSECUTIVE_FAILURES == tmdb_common.MAX_CONSECUTIVE_FAILURES


def test_both_sweeps_read_the_same_default_stale_days() -> None:
    assert enrich_tmdb.DEFAULT_STALE_DAYS == tmdb_common.DEFAULT_STALE_DAYS
    assert tmdb_edges.DEFAULT_STALE_DAYS == tmdb_common.DEFAULT_STALE_DAYS


def test_media_type_for_maps_movie_and_show_and_skips_everything_else() -> None:
    assert tmdb_common.media_type_for("movie") == "movie"
    assert tmdb_common.media_type_for("show") == "tv"
    assert tmdb_common.media_type_for("episode") is None
    assert tmdb_common.media_type_for("collection") is None


def test_is_stale_compares_fetched_at_against_the_cutoff() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = (cutoff + timedelta(days=1)).isoformat(timespec="seconds")
    stale = (cutoff - timedelta(days=1)).isoformat(timespec="seconds")
    assert tmdb_common.is_stale(stale, cutoff) is True
    assert tmdb_common.is_stale(fresh, cutoff) is False
