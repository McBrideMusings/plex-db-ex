"""`keywords.ROLES` against the role set the frozen v14 migration CHECKs, and
`normalize_keyword`'s handling of punctuation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fixture_store import init as init_store

from plexdb.keywords import (
    ROLES,
    RawKeyword,
    normalize_keyword,
    rederive_keywords,
    state_role,
    upsert_keyword_form,
    write_title_keywords,
)
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
        ("heists\u200b,", "heists"),
        ("\u200b(heists)\u2060", "heists"),
        ("café.\u0301", "café"),
        ("\u0301(heists", "heists"),
    ],
)
def test_punctuation_at_a_word_edge_does_not_stop_it_folding(surface: str, same_as: str) -> None:
    assert normalize_keyword(surface) == normalize_keyword(same_as)


@pytest.mark.parametrize(
    ("surface", "stored"),
    [
        ("Heists", "heist"),
        ("Vampires", "vampire"),
        ("comedies", "comedy"),
        ("Women's prison", "woman prison"),
        ("Christmas", "christmas"),
        ("Christmas film", "christmas film"),
        ("boxing", "boxing"),
        ("racing", "racing"),
        ("murderer", "murderer"),
        ("Mars", "mars"),
        ("Las Vegas", "las vegas"),
        ("United States", "united states"),
        ("news", "news"),
    ],
)
def test_a_plural_folds_to_its_singular_and_every_other_word_stays(
    surface: str, stored: str
) -> None:
    assert normalize_keyword(surface) == stored


def test_a_title_keeps_each_spelling_and_derives_one_row_per_stored_value(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('t', 'movie', 'x')")
        stored = write_title_keywords(
            conn,
            "t",
            "anilist",
            [
                RawKeyword("Heists", rank=40),
                RawKeyword("heist", rank=80, spoiler=True),
                RawKeyword("Heists", rank=10),
                RawKeyword(" & "),
                RawKeyword("Christmas"),
            ],
            AT,
        )
        surfaces = conn.execute(
            "SELECT surface, rank, spoiler FROM keyword_surfaces ORDER BY surface"
        ).fetchall()
        rows = conn.execute(
            "SELECT key, value, rank FROM enrichment WHERE item_id = 't' ORDER BY value"
        ).fetchall()
    assert stored == {"Heists": "heist", "heist": "heist", "Christmas": "christmas"}
    assert [tuple(r) for r in surfaces] == [
        ("&", None, 0),
        ("Christmas", None, 0),
        ("Heists", 40, 0),
        ("heist", 80, 1),
    ]
    assert [tuple(r) for r in rows] == [
        ("keyword", "christmas", None),
        ("spoiler_keyword", "heist", 80),
    ]


def test_rederiving_rebuilds_every_keyword_row_and_form_from_the_spellings(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('t', 'movie', 'x')")
        write_title_keywords(conn, "t", "tmdb", [RawKeyword("Vampires")], AT)
        conn.execute("UPDATE enrichment SET value = 'vampir'")
        conn.execute("UPDATE keyword_forms SET keyword = 'vampir'")
        written = rederive_keywords(conn)
        rows = conn.execute("SELECT value FROM enrichment").fetchall()
        forms = conn.execute("SELECT surface, keyword FROM keyword_forms").fetchall()
    assert written == 1
    assert [tuple(r) for r in rows] == [("vampire",)]
    assert [tuple(r) for r in forms] == [("Vampires", "vampire")]


def test_punctuation_inside_a_word_and_symbols_stay() -> None:
    assert normalize_keyword("9/11") == "9/11"
    assert normalize_keyword("c++") == "c++"
    assert normalize_keyword("C#,") == "c#"
    assert normalize_keyword("(100%)") == "100%"
    assert normalize_keyword("کتاب\u200cها,") == ("کتاب\u200cها")


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


AT = "2026-01-01T00:00:00+00:00"


def test_normalize_keyword_returns_what_the_published_fixture_says() -> None:
    """`tests/fixtures/keyword_normalization.json` is the rule's specification, and the
    Rust normalizer (`crates/plexdb-keywords/tests/golden.rs`) is held to it too. A
    change to this function, or a simplemma bump, that the fixture does not follow
    fails here."""

    path = Path(__file__).parent / "fixtures" / "keyword_normalization.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    wrong = [(s, want, normalize_keyword(s)) for s, want in cases if normalize_keyword(s) != want]
    assert wrong[:10] == []
