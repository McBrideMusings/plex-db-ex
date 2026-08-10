"""Ingesting Plex watch history into `plays`.

Drives `ingest_plays` against `HistorySource` fakes built from the recorded
fixtures in `fixtures/plex/` (`tests/plex_fixtures.py`) — never a live
server. The history fixture reuses rating keys the walk fixtures already
define, so a store walked from `recorded_source()` resolves most of the
events, plus two deliberate gaps: an unresolved rating key and an unresolved
device.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from plex_fixtures import FakeHistorySource, recorded_history_source, recorded_source

from plexdb.plays import PlaysStats, ingest_plays
from plexdb.store import init as init_store
from plexdb.store import open_store
from plexdb.walk import walk_all


def _record_history_cutoffs(source: FakeHistorySource) -> list[int | None]:
    """Patch `source.history` to log the `since_viewed_at` it is asked for,
    still returning what the unpatched method would. The returned list fills
    in as `ingest_plays` calls the source."""
    calls: list[int | None] = []
    original_history = source.history

    def _tracking_history(*, since_viewed_at: int | None = None) -> list[dict[str, Any]]:
        calls.append(since_viewed_at)
        return original_history(since_viewed_at=since_viewed_at)

    source.history = _tracking_history  # type: ignore[method-assign]
    return calls


class _IgnoresCutoff:
    """Wraps a `HistorySource` and always reports its full event list,
    ignoring `since_viewed_at` — stands in for a source re-reporting an
    event `ingest_plays` has already recorded."""

    def __init__(self, inner: FakeHistorySource) -> None:
        self._inner = inner

    def history(self, *, since_viewed_at: int | None = None) -> list[dict[str, Any]]:
        return self._inner.history(since_viewed_at=None)

    def devices(self) -> list[dict[str, Any]]:
        return self._inner.devices()


@contextmanager
def _walked_store(store: Path) -> Iterator[Any]:
    """A store already walked from the recorded library fixtures, so
    `plex_items` carries the rating-key -> item_id map an ingest resolves
    against."""
    init_store(store)
    with open_store(store) as conn:
        walk_all(conn, recorded_source())
        yield conn


def test_a_recorded_history_response_produces_one_play_per_resolvable_event(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        stats = ingest_plays(conn, recorded_history_source())

        # Four events recorded; one has a rating key (999999) missing from
        # plex_items, so three plays land.
        assert stats == PlaysStats(
            events_seen=4,
            plays_written=3,
            already_recorded=0,
            unresolved_rating_key=1,
            unresolved_device=1,
        )

        rows = {r["history_key"]: r for r in conn.execute("SELECT * FROM plays")}
        assert set(rows) == {
            "/status/sessions/history/2001",
            "/status/sessions/history/2000",
            "/status/sessions/history/1998",
        }

        burbs_play = rows["/status/sessions/history/2001"]
        assert burbs_play["item_id"] == "imdb:tt0096734"
        assert burbs_play["plex_account_id"] == 1
        assert burbs_play["client_identifier"] == "device-alpha-001"
        assert burbs_play["platform"] == "Roku"
        assert burbs_play["viewed_at"] == 1700000400
        # Plex-only ingest: nothing here comes from Tautulli.
        assert burbs_play["ip"] is None
        assert burbs_play["percent_complete"] is None
        assert burbs_play["paused_counter"] is None

        # deviceID 11's clientIdentifier is empty on the live server for at
        # least one real device — treated as absent, not an empty string.
        episode_play = rows["/status/sessions/history/2000"]
        assert episode_play["item_id"] == "imdb:tt18469978"
        assert episode_play["client_identifier"] is None
        assert episode_play["platform"] == "Chromecast"

        # deviceID 999 is not in the device list at all.
        air_mater_play = rows["/status/sessions/history/1998"]
        assert air_mater_play["client_identifier"] is None
        assert air_mater_play["platform"] is None


def test_an_unresolvable_rating_key_is_counted_and_reported_not_dropped_silently(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        stats = ingest_plays(conn, recorded_history_source())

        assert stats.unresolved_rating_key == 1
        # The unresolved event's rating key never appears in plays.
        row = conn.execute(
            "SELECT * FROM plays WHERE history_key = '/status/sessions/history/1999'"
        ).fetchone()
        assert row is None


def test_a_second_ingest_over_the_same_events_adds_no_duplicate_rows(tmp_path: Path) -> None:
    """Feeds the same full event list twice, bypassing the incremental
    narrowing (`_IgnoresCutoff` never honours `since_viewed_at`) — proving
    the DB-level `ON CONFLICT(history_key)` dedup itself, not just that a
    narrowed second fetch happens to return nothing new. A real source that
    re-reports an already-seen event — clock skew, a boundary event sitting
    exactly at the cutoff — must be just as safe to re-ingest."""
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        first = ingest_plays(conn, _IgnoresCutoff(recorded_history_source()))
        count_after_first = conn.execute("SELECT count(*) FROM plays").fetchone()[0]

        second = ingest_plays(conn, _IgnoresCutoff(recorded_history_source()))
        count_after_second = conn.execute("SELECT count(*) FROM plays").fetchone()[0]

        assert first.plays_written == 3
        assert count_after_first == 3
        assert count_after_second == count_after_first
        # Every event was reported again; all three resolvable ones were
        # already recorded, none written a second time.
        assert second.events_seen == 4
        assert second.plays_written == 0
        assert second.already_recorded == 3


def test_ingest_pins_the_cursor_at_the_oldest_unresolved_event_not_the_newest_written_one(
    tmp_path: Path,
) -> None:
    """The fixture's newest event (viewedAt=1700000400) resolves and gets
    written, but an older one (viewedAt=1699999000) does not. The next
    cutoff must be pinned at that older, still-outstanding event — not
    advanced to the newest write — or the outstanding event falls below the
    next fetch's floor and is never asked for again."""
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        ingest_plays(conn, recorded_history_source())

        source = recorded_history_source()
        calls = _record_history_cutoffs(source)
        ingest_plays(conn, source)

        assert calls == [1699999000]


def test_a_walk_that_later_resolves_an_unresolved_event_lets_the_next_ingest_pick_it_up(
    tmp_path: Path,
) -> None:
    """The regression this module exists to prevent: an event skipped as
    unresolved must still be reachable by a later ingest, once a later walk
    adds its rating key to plex_items — even after a newer event has already
    been written and the cursor has moved."""
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        first = ingest_plays(conn, recorded_history_source())
        assert first.unresolved_rating_key == 1
        missing = conn.execute(
            "SELECT * FROM plays WHERE history_key = '/status/sessions/history/1999'"
        ).fetchone()
        assert missing is None

        # A second ingest with nothing changed must still see it as
        # unresolved, not silently drop it from consideration.
        second = ingest_plays(conn, recorded_history_source())
        assert second.unresolved_rating_key == 1

        # Now a later walk resolves it — exactly what `plexdb walk` would do
        # by inserting a plex_items row for rating key 999999.
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES ('999999', 'imdb:tt0096734', '1', '2024-01-01T00:00:00+00:00')"
        )

        third = ingest_plays(conn, recorded_history_source())

        assert third.unresolved_rating_key == 0
        assert third.plays_written == 1
        now_resolved = conn.execute(
            "SELECT item_id FROM plays WHERE history_key = '/status/sessions/history/1999'"
        ).fetchone()
        assert now_resolved["item_id"] == "imdb:tt0096734"

        # Nothing is outstanding anymore, so the cursor is free to advance
        # past the event it was pinned at.
        cursor_after = conn.execute(
            "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1"
        ).fetchone()[0]
        assert cursor_after == 1700000400


def test_an_empty_store_asks_for_the_whole_history(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        source = recorded_history_source()
        calls = _record_history_cutoffs(source)
        ingest_plays(conn, source)

        assert calls == [None]


def test_a_play_referencing_an_item_id_cascades_on_item_delete(tmp_path: Path) -> None:
    """`plays.item_id` really is a foreign key, not just documentation."""
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        ingest_plays(conn, recorded_history_source())
        before = conn.execute("SELECT count(*) FROM plays").fetchone()[0]
        assert before == 3

        conn.execute("DELETE FROM items WHERE item_id = 'imdb:tt0096734'")

        after = conn.execute("SELECT count(*) FROM plays").fetchone()[0]
        assert after == before - 1


def test_a_history_source_with_no_events_writes_nothing(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _walked_store(store) as conn:
        empty = FakeHistorySource(history_events=[], device_list=[])
        stats = ingest_plays(conn, empty)

        assert stats == PlaysStats()
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 0
