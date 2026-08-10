"""Walking a Plex library into `items`, `external_ids`, and the rating-key map.

Drives `walk_all` against `PlexSource` fakes built from the recorded
fixtures in `fixtures/plex/` (`tests/plex_fixtures.py`) — never a live
server. The fixtures were captured from a real Plex server: a movie with a
full GUID set, a movie with none at all, and a two-season show with
episodes in both seasons, covering issue #3's acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plex_fixtures import FakeSource, recorded_source

from plexdb.identity import canonical_path, derive_item_id
from plexdb.plex_client import PLEX_TYPE_MOVIE, Section
from plexdb.store import init as init_store
from plexdb.store import open_store
from plexdb.walk import WalkStats, walk_all

AIR_MATER_PATH = "/media/movies/Air Mater (2011) {imdb-tt2100093}/Air Mater (2011).mkv"
AIR_MATER_ID = derive_item_id([], canonical_path(AIR_MATER_PATH, ()))


def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    return list(conn.execute(sql, params).fetchall())


def _open(store: Path) -> Any:
    init_store(store)
    return open_store(store)


def test_a_recorded_response_produces_the_expected_items_and_external_ids_rows(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        stats = walk_all(conn, recorded_source())

        burbs = conn.execute("SELECT * FROM items WHERE item_id = 'imdb:tt0096734'").fetchone()
        assert burbs["type"] == "movie"
        assert burbs["title"] == "The 'Burbs"
        assert burbs["title_sort"] == "'Burbs"
        assert burbs["year"] == 1989
        assert burbs["duration_ms"] == 6100386
        assert burbs["content_rating"] == "PG"
        assert burbs["studio"] == "Universal Pictures"

        burbs_guids = {
            (r["ns"], r["value"])
            for r in _rows(
                conn,
                "SELECT ns, value FROM external_ids WHERE item_id = ?",
                ("imdb:tt0096734",),
            )
        }
        assert burbs_guids == {
            ("imdb", "tt0096734"),
            ("tmdb", "11974"),
            ("tvdb", "5869"),
        }

        show = conn.execute("SELECT * FROM items WHERE item_id = 'imdb:tt18335752'").fetchone()
        assert show["type"] == "show"
        assert show["title"] == "1923"
        assert show["studio"] == "Bosque Ranch Productions"

        episode = conn.execute("SELECT * FROM items WHERE item_id = 'imdb:tt18469978'").fetchone()
        assert episode["type"] == "episode"
        assert episode["show_title"] == "1923"
        assert episode["show_item_id"] == "imdb:tt18335752"
        assert episode["season"] == 1
        assert episode["episode"] == 1
        # Recorded live: Plex reports contentRating on an episode's own
        # record (inherited from its show) but never studio there, even
        # though the show-level record does carry one — the walk must keep
        # the one Plex actually sends and not fabricate the other.
        assert episode["studio"] is None
        assert episode["content_rating"] == "TV-MA"

        second_season_episode = conn.execute(
            "SELECT * FROM items WHERE item_id = 'imdb:tt26608270'"
        ).fetchone()
        assert second_season_episode["season"] == 2
        assert second_season_episode["episode"] == 1
        assert second_season_episode["show_item_id"] == "imdb:tt18335752"
        # This one episode really does carry its own titleSort on the live
        # server ("Killing Season", trimming "The") — proves the walk reads
        # titleSort uniformly rather than assuming only movies/shows have one.
        assert second_season_episode["title_sort"] == "Killing Season"

        plex_rows = {
            r["rating_key"]: (r["item_id"], r["section_id"])
            for r in _rows(conn, "SELECT rating_key, item_id, section_id FROM plex_items")
        }
        assert plex_rows["13714"] == ("imdb:tt0096734", "1")
        assert plex_rows["70936"] == (AIR_MATER_ID, "1")
        assert plex_rows["81044"] == ("imdb:tt18335752", "2")
        assert plex_rows["81290"] == ("imdb:tt18469978", "2")

    assert stats == WalkStats(
        sections_walked=4,
        titles_seen=6,
        titles_written=6,
        fallback_to_path=1,
        identity_kept_on_guid_change=0,
    )


def test_a_title_with_no_recognised_guid_lands_with_a_path_derived_id(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        walk_all(conn, recorded_source())

        assert AIR_MATER_ID.startswith("fs:")
        row = conn.execute("SELECT * FROM items WHERE item_id = ?", (AIR_MATER_ID,)).fetchone()
        assert row is not None
        assert row["title"] == "Air Mater"
        assert row["type"] == "movie"
        # No GUID means no external_ids rows for it — nothing to be found by.
        guid_rows = _rows(conn, "SELECT * FROM external_ids WHERE item_id = ?", (AIR_MATER_ID,))
        assert guid_rows == []


def test_show_shaped_and_movie_shaped_sections_are_both_covered(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        walk_all(conn, recorded_source())

        types = {r["type"] for r in _rows(conn, "SELECT DISTINCT type FROM items")}
        assert types == {"movie", "show", "episode"}


_Snapshot = tuple[set[str], list[tuple[Any, ...]], list[tuple[Any, ...]], tuple[int, int, int]]


def _snapshot(conn: Any) -> _Snapshot:
    """Everything a second walk must leave unchanged, in one comparable shot."""
    item_ids = {r["item_id"] for r in _rows(conn, "SELECT item_id FROM items")}
    external = [
        tuple(r)
        for r in _rows(conn, "SELECT item_id, ns, value FROM external_ids ORDER BY 1, 2, 3")
    ]
    plex_items = [
        tuple(r)
        for r in _rows(
            conn, "SELECT rating_key, item_id, section_id FROM plex_items ORDER BY rating_key"
        )
    ]
    counts = (
        conn.execute("SELECT count(*) FROM items").fetchone()[0],
        conn.execute("SELECT count(*) FROM external_ids").fetchone()[0],
        conn.execute("SELECT count(*) FROM plex_items").fetchone()[0],
    )
    return item_ids, external, plex_items, counts


def test_a_second_walk_over_the_same_recorded_response_adds_no_rows_and_changes_no_ids(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        walk_all(conn, recorded_source())
        before = _snapshot(conn)

        second_stats = walk_all(conn, recorded_source())
        after = _snapshot(conn)

    assert after == before
    assert second_stats.identity_kept_on_guid_change == 0


def test_every_rating_key_seen_maps_to_exactly_one_item_id(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        walk_all(conn, recorded_source())
        walk_all(conn, recorded_source())

        rows = _rows(
            conn,
            "SELECT rating_key, count(DISTINCT item_id) AS n FROM plex_items GROUP BY rating_key",
        )
    assert all(r["n"] == 1 for r in rows)
    assert len(rows) == 6


def test_a_titles_guid_set_changing_between_walks_keeps_its_identity_rather_than_forking(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    movies_section = Section(key="1", type="movie", title="Movies")
    first_record = {
        "ratingKey": "999",
        "type": "movie",
        "title": "Test Movie",
        "year": 2000,
        "Guid": [{"id": "imdb://tt1111111"}],
        "Media": [{"Part": [{"file": "/media/movies/Test Movie (2000)/test.mkv"}]}],
    }
    first_pass = FakeSource(
        section_list=[movies_section],
        records={("1", PLEX_TYPE_MOVIE): [first_record]},
    )

    with _open(store) as conn:
        first_stats = walk_all(conn, first_pass)
        assert first_stats.identity_kept_on_guid_change == 0
        kept_id = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '999'"
        ).fetchone()[0]
        assert kept_id == "imdb:tt1111111"

        # Plex re-matches the same physical title (same rating key) to a
        # different IMDb id on the next walk.
        rematched_record = dict(first_record)
        rematched_record["Guid"] = [{"id": "imdb://tt2222222"}]
        second_pass = FakeSource(
            section_list=[movies_section],
            records={("1", PLEX_TYPE_MOVIE): [rematched_record]},
        )
        second_stats = walk_all(conn, second_pass)

        # The identity is kept, not forked: still one items row for rating
        # key 999, still addressed by the original id.
        assert second_stats.identity_kept_on_guid_change == 1
        item_count = conn.execute(
            "SELECT count(*) FROM items WHERE item_id IN ('imdb:tt1111111', 'imdb:tt2222222')"
        ).fetchone()[0]
        assert item_count == 1
        still_mapped = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '999'"
        ).fetchone()[0]
        assert still_mapped == "imdb:tt1111111"

        # Both the original and the newly-seen GUID are recorded and reachable
        # under the kept id — the walk never drops a GUID it has already seen.
        guids = {
            (r["ns"], r["value"])
            for r in _rows(
                conn, "SELECT ns, value FROM external_ids WHERE item_id = 'imdb:tt1111111'"
            )
        }
        assert guids == {("imdb", "tt1111111"), ("imdb", "tt2222222")}


def test_walk_can_be_scoped_to_one_section(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        stats = walk_all(conn, recorded_source(), section_key="1")

        assert stats.sections_walked == 1
        assert stats.titles_seen == 2
        types = {r["type"] for r in _rows(conn, "SELECT DISTINCT type FROM items")}
    assert types == {"movie"}


def test_an_unrecognised_section_type_is_skipped_not_errored(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    source = FakeSource(
        section_list=[Section(key="9", type="photo", title="Photos")],
        records={},
    )
    with _open(store) as conn:
        stats = walk_all(conn, source)

    assert stats.sections_walked == 0
    assert stats.titles_seen == 0
