"""Deriving `local_collection` edges from Plex collection co-membership.

Drives `refresh_local_edges` against an in-memory `CollectionSource` fake —
never a live server. Mirrors `test_tmdb_edges.py`'s shape where the same
rule applies (a member outside the library is dropped, not invented), and
adds the rules specific to a local, always-recomputed source: no staleness
cursor, a constant rank, and skipping `smart` collections as
not-actually-curated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexdb.local_edges import (
    DEFAULT_REPORT_THRESHOLD,
    LOCAL_COLLECTION_EDGE_TYPE,
    LargeCollection,
    refresh_local_edges,
)
from plexdb.plex_client import Section
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
    #: Every `collection_children` call this fake has served, in order —
    #: lets a test prove there is no per-title cache short-circuiting a
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


def _seed_rises(conn: sqlite3.Connection) -> None:
    """The third of the trilogy, at rating key "102"."""
    _seed_item(
        conn, item_id=RISES, item_type="movie", title="The Dark Knight Rises", rating_key="102"
    )


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    return list(conn.execute(sql, params).fetchall())


def test_titles_sharing_a_collection_produce_edges_in_both_directions(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={
                "1": [{"ratingKey": "500", "title": "The Dark Knight Collection", "smart": None}]
            },
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )

        stats = refresh_local_edges(conn, source)

        rows = {
            tuple(row) for row in _rows(conn, "SELECT from_id, to_id, edge_type, rank FROM edges")
        }

    assert rows == {
        (DARK_KNIGHT, BATMAN_BEGINS, LOCAL_COLLECTION_EDGE_TYPE, 1),
        (BATMAN_BEGINS, DARK_KNIGHT, LOCAL_COLLECTION_EDGE_TYPE, 1),
    }
    assert stats.edges_written == 2
    assert stats.collections_processed == 1
    assert stats.collections_seen == 1


def test_a_title_removed_from_a_collection_loses_its_edges_on_the_next_sweep(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        _seed_rises(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            children_by_collection={
                "500": [{"ratingKey": "100"}, {"ratingKey": "101"}, {"ratingKey": "102"}]
            },
        )
        refresh_local_edges(conn, source)

        # Rises leaves the collection before the next sweep.
        source.children_by_collection["500"] = [{"ratingKey": "100"}, {"ratingKey": "101"}]
        refresh_local_edges(conn, source)

        to_ids = {
            row["to_id"]
            for row in _rows(conn, "SELECT to_id FROM edges WHERE from_id = ?", (DARK_KNIGHT,))
        }

    assert to_ids == {BATMAN_BEGINS}


def test_a_re_pull_adds_an_edge_the_collection_gained(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        _seed_rises(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )
        refresh_local_edges(conn, source)

        source.children_by_collection["500"] = [
            {"ratingKey": "100"},
            {"ratingKey": "101"},
            {"ratingKey": "102"},
        ]
        refresh_local_edges(conn, source)

        to_ids = {
            row["to_id"]
            for row in _rows(conn, "SELECT to_id FROM edges WHERE from_id = ?", (DARK_KNIGHT,))
        }

    assert to_ids == {BATMAN_BEGINS, RISES}


def test_local_edges_carry_no_staleness_threshold_and_recompute_every_run(
    tmp_path: Path,
) -> None:
    """Unlike `tmdb_edges.py`, there is no fetch cursor to check — a second
    sweep re-asks Plex for the exact same collection rather than treating
    anything as cached, proven by the fake's own call log."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )

        refresh_local_edges(conn, source)
        refresh_local_edges(conn, source)

    assert source.children_calls == ["500", "500"]


def test_a_smart_collection_is_skipped_as_not_actually_curated(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={
                "1": [{"ratingKey": "500", "title": "Recently Released Movies", "smart": "1"}]
            },
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )

        stats = refresh_local_edges(conn, source)

        rows = _rows(conn, "SELECT * FROM edges")

    assert rows == []
    assert stats.collections_seen == 1
    assert stats.collections_skipped_smart == 1
    assert stats.collections_processed == 0
    assert source.children_calls == []  # a smart collection's members are never even fetched


def test_a_collection_member_outside_the_library_is_dropped_not_invented(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", rating_key="100"
        )
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            # "999999" has no plex_items row — never walked.
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "999999"}]},
        )

        stats = refresh_local_edges(conn, source)

        rows = _rows(conn, "SELECT * FROM edges")

    assert rows == []
    assert stats.members_skipped_not_in_library == 1


def test_both_directions_are_queryable_without_string_parsing(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )
        refresh_local_edges(conn, source)

        forward = _rows(conn, "SELECT to_id FROM edges WHERE from_id = ?", (DARK_KNIGHT,))
        reverse = _rows(conn, "SELECT from_id FROM edges WHERE to_id = ?", (BATMAN_BEGINS,))

    assert [r["to_id"] for r in forward] == [BATMAN_BEGINS]
    assert [r["from_id"] for r in reverse] == [DARK_KNIGHT]


def test_a_collection_listing_the_same_member_twice_does_not_crash_or_self_loop(
    tmp_path: Path,
) -> None:
    """Schema-legal input Plex should never send, guarded the same way
    `tmdb_edges.py` guards a target seen twice in one recommendation list."""
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            children_by_collection={
                "500": [{"ratingKey": "100"}, {"ratingKey": "100"}, {"ratingKey": "101"}]
            },
        )

        stats = refresh_local_edges(conn, source)  # must not raise

        rows = _rows(conn, "SELECT from_id, to_id FROM edges")

    assert len(rows) == 2
    assert stats.edges_written == 2


def test_a_collection_of_one_produces_no_edges(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(
            conn, item_id=DARK_KNIGHT, item_type="movie", title="The Dark Knight", rating_key="100"
        )
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Solo", "smart": None}]},
            children_by_collection={"500": [{"ratingKey": "100"}]},
        )

        stats = refresh_local_edges(conn, source)

        rows = _rows(conn, "SELECT * FROM edges")

    assert rows == []
    assert stats.edges_written == 0
    assert stats.collections_processed == 1


def test_a_pair_sharing_two_collections_lands_as_one_edge_not_two(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={
                "1": [
                    {"ratingKey": "500", "title": "Batman", "smart": None},
                    {"ratingKey": "501", "title": "Best Movies", "smart": None},
                ]
            },
            children_by_collection={
                "500": [{"ratingKey": "100"}, {"ratingKey": "101"}],
                "501": [{"ratingKey": "100"}, {"ratingKey": "101"}],
            },
        )

        stats = refresh_local_edges(conn, source)

        rows = _rows(conn, "SELECT from_id, to_id FROM edges")

    assert len(rows) == 2
    assert stats.edges_written == 2


def test_a_collection_at_or_above_the_threshold_is_reported_with_its_size(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        for i in range(5):
            _seed_item(
                conn,
                item_id=f"imdb:tt{i:07d}",
                item_type="movie",
                title=f"Movie {i}",
                rating_key=str(100 + i),
            )
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={
                "1": [{"ratingKey": "500", "title": "Big Shelf", "smart": None}]
            },
            children_by_collection={"500": [{"ratingKey": str(100 + i)} for i in range(5)]},
        )

        stats = refresh_local_edges(conn, source, report_threshold=5)

    assert stats.large_collections == [LargeCollection("Big Shelf", 5)]


def test_a_collection_under_the_threshold_is_not_reported(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_batman_pair(conn)
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"ratingKey": "500", "title": "Batman", "smart": None}]},
            children_by_collection={"500": [{"ratingKey": "100"}, {"ratingKey": "101"}]},
        )

        stats = refresh_local_edges(conn, source)  # DEFAULT_REPORT_THRESHOLD

    assert stats.large_collections == []
    assert DEFAULT_REPORT_THRESHOLD > 2


def test_a_show_section_is_walked_and_a_non_movie_non_show_section_is_skipped(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        _seed_item(
            conn, item_id=BREAKING_BAD, item_type="show", title="Breaking Bad", rating_key="200"
        )
        source = FakeCollectionSource(
            section_list=[
                Section(key="2", type="show", title="TV Shows"),
                Section(key="9", type="photo", title="Photos"),
            ],
            collections_by_section={
                "2": [{"ratingKey": "600", "title": "Prestige TV", "smart": None}],
                # A photo section reaching `collections()` at all would be a
                # bug, so nothing is wired up for key "9" — a call there
                # returns nothing rather than the one collection counted below.
            },
            children_by_collection={"600": [{"ratingKey": "200"}]},
        )

        stats = refresh_local_edges(conn, source)

    assert stats.collections_seen == 1
    assert stats.collections_processed == 1


def test_a_collection_with_no_rating_key_is_skipped_not_a_crash(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    with _open(store) as conn:
        source = FakeCollectionSource(
            section_list=[MOVIES_SECTION],
            collections_by_section={"1": [{"title": "No Key", "smart": None}]},
        )

        stats = refresh_local_edges(conn, source)  # must not raise

        rows = _rows(conn, "SELECT * FROM edges")

    assert rows == []
    assert stats.collections_processed == 1
    assert stats.edges_written == 0
