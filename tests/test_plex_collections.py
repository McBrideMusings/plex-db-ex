"""Harvesting Plex's hand-built collections into collection membership.

Drives `refresh_plex_collections` against an in-memory `CollectionSource` fake
— never a live Plex server.

This module replaced one that wrote pairwise edges (issue #48). The test that
matters most here is `test_a_large_collection_writes_one_row_per_member`: it
pins the linear shape directly, because the old behaviour passed every other
test in the file and only failed at scale, on a real library, months later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.plex_client import Section
from plexdb.plex_collections import PLEX_SOURCE, refresh_plex_collections
from plexdb.store import init as init_store
from plexdb.store import open_store

DARK_KNIGHT = "imdb:tt0468569"
BATMAN_BEGINS = "imdb:tt0372784"
RISES = "imdb:tt1345836"
BREAKING_BAD = "imdb:tt0903747"

#: The section nearly every test here walks; a test about section types
#: builds its own list instead.
MOVIES_SECTION = Section(key="1", type="movie", title="Movies")


@dataclass
class FakeCollectionSource:
    """A `CollectionSource` over in-memory records — no HTTP."""

    section_list: list[Section]
    collections_by_section: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    children_by_collection: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Every `collection_children` call this fake has served, in order — lets
    #: a test prove there is no per-collection cache short-circuiting a
    #: second sweep.
    children_calls: list[str] = field(default_factory=list)

    def sections(self) -> list[Section]:
        return list(self.section_list)

    def collections(self, section_key: str) -> list[dict[str, Any]]:
        return list(self.collections_by_section.get(section_key, []))

    def collection_children(self, collection_key: str) -> list[dict[str, Any]]:
        self.children_calls.append(collection_key)
        return list(self.children_by_collection.get(collection_key, []))


def _open(store: Path) -> Any:
    init_store(store)
    return open_store(store)


def _seed_item(
    conn: sqlite3.Connection, *, item_id: str, item_type: str, title: str, rating_key: str
) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)", (item_id, item_type, title)
    )
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
        "VALUES (?, ?, '1', '2024-01-01T00:00:00+00:00')",
        (rating_key, item_id),
    )
    conn.commit()


def _seed_batman_pair(conn: sqlite3.Connection) -> None:
    """The two movies most tests here share: The Dark Knight at Plex rating
    key "100", Batman Begins at "101"."""
    _seed_item(
        conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", rating_key="100"
    )
    _seed_item(
        conn, item_id=BATMAN_BEGINS, item_type="movie", title="Batman Begins", rating_key="101"
    )


def _one_collection(
    members: list[str], *, key: str = "500", title: str = "The Dark Knight Collection"
) -> FakeCollectionSource:
    return FakeCollectionSource(
        section_list=[MOVIES_SECTION],
        collections_by_section={"1": [{"ratingKey": key, "title": title, "smart": None}]},
        children_by_collection={key: [{"ratingKey": m} for m in members]},
    )


def _memberships(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT collection_id, item_id, rank, mentions FROM collection_membership "
            "ORDER BY collection_id, item_id"
        )
    ]


def test_a_collection_becomes_one_row_and_one_membership_per_member(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)

        stats = refresh_plex_collections(conn, _one_collection(["100", "101"]))

        collection = conn.execute(
            "SELECT collection_id, source, name, url, size, likes FROM collection"
        ).fetchone()
        assert tuple(collection) == (
            "plex:500",
            PLEX_SOURCE,
            "The Dark Knight Collection",
            None,
            2,
            None,
        )
        assert _memberships(conn) == [
            ("plex:500", BATMAN_BEGINS, None, None),
            ("plex:500", DARK_KNIGHT, None, None),
        ]
        assert stats.memberships_written == 2
        assert stats.collections_written == 1


def test_a_large_collection_writes_one_row_per_member(tmp_path: Path) -> None:
    """The regression for issue #48.

    Co-membership used to be stored as every ordered pair, so 200 members
    became 39,800 rows. Against the real library that was 19,365 memberships
    expanding to 17,809,980 edges — a 920x blow-up that took the published
    snapshot to 3.7 GB. Every other test in the old file passed throughout.
    """
    members = [str(1000 + n) for n in range(200)]
    with _open(tmp_path / "plexdb.db") as conn:
        for n, key in enumerate(members):
            _seed_item(
                conn, item_id=f"imdb:tt{n:07d}", item_type="movie", title=f"F{n}", rating_key=key
            )

        stats = refresh_plex_collections(conn, _one_collection(members))

        count = conn.execute("SELECT count(*) FROM collection_membership").fetchone()[0]

    assert count == 200, "one row per membership — not 200*199 = 39,800 pairs"
    assert stats.memberships_written == 200


def test_size_records_the_whole_collection_even_when_members_are_not_in_the_library(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)

        # Three members, one of which this library has never walked.
        stats = refresh_plex_collections(conn, _one_collection(["100", "101", "999"]))

        size = conn.execute("SELECT size FROM collection").fetchone()[0]

    assert stats.members_skipped_not_in_library == 1
    assert stats.memberships_written == 2
    assert size == 3, "a consumer must be able to see it holds two thirds of the collection"


def test_a_title_removed_from_a_collection_loses_its_membership_on_the_next_sweep(
    tmp_path: Path,
) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        source = _one_collection(["100", "101"])
        refresh_plex_collections(conn, source)

        source.children_by_collection["500"] = [{"ratingKey": "100"}]
        refresh_plex_collections(conn, source)

        assert _memberships(conn) == [("plex:500", DARK_KNIGHT, None, None)]


def test_a_re_pull_adds_a_member_the_collection_gained(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        _seed_item(
            conn, item_id=RISES, item_type="movie", title="The Dark Knight Rises", rating_key="102"
        )
        source = _one_collection(["100", "101"])
        refresh_plex_collections(conn, source)

        source.children_by_collection["500"] = [
            {"ratingKey": "100"},
            {"ratingKey": "101"},
            {"ratingKey": "102"},
        ]
        refresh_plex_collections(conn, source)

        assert len(_memberships(conn)) == 3


def test_it_carries_no_staleness_threshold_and_recomputes_every_run(tmp_path: Path) -> None:
    """Unlike MDBList, the input is local — there is no rate-limited API to
    cache against, so every run re-reads Plex rather than skipping a fresh
    collection."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        source = _one_collection(["100", "101"])

        refresh_plex_collections(conn, source)
        refresh_plex_collections(conn, source)

        assert source.children_calls == ["500", "500"]


def test_a_smart_collection_is_skipped_as_not_actually_curated(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={
                "1": [{"ratingKey": "500", "title": "Recently Added", "smart": "1"}]
            },
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )

        stats = refresh_plex_collections(conn, source)

        assert stats.collections_skipped_smart == 1
        assert stats.collections_written == 0
        assert _memberships(conn) == []
        assert source.children_calls == [], "a skipped collection is never even fetched"


def test_a_collection_listing_the_same_member_twice_writes_one_row(tmp_path: Path) -> None:
    """`(collection_id, item_id)` is the table's primary key, so an unguarded
    duplicate would abort the whole harvest rather than one row."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)

        refresh_plex_collections(conn, _one_collection(["100", "101", "100"]))

        assert len(_memberships(conn)) == 2


def test_a_collection_of_one_still_records_that_membership(tmp_path: Path) -> None:
    """Behaviour change from the edge shape, and the right one. A collection
    of one produced no *pairs*, so it vanished; it is still a fact that the
    title is in it."""
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)

        refresh_plex_collections(conn, _one_collection(["100"]))

        assert _memberships(conn) == [("plex:500", DARK_KNIGHT, None, None)]


def test_a_title_in_two_collections_lands_in_both(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={
                "1": [
                    {"ratingKey": "500", "title": "Batman", "smart": None},
                    {"ratingKey": "501", "title": "Nolan", "smart": None},
                ]
            },
            children_by_collection={
                "500": [{"ratingKey": "100"}, {"ratingKey": "101"}],
                "501": [{"ratingKey": "100"}, {"ratingKey": "101"}],
            },
        )

        refresh_plex_collections(conn, source)

        assert _memberships(conn) == [
            ("plex:500", BATMAN_BEGINS, None, None),
            ("plex:500", DARK_KNIGHT, None, None),
            ("plex:501", BATMAN_BEGINS, None, None),
            ("plex:501", DARK_KNIGHT, None, None),
        ]


def test_a_show_section_is_walked_and_other_section_types_are_skipped(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_item(
            conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", rating_key="200"
        )
        source = FakeCollectionSource(
            section_list=[
                Section(key="2", type="show", title="TV"),
                Section(key="3", type="artist", title="Music"),
            ],
            collections_by_section={
                "2": [{"ratingKey": "600", "title": "Prestige TV", "smart": None}],
                "3": [{"ratingKey": "700", "title": "Albums", "smart": None}],
            },
            children_by_collection={"600": [{"ratingKey": "200"}], "700": [{"ratingKey": "999"}]},
        )

        refresh_plex_collections(conn, source)

        assert _memberships(conn) == [("plex:600", BREAKING_BAD, None, None)]
        assert source.children_calls == ["600"], "a music section is never fetched"


def test_a_collection_with_no_rating_key_is_skipped_not_a_crash(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"title": "Nameless", "smart": None}]},
        )

        stats = refresh_plex_collections(conn, source)

        assert stats.collections_seen == 1
        assert _memberships(conn) == []


def test_harvesting_plex_leaves_another_source_untouched(tmp_path: Path) -> None:
    with _open(tmp_path / "plexdb.db") as conn:
        _seed_batman_pair(conn)
        conn.execute(
            "INSERT INTO collection (collection_id, source, name, observed_at) "
            "VALUES ('mdblist:14', 'mdblist', 'Top Watched', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO collection_membership (collection_id, item_id, rank, observed_at) "
            "VALUES ('mdblist:14', ?, 1, '2026-01-01T00:00:00+00:00')",
            (DARK_KNIGHT,),
        )
        conn.commit()

        refresh_plex_collections(conn, _one_collection(["100"]))
        refresh_plex_collections(conn, _one_collection(["100"]))

        assert _memberships(conn) == [
            ("mdblist:14", DARK_KNIGHT, 1, None),
            ("plex:500", DARK_KNIGHT, None, None),
        ]
