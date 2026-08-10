"""Enriching `plays` from Tautulli's `get_history` (issue #9): IP, completion
percentage, paused time, and the seconds actually watched.

Drives `match_tautulli_history` against a minimal store built directly with
SQL — items, plex_items, and plays rows inserted by hand rather than through
a full Plex walk + ingest, since these tests are about the Tautulli match
itself, not about Plex ingestion (`test_plays.py`'s job). Every source here
is an in-memory `_FakeTautulliSource` built from plain dicts; nothing reaches
the network.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from plexdb.plays import MATCH_WINDOW_SECONDS, TautulliMatchStats, match_tautulli_history
from plexdb.store import init as init_store
from plexdb.store import open_store


@dataclass
class _FakeTautulliSource:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def history(self) -> list[dict[str, Any]]:
        return list(self.rows)


def _open(store: Path) -> Any:
    init_store(store)
    return open_store(store)


def _seed_item(conn: sqlite3.Connection, item_id: str, rating_key: str) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', 'A Title')", (item_id,)
    )
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
        "VALUES (?, ?, '1', '2024-01-01T00:00:00+00:00')",
        (rating_key, item_id),
    )


def _seed_play(
    conn: sqlite3.Connection,
    *,
    history_key: str,
    item_id: str,
    plex_account_id: int,
    client_identifier: str | None,
    viewed_at: int,
) -> None:
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, viewed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (history_key, item_id, plex_account_id, client_identifier, viewed_at),
    )


def _play(conn: sqlite3.Connection, history_key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM plays WHERE history_key = ?", (history_key,)).fetchone()
    assert row is not None
    return row


def _tautulli_row(
    *,
    row_id: int | None,
    rating_key: int,
    user_id: int,
    machine_id: str,
    stopped: int,
    duration: int = 3000,
    paused_counter: int = 0,
    percent_complete: int = 100,
    ip_address: str = "203.0.113.1",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "rating_key": rating_key,
        "user_id": user_id,
        "machine_id": machine_id,
        "stopped": stopped,
        "duration": duration,
        "paused_counter": paused_counter,
        "percent_complete": percent_complete,
        "ip_address": ip_address,
    }


def test_a_matched_row_writes_ip_completion_paused_time_and_seconds_watched(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=9001,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000450,
                    duration=5400,
                    paused_counter=1200,
                    percent_complete=90,
                    ip_address="203.0.113.10",
                )
            ]
        )

        stats = match_tautulli_history(conn, source)

        assert stats == TautulliMatchStats(
            rows_seen=1, rows_matched=1, plays_without_tautulli_data=0
        )
        play = _play(conn, "/status/sessions/history/1")
        assert play["ip"] == "203.0.113.10"
        assert play["percent_complete"] == 90
        assert play["paused_counter"] == 1200
        assert play["seconds_watched"] == 5400
        assert play["tautulli_id"] == 9001


def test_seconds_watched_is_durations_verbatim_not_duration_minus_paused_counter(
    tmp_path: Path,
) -> None:
    """The bug issue #9 exists to prevent: `duration` is already net of
    paused time, so computing `duration - paused_counter` subtracts pause a
    second time and halves the footage on exactly the paused-heavy rows this
    column is meant to measure. 2361/1167 is one of the issue's own measured
    live rows."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=9001,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000450,
                    duration=2361,
                    paused_counter=1167,
                )
            ]
        )

        match_tautulli_history(conn, source)

        play = _play(conn, "/status/sessions/history/1")
        assert play["seconds_watched"] == 2361, "must be duration verbatim, not 2361 - 1167"


def test_a_row_outside_the_match_window_is_left_unmatched(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=9001,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000400 + MATCH_WINDOW_SECONDS + 1,
                )
            ]
        )

        stats = match_tautulli_history(conn, source)

        assert stats.rows_matched == 0
        assert stats.rows_unmatched == 1
        assert stats.plays_without_tautulli_data == 1
        play = _play(conn, "/status/sessions/history/1")
        assert play["tautulli_id"] is None
        assert play["ip"] is None


def test_a_row_exactly_at_the_window_boundary_still_matches(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=9001,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000400 + MATCH_WINDOW_SECONDS,
                )
            ]
        )

        stats = match_tautulli_history(conn, source)

        assert stats.rows_matched == 1


def test_an_in_progress_row_with_a_null_id_is_skipped_not_unmatched(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=None,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000450,
                )
            ]
        )

        stats = match_tautulli_history(conn, source)

        assert stats == TautulliMatchStats(
            rows_seen=1, rows_in_progress=1, plays_without_tautulli_data=1
        )
        play = _play(conn, "/status/sessions/history/1")
        assert play["tautulli_id"] is None


def test_a_second_run_over_the_same_row_changes_nothing(tmp_path: Path) -> None:
    """Acceptance: running the adapter twice must be a no-op the second
    time, enforced by the row's stored tautulli_id."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        row = _tautulli_row(
            row_id=9001,
            rating_key=9001,
            user_id=1,
            machine_id="device-alpha-001",
            stopped=1700000450,
        )

        first = match_tautulli_history(conn, _FakeTautulliSource(rows=[row]))
        second = match_tautulli_history(conn, _FakeTautulliSource(rows=[row]))

        assert first.rows_matched == 1
        assert second.rows_matched == 0
        assert second.rows_already_matched == 1
        assert second.plays_without_tautulli_data == 0


def test_a_play_already_carrying_a_different_tautulli_id_is_not_re_matched(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.execute(
            "UPDATE plays SET tautulli_id = 555 WHERE history_key = ?",
            ("/status/sessions/history/1",),
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=9001,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000450,
                    ip_address="203.0.113.99",
                )
            ]
        )

        stats = match_tautulli_history(conn, source)

        assert stats.rows_matched == 0
        assert stats.rows_unmatched == 1
        play = _play(conn, "/status/sessions/history/1")
        assert play["tautulli_id"] == 555
        assert play["ip"] is None, "row 9001's data must not land on a play row 555 already claimed"


def test_the_nearest_viewed_at_wins_among_two_rewatches_sharing_hard_keys(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/early",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000000,
        )
        _seed_play(
            conn,
            history_key="/status/sessions/history/late",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700005000,
        )
        conn.commit()

        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=9001,
                    rating_key=9001,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700005050,
                )
            ]
        )

        match_tautulli_history(conn, source)

        early = _play(conn, "/status/sessions/history/early")
        late = _play(conn, "/status/sessions/history/late")
        assert early["tautulli_id"] is None
        assert late["tautulli_id"] == 9001


def test_a_row_whose_rating_key_resolves_to_no_item_is_unmatched_not_an_error(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        source = _FakeTautulliSource(
            rows=[
                _tautulli_row(
                    row_id=1,
                    rating_key=99999,
                    user_id=1,
                    machine_id="device-alpha-001",
                    stopped=1700000000,
                )
            ]
        )

        stats = match_tautulli_history(conn, source)

        assert stats.rows_unmatched == 1


def test_a_play_with_no_candidate_row_is_counted_in_the_visible_summary(tmp_path: Path) -> None:
    """Acceptance: a silently-empty match is the failure this must make
    visible — an empty Tautulli response still reports how many plays carry
    no Tautulli data."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        conn.commit()

        stats = match_tautulli_history(conn, _FakeTautulliSource(rows=[]))

        assert stats.rows_seen == 0
        assert stats.plays_without_tautulli_data == 1


def test_a_null_client_identifier_matches_a_null_machine_id(tmp_path: Path) -> None:
    """A play whose device never resolved (client_identifier is NULL, the
    same "unresolved device" state `ingest_plays` leaves behind) must still
    be reachable if Tautulli also reports no machine_id for that session."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier=None,
            viewed_at=1700000400,
        )
        conn.commit()

        row = _tautulli_row(
            row_id=9001,
            rating_key=9001,
            user_id=1,
            machine_id="",
            stopped=1700000450,
        )
        stats = match_tautulli_history(conn, _FakeTautulliSource(rows=[row]))

        assert stats.rows_matched == 1


def test_the_stored_tautulli_id_can_never_land_on_two_plays_at_the_schema_level(
    tmp_path: Path,
) -> None:
    """Defense in depth behind the application-level claim check: the
    partial unique index on `plays.tautulli_id` (schema v4) refuses a second
    play carrying the same non-null value even if application logic were
    ever bypassed."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(conn, "imdb:tt1", "9001")
        _seed_play(
            conn,
            history_key="/status/sessions/history/1",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700000400,
        )
        _seed_play(
            conn,
            history_key="/status/sessions/history/2",
            item_id="imdb:tt1",
            plex_account_id=1,
            client_identifier="device-alpha-001",
            viewed_at=1700009000,
        )
        conn.execute(
            "UPDATE plays SET tautulli_id = 42 WHERE history_key = ?",
            ("/status/sessions/history/1",),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE plays SET tautulli_id = 42 WHERE history_key = ?",
                ("/status/sessions/history/2",),
            )
