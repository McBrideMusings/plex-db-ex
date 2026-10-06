"""The Wikidata sweep against the recorded SPARQL response in
`fixtures/wikidata/` — never the live endpoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from wikidata_fixtures import RecordedWikidataSource

from plexdb.enrich_wikidata import CANDIDATES_SQL, enrich_wikidata, wipe
from plexdb.errors import WikidataError
from plexdb.store import init as init_store
from plexdb.store import open_store

INCEPTION = "imdb:tt1375666"
RYAN = "imdb:tt0120815"
UNKNOWN = "imdb:tt0000000"


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as c:
        for item_id in (INCEPTION, RYAN, UNKNOWN):
            c.execute(
                "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)",
                (item_id, item_id),
            )
            c.execute(
                "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
                "VALUES (?, 'imdb', ?, 'movie', '2026-01-01T00:00:00+00:00')",
                (item_id, item_id.removeprefix("imdb:")),
            )
        c.commit()
        yield c


def _values(conn: sqlite3.Connection, item_id: str, namespace: str) -> set[str]:
    return {
        row["value"]
        for row in conn.execute(
            "SELECT value FROM enrichment WHERE item_id = ? AND namespace = ? "
            "AND source = 'wikidata'",
            (item_id, namespace),
        )
    }


def _roles(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT keyword, role, score, model, error FROM keyword_roles WHERE source = 'wikidata'"
    ):
        assert (row["score"], row["model"], row["error"]) == (None, None, None)
        out.setdefault(row["role"], set()).add(row["keyword"])
    return out


def test_narrative_locations_become_stemmed_keywords_with_a_region_role(conn: Any) -> None:
    enrich_wikidata(conn, RecordedWikidataSource())

    keywords = _values(conn, INCEPTION, "keywords")
    assert {"los angel", "pari", "australia"} <= keywords
    assert "heist film" in keywords  # a P136 genre, no role
    assert {"los angel", "pari", "australia"} <= _roles(conn)["region"]
    assert "heist film" not in set().union(*_roles(conn).values())
    surfaces = dict(conn.execute("SELECT surface, keyword FROM keyword_forms").fetchall())
    assert surfaces["Los Angeles"] == "los angel"


def test_a_period_states_an_era_and_an_award_lands_in_its_own_namespace(conn: Any) -> None:
    enrich_wikidata(conn, RecordedWikidataSource())

    assert {"1940s", "world war ii", "20th centuri"} <= _roles(conn)["era"]
    assert "Academy Award for Best Director" in _values(conn, RYAN, "awards")
    # An award is not a keyword.
    assert not any("award" in v for v in _values(conn, RYAN, "keywords"))


def test_a_title_wikidata_does_not_know_is_cached_with_nothing_written(conn: Any) -> None:
    stats = enrich_wikidata(conn, RecordedWikidataSource())

    assert stats.titles_failed == 0
    assert stats.titles_fetched == 3 and stats.titles_matched == 2
    assert _values(conn, UNKNOWN, "keywords") == set()
    cursor = conn.execute(
        "SELECT COUNT(*) FROM enrichment_cursor WHERE item_id = ? AND source = 'wikidata'",
        (UNKNOWN,),
    ).fetchone()[0]
    assert cursor == 1


def test_a_second_run_inside_the_window_sends_no_query(conn: Any) -> None:
    enrich_wikidata(conn, RecordedWikidataSource())
    again = RecordedWikidataSource()

    stats = enrich_wikidata(conn, again, stale_days=45)

    assert again.calls == []
    assert stats.titles_cached == 3


def test_titles_are_asked_in_batches_one_query_each(conn: Any) -> None:
    source = RecordedWikidataSource()

    stats = enrich_wikidata(conn, source, batch_size=2)

    assert [len(ids) for ids in source.calls] == [2, 1]
    assert stats.queries_sent == 2


def test_a_failed_batch_writes_nothing_and_is_asked_again(conn: Any) -> None:
    stats = enrich_wikidata(conn, RecordedWikidataSource(fail_calls={1}), batch_size=2)

    assert stats.titles_failed == 2 and stats.titles_fetched == 1
    retry = RecordedWikidataSource()
    enrich_wikidata(conn, retry, batch_size=2)
    assert [len(ids) for ids in retry.calls] == [2]


def test_three_failed_batches_in_a_row_abort(conn: Any) -> None:
    with pytest.raises(WikidataError, match="consecutive"):
        enrich_wikidata(conn, RecordedWikidataSource(fail_calls={1, 2, 3}), batch_size=1)


def test_rewipe_removes_only_wikidata_rows(conn: Any) -> None:
    enrich_wikidata(conn, RecordedWikidataSource())
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'keyword', 'dream', '2026-01-01T00:00:00+00:00')",
        (INCEPTION,),
    )
    conn.execute(
        "INSERT INTO enrichment_cursor (item_id, namespace, source, key, fetched_at) "
        "VALUES (?, 'keywords', 'tmdb', 'fetched', '2026-01-01T00:00:00+00:00')",
        (INCEPTION,),
    )
    conn.execute(
        "INSERT INTO keyword_roles (keyword, role, source, score, model, stated_at) "
        "VALUES ('dream', 'theme', 'jev', 0.9, 'jev-1', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    assert wipe(conn) > 0

    left = conn.execute("SELECT DISTINCT source FROM enrichment").fetchall()
    assert [r[0] for r in left] == ["tmdb"]
    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM enrichment_cursor")] == ["tmdb"]
    assert [r[0] for r in conn.execute("SELECT DISTINCT source FROM keyword_roles")] == ["jev"]


def test_a_stale_title_is_asked_again_and_its_rows_replaced(conn: Any) -> None:
    enrich_wikidata(conn, RecordedWikidataSource())
    conn.execute(
        "UPDATE enrichment_cursor SET fetched_at = '2020-01-01T00:00:00+00:00' WHERE item_id = ?",
        (INCEPTION,),
    )
    conn.commit()
    fewer = RecordedWikidataSource(
        statements_recorded=[("tt1375666", "P840", "Paris"), ("tt1375666", "P166", "Hugo Award")]
    )

    enrich_wikidata(conn, fewer)

    assert fewer.calls == [["tt1375666"]]
    assert _values(conn, INCEPTION, "keywords") == {"pari"}
    assert _values(conn, INCEPTION, "awards") == {"Hugo Award"}


def test_a_malformed_imdb_id_is_skipped_without_failing_its_batch(conn: Any) -> None:
    conn.execute("INSERT INTO items (item_id, type, title) VALUES ('plex:x', 'movie', 'x')")
    conn.execute(
        "INSERT INTO external_ids (item_id, ns, value, kind, last_seen) "
        "VALUES ('plex:x', 'imdb', 'TT12 ', 'movie', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    stats = enrich_wikidata(conn, RecordedWikidataSource())

    assert stats.titles_skipped_no_imdb_id == 1
    assert stats.titles_fetched == 3 and stats.titles_failed == 0


def test_the_candidate_query_searches_the_external_ids_item_index(conn: Any) -> None:
    plan = [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + CANDIDATES_SQL)]
    assert any("idx_external_ids_item" in d for d in plan), plan
