"""`keywords.ROLES` against the role set the frozen v14 migration CHECKs, and
`normalize_keyword`'s handling of punctuation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb.keywords import ROLES, normalize_keyword, state_role, upsert_keyword_form
from plexdb.store import init as init_store
from plexdb.store import open_store


def test_the_constant_is_exactly_the_set_the_schema_accepts(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        for role in ROLES:
            state_role(conn, "shark", role, "test", "2026-01-01T00:00:00+00:00")
            conn.execute(
                "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) "
                "VALUES ('shark', ?, 'accepted', '2026-01-01T00:00:00+00:00')",
                (role,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO keyword_roles (keyword, role, source, stated_at) "
                "VALUES ('shark', 'mood', 'test', '2026-01-01T00:00:00+00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO keyword_role_decisions (keyword, role, decision, decided_at) "
                "VALUES ('shark', 'mood', 'accepted', '2026-01-01T00:00:00+00:00')"
            )


def test_stating_an_unknown_role_is_refused_before_sql(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn, pytest.raises(ValueError, match="mood"):
        state_role(conn, "shark", "mood", "test", "2026-01-01T00:00:00+00:00")


@pytest.mark.parametrize(
    ("surface", "same_as"),
    [
        ("Dreamlike, quirky, and surreal", "dreamlike quirky and surreal"),
        ("Football (Soccer)", "football soccer"),
        ("sci‑fi romance", "sci-fi romance"),
        ("Women’s prison", "women's prison"),
        ("social & cultural documentary", "social cultural documentary"),
    ],
)
def test_punctuation_at_a_word_edge_does_not_stop_it_stemming(surface: str, same_as: str) -> None:
    assert normalize_keyword(surface) == normalize_keyword(same_as)


def test_punctuation_inside_a_word_and_symbols_stay() -> None:
    assert normalize_keyword("9/11") == "9/11"
    assert normalize_keyword("c++") == "c++"
    assert normalize_keyword("C#,") == "c#"
    assert normalize_keyword("(100%)") == "100%"


def test_an_all_punctuation_surface_is_no_keyword_and_records_no_form(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        assert upsert_keyword_form(conn, " ,, & ") == ""
        assert conn.execute("SELECT COUNT(*) FROM keyword_forms").fetchone()[0] == 0


def test_a_rewrite_moves_a_surface_to_the_current_rule(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO keyword_forms (surface, keyword) "
            "VALUES ('Miami, Florida', 'miami, florida')"
        )
        assert upsert_keyword_form(conn, "Miami, Florida") == "miami florida"
        stored = conn.execute(
            "SELECT keyword FROM keyword_forms WHERE surface = 'Miami, Florida'"
        ).fetchone()
    assert tuple(stored) == ("miami florida",)
