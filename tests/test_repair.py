"""Splitting identities that fused two unrelated titles (issue #23).

Builds the fused state directly — the shape a store carried when
`external_ids` was keyed on `(ns, value)` alone — then drives `repair`
against a `PlexSource` fake holding the two real titles.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from plex_fixtures import FakeSource

from plexdb.plex_client import PLEX_TYPE_EPISODE, PLEX_TYPE_MOVIE, PLEX_TYPE_SHOW, Section
from plexdb.repair import fused_item_ids, kinds_by_rating_key, repair
from plexdb.store import init as init_store
from plexdb.store import open_store
from plexdb.walk import walk_all

FUSED_ID = "imdb:tt0047034"

GODZILLA = {
    "ratingKey": "5550",
    "type": "movie",
    "title": "Godzilla",
    "year": 1954,
    "Guid": [{"id": "imdb://tt0047034"}, {"id": "tmdb://1678"}, {"id": "tvdb://5015"}],
    "Media": [{"Part": [{"file": "/media/movies/Godzilla (1954)/g.mkv"}]}],
}
GOLDEN_GIRLS = {
    "ratingKey": "141718",
    "type": "show",
    "title": "The Golden Girls",
    "year": 1985,
    "key": "/library/metadata/141718",
    "Guid": [{"id": "imdb://tt0088526"}, {"id": "tmdb://1678"}, {"id": "tvdb://71292"}],
}


def _source() -> FakeSource:
    return FakeSource(
        section_list=[
            Section(key="1", type="movie", title="Movies"),
            Section(key="2", type="show", title="TV Shows"),
        ],
        records={
            ("1", PLEX_TYPE_MOVIE): [GODZILLA],
            ("2", PLEX_TYPE_SHOW): [GOLDEN_GIRLS],
            ("2", PLEX_TYPE_EPISODE): [],
        },
    )


def _fused_store(path: Path, *, with_play: bool = True) -> None:
    """One identity carrying both titles' ids and both titles' rating keys."""
    init_store(path)
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', 'Godzilla', 1954)",
            (FUSED_ID,),
        )
        for ns, value in (
            ("imdb", "tt0047034"),
            ("imdb", "tt0088526"),
            ("tmdb", "1678"),
            ("tvdb", "5015"),
            ("tvdb", "71292"),
        ):
            conn.execute(
                "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
                "VALUES (?, ?, ?, 'movie', '2024-01-01T00:00:00+00:00')",
                (FUSED_ID, ns, value),
            )
        for rating_key, section in (("5550", "1"), ("141718", "2")):
            conn.execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
                "VALUES (?, ?, ?, '2024-01-01T00:00:00+00:00')",
                (rating_key, FUSED_ID, section),
            )
        if with_play:
            conn.execute(
                "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
                "VALUES ('h1', ?, 1, 1000)",
                (FUSED_ID,),
            )
            conn.execute("INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, 9000)")
        conn.commit()


def _rows(conn: sqlite3.Connection, sql: str) -> list[Any]:
    return list(conn.execute(sql).fetchall())


def _seed_plex_items(
    conn: sqlite3.Connection, item_id: str, keys: tuple[tuple[str, str], ...]
) -> None:
    for rating_key, section in keys:
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES (?, ?, ?, '2024-01-01T00:00:00+00:00')",
            (rating_key, item_id, section),
        )


def test_an_identity_covering_a_movie_and_a_show_is_reported_as_fused(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    _fused_store(store)
    with open_store(store) as conn:
        assert fused_item_ids(conn, kinds_by_rating_key(_source())) == [FUSED_ID]


def test_a_duplicate_of_one_title_across_two_sections_is_not_reported_as_fused(
    tmp_path: Path,
) -> None:
    """The same movie in two library sections is two rating keys of the same
    kind on one identity. That merge is what issue #19 asked for and must
    survive a repair pass untouched."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('m', 'movie', 'Dupe')")
        _seed_plex_items(conn, "m", (("5550", "1"), ("5551", "3")))
        conn.commit()

    source = FakeSource(
        section_list=[
            Section(key="1", type="movie", title="Movies"),
            Section(key="3", type="movie", title="More Movies"),
        ],
        records={
            ("1", PLEX_TYPE_MOVIE): [GODZILLA],
            ("3", PLEX_TYPE_MOVIE): [dict(GODZILLA, ratingKey="5551")],
        },
    )
    with open_store(store) as conn:
        assert fused_item_ids(conn, kinds_by_rating_key(source)) == []


def test_two_records_of_one_kind_that_disagree_on_an_id_are_not_reported_as_fused(
    tmp_path: Path,
) -> None:
    """Measured on the author's library: South Park S28E1 exists twice, the
    two copies share a TVDB id so version 5 merges them, and Plex matched
    them to different IMDb entries. Two ids in one namespace, one media kind
    — not a fusion. Flagging it would delete and re-walk it into byte-
    identical rows on every run, dropping its plays each time."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    episode_a = {
        "ratingKey": "139312",
        "type": "episode",
        "title": "Twisted Christian",
        "grandparentTitle": "South Park",
        "parentIndex": 28,
        "index": 1,
        "Guid": [{"id": "imdb://tt38043385"}, {"id": "tvdb://11256175"}],
        "Media": [{"Part": [{"file": "/media/tv/South Park/s28e01-a.mkv"}]}],
    }
    episode_b = dict(
        episode_a,
        ratingKey="139321",
        Guid=[{"id": "imdb://tt26764428"}, {"id": "tvdb://11256175"}],
        Media=[{"Part": [{"file": "/media/tv/South Park/s28e01-b.mkv"}]}],
    )
    source = FakeSource(
        section_list=[Section(key="2", type="show", title="TV Shows")],
        records={("2", PLEX_TYPE_SHOW): [], ("2", PLEX_TYPE_EPISODE): [episode_a, episode_b]},
    )

    with open_store(store) as conn:
        walk_all(conn, source)
        # The two records did merge — that is version 5 working as intended.
        assert conn.execute("SELECT COUNT(DISTINCT item_id) FROM plex_items").fetchone()[0] == 1
        assert fused_item_ids(conn, kinds_by_rating_key(source)) == []


def test_one_plex_record_carrying_several_ids_in_a_namespace_is_not_fused(
    tmp_path: Path,
) -> None:
    """Plex reports every match it has, so one record can legitimately hold
    several ids in a namespace — *The Animatrix* carries nine TMDB ids on a
    single rating key. One record is one kind, so it is never a fusion."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    animatrix = {
        "ratingKey": "700",
        "type": "movie",
        "title": "The Animatrix",
        "Guid": [{"id": "tmdb://24357"}, {"id": "tmdb://24358"}, {"id": "tmdb://24362"}],
        "Media": [{"Part": [{"file": "/media/movies/The Animatrix (2003)/a.mkv"}]}],
    }
    source = FakeSource(
        section_list=[Section(key="1", type="movie", title="Movies")],
        records={("1", PLEX_TYPE_MOVIE): [animatrix]},
    )
    with open_store(store) as conn:
        walk_all(conn, source)
        assert fused_item_ids(conn, kinds_by_rating_key(source)) == []


def test_repair_splits_a_fused_identity_into_the_two_titles_plex_reports(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    _fused_store(store)

    with open_store(store) as conn:
        stats = repair(conn, _source())

        assert stats.fused_found == 1
        movie_id = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '5550'"
        ).fetchone()[0]
        show_id = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = '141718'"
        ).fetchone()[0]
        assert movie_id == "imdb:tt0047034"
        assert show_id == "imdb:tt0088526"

        titles = {r["item_id"]: r["title"] for r in _rows(conn, "SELECT item_id, title FROM items")}
        assert titles == {"imdb:tt0047034": "Godzilla", "imdb:tt0088526": "The Golden Girls"}

        # No identity spans two media kinds any more.
        assert fused_item_ids(conn, kinds_by_rating_key(_source())) == []


def test_repair_drops_the_plays_it_cannot_repoint_and_rewinds_the_cursor(
    tmp_path: Path,
) -> None:
    """A pre-v5 play carries no rating key, so nothing records which of the
    two titles it was actually for. It is deleted with the identity and the
    cursor is rewound so the next ingest reads it back from history."""
    store = tmp_path / "plexdb.db"
    _fused_store(store)

    with open_store(store) as conn:
        stats = repair(conn, _source())

        assert stats.plays_dropped == 1
        assert stats.plays_repointed == 0
        assert stats.cursor_rewound_to == 1000
        assert conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 0
        cursor = conn.execute(
            "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1"
        ).fetchone()[0]
        assert cursor == 1000


def test_a_play_carrying_its_rating_key_is_repointed_not_dropped(
    tmp_path: Path,
) -> None:
    """The whole point of `plays.rating_key`. Two plays on the fused identity,
    one for each of the two real titles; both name the Plex item they came
    from, so both survive the split and land on the right half — no history
    re-read, and no cursor rewind, because nothing had to be dropped."""
    store = tmp_path / "plexdb.db"
    _fused_store(store, with_play=False)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at, "
            "seconds_watched) VALUES ('movie', ?, '5550', 1, 1000, 5400)",
            (FUSED_ID,),
        )
        conn.execute(
            "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at) "
            "VALUES ('show', ?, '141718', 1, 2000)",
            (FUSED_ID,),
        )
        conn.execute("INSERT INTO plays_ingest_cursor (id, since_viewed_at) VALUES (1, 9000)")
        conn.commit()

        stats = repair(conn, _source())

        # One moved, not two: the fused identity *was* the movie's id, so the
        # movie's play was already where it belongs and only the show's play
        # had to travel. A play that needs no move is not a repoint.
        assert stats.plays_repointed == 1
        assert stats.plays_dropped == 0
        assert stats.plays_orphaned == 0
        assert stats.cursor_rewound_to is None, (
            "nothing was dropped, so there is nothing to re-read — rewinding would re-fetch "
            "history the store already holds"
        )
        cursor = conn.execute(
            "SELECT since_viewed_at FROM plays_ingest_cursor WHERE id = 1"
        ).fetchone()[0]
        assert cursor == 9000

        landed = {
            r["history_key"]: r["item_id"]
            for r in _rows(conn, "SELECT history_key, item_id FROM plays")
        }
        assert landed == {"movie": "imdb:tt0047034", "show": "imdb:tt0088526"}, (
            "each play must follow its own rating key onto the correct half of the split"
        )

        # Columns other than item_id survive the round trip — a repair must not
        # quietly discard the Tautulli data a separate pass worked to attach.
        watched = conn.execute(
            "SELECT seconds_watched FROM plays WHERE history_key = 'movie'"
        ).fetchone()[0]
        assert watched == 5400


def test_a_play_whose_rating_key_left_the_library_is_kept_not_guessed_at(
    tmp_path: Path,
) -> None:
    """A title can leave Plex before the repair runs. Its play cannot be
    placed against any identity — inventing one would credit someone's viewing
    to the wrong title — so it stays exactly where it is and is counted. The
    identity holding it survives for the same reason: deleting it would take
    real viewing history along with it."""
    store = tmp_path / "plexdb.db"
    _fused_store(store, with_play=False)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at) "
            "VALUES ('gone', ?, '999999', 1, 1000)",
            (FUSED_ID,),
        )
        conn.commit()

        stats = repair(conn, _source())

        assert stats.plays_orphaned == 1
        assert stats.plays_repointed == 0
        assert stats.plays_dropped == 0
        surviving = conn.execute("SELECT item_id FROM plays WHERE history_key = 'gone'").fetchone()
        assert surviving is not None, "an unplaceable play must not be deleted"
        assert surviving["item_id"] == FUSED_ID
        assert (
            conn.execute("SELECT COUNT(*) FROM items WHERE item_id = ?", (FUSED_ID,)).fetchone()[0]
            == 1
        ), "the identity holding an unplaceable play is kept, or the play goes with it"


def test_repair_recovers_when_an_earlier_pass_died_before_moving_the_plays(
    tmp_path: Path,
) -> None:
    """The crash window this design exists to close. An interrupted pass can
    leave a play sitting on an identity that no longer appears in `plex_items`
    at all. Nothing looks fused any more, so the fused scan finds nothing — the
    next run must still put the play right, from the store alone."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        walk_all(conn, _source())
        # The state an interrupted run leaves: the play still names the rating
        # key it came from, but sits on the wrong identity.
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('stale', 'movie', 'Stale')")
        conn.execute(
            "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at) "
            "VALUES ('orphan', 'stale', '141718', 1, 1000)"
        )
        conn.commit()

        stats = repair(conn, _source())

        assert stats.fused_found == 0, "nothing is fused — the earlier run got that far"
        assert stats.plays_repointed == 1
        landed = conn.execute("SELECT item_id FROM plays WHERE history_key = 'orphan'").fetchone()[
            0
        ]
        assert landed == "imdb:tt0088526", (
            "the play follows its own rating key home without any record of what the "
            "interrupted run was trying to do"
        )


def test_repair_on_a_clean_store_changes_nothing(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        stats = repair(conn, _source())

    assert stats.fused_found == 0
    assert stats.plays_dropped == 0
    assert stats.cursor_rewound_to is None
    assert stats.walk is None


def test_repair_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    _fused_store(store)

    with open_store(store) as conn:
        repair(conn, _source())
        second = repair(conn, _source())

    assert second.fused_found == 0
