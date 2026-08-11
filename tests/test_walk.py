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
from plexdb.plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PLEX_TYPE_SHOW, Section
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


def test_a_titles_rating_key_changing_between_walks_keeps_its_identity_by_external_id(
    tmp_path: Path,
) -> None:
    """ADR-0008's amendment, second churn case: a remove-and-re-add changes
    the rating key but not the GUIDs. The external id lookup (tried before
    the rating key) finds the identity the old rating key was already
    recorded against, so both rating keys end up mapped to the one item_id
    — nothing forks."""
    store = tmp_path / "plexdb.db"
    movies_section = Section(key="1", type="movie", title="Movies")
    first_record = {
        "ratingKey": "500",
        "type": "movie",
        "title": "Test Movie",
        "year": 2000,
        "Guid": [{"id": "imdb://tt3333333"}],
        "Media": [{"Part": [{"file": "/media/movies/Test Movie (2000)/test.mkv"}]}],
    }
    first_pass = FakeSource(
        section_list=[movies_section],
        records={("1", PLEX_TYPE_MOVIE): [first_record]},
    )

    with _open(store) as conn:
        walk_all(conn, first_pass)

        # Plex removed and re-added the title: same GUIDs, a new rating key.
        # The old rating key (500) is absent from this pass's records —
        # exactly what "remove-and-re-add" means at the Plex level.
        readded_record = dict(first_record)
        readded_record["ratingKey"] = "501"
        second_pass = FakeSource(
            section_list=[movies_section],
            records={("1", PLEX_TYPE_MOVIE): [readded_record]},
        )
        second_stats = walk_all(conn, second_pass)

        first_mapped = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '500'"
        ).fetchone()[0]
        second_mapped = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '501'"
        ).fetchone()[0]
        assert first_mapped == second_mapped == "imdb:tt3333333"
        item_count = conn.execute(
            "SELECT count(*) FROM items WHERE item_id = 'imdb:tt3333333'"
        ).fetchone()[0]
        assert item_count == 1

    # The GUIDs did not actually change, so a fresh derivation from the
    # re-added record already lands on the same id the external-id lookup
    # found — nothing was overridden, so neither "kept" counter fires.
    assert second_stats.identity_kept_by_external_id == 0
    assert second_stats.identity_kept_on_guid_change == 0


def test_an_external_id_still_matching_a_prior_identity_wins_over_a_fresh_higher_priority_guid(
    tmp_path: Path,
) -> None:
    """A title gains a higher-priority GUID (tmdb) it did not carry before,
    while keeping a lower-priority one (tvdb) already recorded against its
    existing identity. A fresh derivation from this pass's GUIDs alone would
    prefer the new tmdb id; the external-id lookup instead finds the
    already-known tvdb match first (in priority order, skipping the
    unrecorded tmdb id) and keeps the existing identity — counted under
    `identity_kept_by_external_id`, distinct from a rating-key-found keep."""
    store = tmp_path / "plexdb.db"
    movies_section = Section(key="1", type="movie", title="Movies")
    first_record = {
        "ratingKey": "600",
        "type": "movie",
        "title": "Test Movie",
        "year": 2000,
        "Guid": [{"id": "tvdb://4444"}],
        "Media": [{"Part": [{"file": "/media/movies/Test Movie 2 (2000)/test.mkv"}]}],
    }
    first_pass = FakeSource(
        section_list=[movies_section],
        records={("1", PLEX_TYPE_MOVIE): [first_record]},
    )

    with _open(store) as conn:
        first_stats = walk_all(conn, first_pass)
        assert first_stats.identity_kept_by_external_id == 0
        kept_id = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '600'"
        ).fetchone()[0]
        assert kept_id == "tvdb:4444"

        gained_guid_record = dict(first_record)
        gained_guid_record["Guid"] = [{"id": "tmdb://12345"}, {"id": "tvdb://4444"}]
        second_pass = FakeSource(
            section_list=[movies_section],
            records={("1", PLEX_TYPE_MOVIE): [gained_guid_record]},
        )
        second_stats = walk_all(conn, second_pass)

        # A fresh derivation would have picked "tmdb:12345" (tmdb outranks
        # tvdb); the identity is kept at "tvdb:4444" instead, and the keep
        # is attributed to the external id, not the rating key.
        still_mapped = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '600'"
        ).fetchone()[0]
        assert still_mapped == "tvdb:4444"
        item_count = conn.execute(
            "SELECT count(*) FROM items WHERE item_id IN ('tvdb:4444', 'tmdb:12345')"
        ).fetchone()[0]
        assert item_count == 1

    assert second_stats.identity_kept_by_external_id == 1
    assert second_stats.identity_kept_on_guid_change == 0


def test_several_external_ids_matching_different_identities_resolve_by_priority_order(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: when a record's external ids match two
    different already-recorded identities, the pick is deterministic —
    whichever namespace `identity.PRIORITY` ranks higher wins, not
    whichever the database happens to return first."""
    store = tmp_path / "plexdb.db"
    movies_section = Section(key="1", type="movie", title="Movies")
    record_a = {
        "ratingKey": "700",
        "type": "movie",
        "title": "Movie A",
        "year": 2001,
        "Guid": [{"id": "imdb://tt7777777"}],
        "Media": [{"Part": [{"file": "/media/movies/Movie A (2001)/a.mkv"}]}],
    }
    record_b = {
        "ratingKey": "701",
        "type": "movie",
        "title": "Movie B",
        "year": 2002,
        "Guid": [{"id": "tvdb://8888"}],
        "Media": [{"Part": [{"file": "/media/movies/Movie B (2002)/b.mkv"}]}],
    }
    first_pass = FakeSource(
        section_list=[movies_section],
        records={("1", PLEX_TYPE_MOVIE): [record_a, record_b]},
    )

    with _open(store) as conn:
        walk_all(conn, first_pass)

        # A third rating key's record carries both external ids — imdb
        # (Movie A's identity) and tvdb (Movie B's identity). imdb outranks
        # tvdb in identity.PRIORITY, so it must win.
        ambiguous_record = {
            "ratingKey": "702",
            "type": "movie",
            "title": "Movie A",
            "year": 2001,
            "Guid": [{"id": "tvdb://8888"}, {"id": "imdb://tt7777777"}],
            "Media": [{"Part": [{"file": "/media/movies/Movie A (2001)/a2.mkv"}]}],
        }
        second_pass = FakeSource(
            section_list=[movies_section],
            records={("1", PLEX_TYPE_MOVIE): [ambiguous_record]},
        )
        walk_all(conn, second_pass)

        resolved = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '702'"
        ).fetchone()[0]
    assert resolved == "imdb:tt7777777"


def test_two_records_in_the_same_pass_sharing_an_external_id_resolve_to_one_identity(
    tmp_path: Path,
) -> None:
    """The external-id lookup must see writes made earlier in the *same*
    `walk_all` call, not just what was already in the store when the walk
    started. Record A (processed first) claims both imdb and tvdb ids;
    record B (a different rating key, processed second in the same pass)
    carries only the tvdb id A also has. B must resolve to A's identity —
    found by the external id A wrote moments ago — rather than deriving a
    fresh id of its own and forking a second row for the same title."""
    store = tmp_path / "plexdb.db"
    movies_section = Section(key="1", type="movie", title="Movies")
    record_a = {
        "ratingKey": "900",
        "type": "movie",
        "title": "Same Title",
        "year": 2003,
        "Guid": [{"id": "imdb://tt5555555"}, {"id": "tvdb://1111"}],
        "Media": [{"Part": [{"file": "/media/movies/Same Title (2003)/a.mkv"}]}],
    }
    record_b = {
        "ratingKey": "901",
        "type": "movie",
        "title": "Same Title",
        "year": 2003,
        "Guid": [{"id": "tvdb://1111"}],
        "Media": [{"Part": [{"file": "/media/movies/Same Title (2003)/b.mkv"}]}],
    }
    source = FakeSource(
        section_list=[movies_section],
        records={("1", PLEX_TYPE_MOVIE): [record_a, record_b]},
    )

    with _open(store) as conn:
        stats = walk_all(conn, source)

        a_id = conn.execute("SELECT item_id FROM plex_items WHERE rating_key = '900'").fetchone()[0]
        b_id = conn.execute("SELECT item_id FROM plex_items WHERE rating_key = '901'").fetchone()[0]
        assert a_id == "imdb:tt5555555"
        assert b_id == a_id
        item_count = conn.execute(
            "SELECT count(*) FROM items WHERE item_id = 'imdb:tt5555555'"
        ).fetchone()[0]
        assert item_count == 1

    assert stats.identity_kept_by_external_id == 1


def test_a_movie_and_a_show_sharing_a_tmdb_number_stay_two_identities(
    tmp_path: Path,
) -> None:
    """Issue #23. TMDB numbers movies and shows in two separate lists that
    both start at 1, so `tmdb://1678` is *Godzilla* (1954) as a movie and
    *The Golden Girls* (1985) as a show — unrelated records that happen to
    share a number. Before the media kind entered the external-id key, the
    show resolved to the movie's identity and the two fused into one row."""
    store = tmp_path / "plexdb.db"
    movies_section = Section(key="1", type="movie", title="Movies")
    shows_section = Section(key="2", type="show", title="TV Shows")
    godzilla = {
        "ratingKey": "5550",
        "type": "movie",
        "title": "Godzilla",
        "year": 1954,
        "Guid": [{"id": "imdb://tt0047034"}, {"id": "tmdb://1678"}, {"id": "tvdb://5015"}],
        "Media": [{"Part": [{"file": "/media/movies/Godzilla (1954)/g.mkv"}]}],
    }
    golden_girls = {
        "ratingKey": "141718",
        "type": "show",
        "title": "The Golden Girls",
        "year": 1985,
        "key": "/library/metadata/141718",
        "Guid": [{"id": "imdb://tt0088526"}, {"id": "tmdb://1678"}, {"id": "tvdb://71292"}],
    }
    source = FakeSource(
        section_list=[movies_section, shows_section],
        records={
            ("1", PLEX_TYPE_MOVIE): [godzilla],
            ("2", PLEX_TYPE_SHOW): [golden_girls],
            ("2", PLEX_TYPE_EPISODE): [],
        },
    )

    with _open(store) as conn:
        stats = walk_all(conn, source)

        movie_id = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '5550'"
        ).fetchone()[0]
        show_id = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '141718'"
        ).fetchone()[0]
        assert movie_id == "imdb:tt0047034"
        assert show_id == "imdb:tt0088526"
        assert movie_id != show_id

        # The shared number is recorded once per kind, against its own title.
        shared = {
            (r["item_id"], r["kind"])
            for r in _rows(
                conn, "SELECT item_id, kind FROM external_ids WHERE ns = 'tmdb' AND value = '1678'"
            )
        }
        assert shared == {("imdb:tt0047034", "movie"), ("imdb:tt0088526", "show")}

    assert stats.identity_kept_by_external_id == 0


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
