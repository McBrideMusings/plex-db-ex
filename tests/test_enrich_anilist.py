"""The AniList sweep against the recorded responses in `fixtures/anilist/` —
never the live API."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from anilist_fixtures import RecordedAniListSource

from plexdb.anilist_client import Tag
from plexdb.enrich_anilist import enrich_anilist, index_mapping, role_for, wipe
from plexdb.errors import AniListError
from plexdb.store import init as init_store
from plexdb.store import open_store

FMAB = "imdb:tt1355642"
EYES = "tmdb:62913"
INCEPTION = "imdb:tt1375666"


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as c:
        for item_id, kind, ids in (
            (FMAB, "show", [("imdb", "tt1355642"), ("tmdb", "31911")]),
            (EYES, "show", [("tmdb", "62913")]),
            (INCEPTION, "movie", [("imdb", "tt1375666"), ("tmdb", "27205")]),
        ):
            c.execute(
                "INSERT INTO items (item_id, type, title) VALUES (?, ?, ?)",
                (item_id, kind, item_id),
            )
            for ns, value in ids:
                c.execute(
                    "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
                    "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
                    (item_id, ns, value, kind),
                )
        c.commit()
        yield c


def _rows(conn: sqlite3.Connection, item_id: str) -> dict[str, tuple[str, int | None]]:
    """value → (key, rank) for one title's anilist rows."""
    return {
        row["value"]: (row["key"], row["rank"])
        for row in conn.execute(
            "SELECT key, value, rank FROM enrichment "
            "WHERE item_id = ? AND namespace = 'keywords' AND source = 'anilist'",
            (item_id,),
        )
    }


def _roles(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT keyword, role, score, model, error FROM keyword_roles WHERE source = 'anilist'"
    ):
        assert (row["score"], row["model"], row["error"]) == (None, None, None)
        out.setdefault(row["keyword"], set()).add(row["role"])
    return out


def test_fmab_stores_alchemy_with_its_rank(conn: Any) -> None:
    enrich_anilist(conn, RecordedAniListSource())

    assert _rows(conn, FMAB)["alchemy"] == ("keyword", 97)
    surfaces = dict(conn.execute("SELECT surface, keyword FROM keyword_forms").fetchall())
    assert surfaces["Alchemy"] == "alchemy"


def test_a_spoiler_tag_is_stored_under_spoiler_keyword(conn: Any) -> None:
    enrich_anilist(conn, RecordedAniListSource())

    rows = _rows(conn, FMAB)
    assert rows["conspiracy"] == ("spoiler_keyword", 94)
    keyword_values = {
        r[0]
        for r in conn.execute(
            "SELECT value FROM enrichment WHERE item_id = ? AND key = 'keyword'", (FMAB,)
        )
    }
    assert "conspiracy" not in keyword_values


def test_a_show_spanning_two_entries_merges_their_tags(conn: Any) -> None:
    source = RecordedAniListSource()

    enrich_anilist(conn, source)

    rows = _rows(conn, EYES)
    assert rows["magic"] == ("keyword", 79)  # 75 on the first OVA, 79 on the second
    assert rows["demon"] == ("keyword", 90)
    assert rows["amnesia"] == ("spoiler_keyword", 60)  # only the second carries it
    assert rows["motorcycle"] == ("keyword", 10)  # only the first carries it
    assert sorted(source.calls[0]) == [300, 1225, 5114]


def test_a_tag_one_entry_calls_a_spoiler_is_a_spoiler(conn: Any) -> None:
    source = RecordedAniListSource(
        recorded={
            300: [Tag("Twist", 50, "Theme-Drama", True), Tag("Magic", 40, None, False)],
            1225: [Tag("Twist", 40, "Theme-Drama", False), Tag("Magic", 60, None, False)],
        }
    )

    enrich_anilist(conn, source)

    rows = _rows(conn, EYES)
    assert rows["twist"] == ("spoiler_keyword", 50)
    assert rows["magic"] == ("keyword", 60)


def test_tag_categories_state_roles_through_the_table(conn: Any) -> None:
    stats = enrich_anilist(conn, RecordedAniListSource())

    roles = _roles(conn)
    assert roles["alchemy"] == {"theme"}  # Theme-Fantasy
    assert roles["military"] == {"theme"}  # Theme-Other-Organisations, via its parent
    assert roles["cyborg"] == {"character_trait"}  # Cast-Traits
    assert roles["anachronism"] == {"era"}  # Setting-Time
    assert "shounen" not in roles  # Demographic states nothing
    assert "ensemble cast" not in roles  # Cast-Main Cast states nothing
    assert stats.roles_stated == sum(len(r) for r in roles.values())


def test_role_for_walks_up_the_category() -> None:
    assert role_for("Theme-Other-Organisations") == "theme"
    assert role_for("Setting-Time") == "era"
    assert role_for("Setting-Scene") is None
    assert role_for("Cast-Main Cast") is None
    assert role_for(None) is None


def test_mapping_ids_resolve_from_scalar_list_and_object_fields() -> None:
    index = index_mapping(
        [
            {"anilist_id": 1, "type": "TV", "themoviedb_id": {"tv": 100}, "tvdb_id": 7},
            {"anilist_id": 2, "type": "MOVIE", "themoviedb_id": {"movie": [200, 201]}},
            {"anilist_id": 3, "type": "MOVIE", "themoviedb_id": 300, "imdb_id": "tt0000003"},
            {"anilist_id": 4, "type": "TV", "themoviedb_id": [400], "imdb_id": ["tt0000004"]},
            {"anilist_id": 5, "type": "TV", "tvdb_id": [50, 51]},
            {"mal_id": 6, "themoviedb_id": {"tv": 600}},
        ]
    )

    assert index.lookup("show", ["100"], [], []) == [1]
    assert index.lookup("movie", ["201"], [], []) == [2]
    assert index.lookup("movie", ["300"], [], []) == [3]
    assert index.lookup("show", ["300"], [], []) == []  # a movie id is not a show id
    assert index.lookup("show", ["400"], [], []) == [4]
    assert index.lookup("movie", [], ["tt0000003"], []) == [3]
    assert index.lookup("show", [], ["tt0000004"], []) == [4]
    assert index.lookup("show", [], [], ["51"]) == [5]
    assert index.lookup("show", [], [], ["7"]) == [1]
    assert index.lookup("movie", [], [], ["7"]) == []  # a movie's TVDB id is another id space
    assert index.lookup("show", ["600"], [], []) == []  # no anilist_id


def test_a_title_absent_from_the_mapping_makes_no_request(conn: Any) -> None:
    conn.execute("DELETE FROM items WHERE item_id != ?", (INCEPTION,))
    conn.commit()
    source = RecordedAniListSource()

    stats = enrich_anilist(conn, source)

    assert source.calls == []
    assert stats.titles_seen == 1 and stats.titles_anime == 0
    cursors = conn.execute(
        "SELECT COUNT(*) FROM enrichment_cursor WHERE source = 'anilist'"
    ).fetchone()[0]
    assert cursors == 0


def test_every_fetched_title_gets_one_cursor_even_when_empty(conn: Any) -> None:
    stats = enrich_anilist(conn, RecordedAniListSource(recorded={5114: []}))

    assert stats.titles_fetched == 2 and stats.titles_matched == 0
    cursors = {
        r[0] for r in conn.execute("SELECT item_id FROM enrichment_cursor WHERE source = 'anilist'")
    }
    assert cursors == {FMAB, EYES}


def test_a_second_run_inside_the_window_sends_no_request(conn: Any) -> None:
    enrich_anilist(conn, RecordedAniListSource())
    again = RecordedAniListSource()

    stats = enrich_anilist(conn, again, stale_days=45)

    assert again.calls == []
    assert stats.titles_cached == 2


def test_titles_are_batched_by_anilist_id_count(conn: Any) -> None:
    source = RecordedAniListSource()

    stats = enrich_anilist(conn, source, batch_ids=2)

    assert [sorted(ids) for ids in source.calls] == [[5114], [300, 1225]]
    assert stats.batches_sent == 2 and stats.anilist_ids_asked == 3


def test_a_failed_batch_writes_nothing_and_is_asked_again(conn: Any) -> None:
    stats = enrich_anilist(conn, RecordedAniListSource(fail_calls={1}), batch_ids=2)

    assert stats.titles_failed == 1 and stats.titles_fetched == 1
    assert _rows(conn, FMAB) == {}
    retry = RecordedAniListSource()
    enrich_anilist(conn, retry, batch_ids=2)
    assert retry.calls == [[5114]]


def test_three_failed_batches_in_a_row_abort(conn: Any) -> None:
    conn.execute("INSERT INTO items (item_id, type, title) VALUES ('tvdb:85249x', 'show', 'x')")
    conn.execute(
        "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
        "VALUES ('tvdb:85249x', 'tvdb', '70973', 'show', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    with pytest.raises(AniListError, match="consecutive"):
        enrich_anilist(conn, RecordedAniListSource(fail_calls={1, 2, 3}), batch_ids=1)


def test_rewipe_removes_only_anilist_rows(conn: Any) -> None:
    enrich_anilist(conn, RecordedAniListSource())
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'keyword', 'alchemy', '2026-01-01T00:00:00+00:00')",
        (FMAB,),
    )
    conn.execute(
        "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'fetched', '2026-01-01T00:00:00+00:00')",
        (FMAB,),
    )
    conn.execute(
        "INSERT INTO keyword_roles (keyword, role, source, stated_at) "
        "VALUES ('alchemy', 'theme', 'wikidata', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    assert wipe(conn) > 0

    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM enrichment")] == ["tmdb"]
    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM enrichment_cursor")] == ["tmdb"]
    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM keyword_roles")] == ["wikidata"]


def test_a_mapping_with_no_anilist_ids_fails_and_keeps_every_row(conn: Any) -> None:
    enrich_anilist(conn, RecordedAniListSource())
    before = conn.execute("SELECT COUNT(*) FROM enrichment WHERE source = 'anilist'").fetchone()[0]
    broken = RecordedAniListSource(entries=[{"mal_id": 1, "themoviedb_id": {"tv": 31911}}])

    with pytest.raises(AniListError, match="no entry with an anilist_id"):
        enrich_anilist(conn, broken, stale_days=0)

    assert broken.calls == []
    after = conn.execute("SELECT COUNT(*) FROM enrichment WHERE source = 'anilist'").fetchone()[0]
    assert after == before
