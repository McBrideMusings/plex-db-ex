"""`keywords.ROLES` against the role set the frozen v14 migration CHECKs, and
`normalize_keyword`'s handling of punctuation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from plexdb.cli import main
from plexdb.keywords import (
    FILM_SUFFIX_MODEL,
    ROLES,
    RawKeyword,
    normalize_keyword,
    prune_keyword_verdicts,
    rederive_keywords,
    state_role,
    upsert_keyword_form,
    write_film_suffix_verdicts,
    write_title_keywords,
)
from plexdb.roles import fold_role_decisions
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
OLD = "\u200bhidden world"


def _carry(conn: sqlite3.Connection, keyword: str) -> None:
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, source, key, value, fetched_at) "
        "VALUES ('imdb:tt1', 'keywords', 'tmdb', 'keyword', ?, ?)",
        (keyword, AT),
    )


def _judge(conn: sqlite3.Connection, keyword: str) -> None:
    conn.execute(
        "INSERT INTO keyword_roles (keyword, role, source, score, model, stated_at) "
        "VALUES (?, 'theme', 'jev', 0.9, 'jev-test', ?)",
        (keyword, AT),
    )


def _judged_store(tmp_path: Path) -> Path:
    """A title carrying `OLD` and `heist`, each judged and decided, and a decided pair."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'T')")
        for keyword in (OLD, "heist"):
            _carry(conn, keyword)
            _judge(conn, keyword)
            conn.execute(
                "INSERT INTO keyword_role_decisions VALUES (?, 'theme', 'accepted', ?)",
                (keyword, AT),
            )
        conn.execute(
            "INSERT INTO keyword_pairs (keyword_a, keyword_b, jev_score, jev_model, judged_at, "
            "decision, decided_at) VALUES ('heist', ?, 0.8, 'jev-test', ?, 'rejected', ?)",
            (OLD, AT, AT),
        )
        conn.commit()
    return store


def test_a_value_a_refetch_moved_loses_its_rows_and_a_carried_value_keeps_its(
    tmp_path: Path,
) -> None:
    store = _judged_store(tmp_path)
    with open_store(store) as conn:
        conn.execute("DELETE FROM enrichment WHERE value = ?", (OLD,))
        _carry(conn, "hidden world")
        conn.commit()
        stats = prune_keyword_verdicts(conn)
        keyed = {
            table: [r[0] for r in conn.execute(f"SELECT {column} FROM {table} ORDER BY 1")]
            for table, column in (
                ("keyword_pairs", "keyword_a"),
                ("keyword_roles", "keyword"),
                ("keyword_role_decisions", "keyword"),
            )
        }
    assert keyed == {
        "keyword_pairs": [],
        "keyword_roles": ["heist"],
        "keyword_role_decisions": ["heist"],
    }
    assert (stats.keywords_pruned, stats.keywords_stored) == ([OLD], 2)
    assert (stats.pairs_deleted, stats.roles_deleted, stats.decisions_deleted) == (1, 1, 1)


def test_the_command_prints_the_counts_and_each_pruned_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _judged_store(tmp_path)
    with open_store(store) as conn:
        conn.execute("DELETE FROM enrichment WHERE value = ?", (OLD,))
        conn.commit()
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    assert main(["prune-keyword-verdicts"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "pruned 1 keyword value(s) no title carries (of 1 stored): "
        "1 pair row(s), 1 role row(s), 1 decision row(s)",
        "pruned keyword '\\u200bhidden world'",
    ]


def test_a_pruned_decision_comes_back_from_its_file_once_the_value_is_judged_again(
    tmp_path: Path,
) -> None:
    store = _judged_store(tmp_path)
    decisions = tmp_path / "role_decisions.json"
    decisions.write_text(
        json.dumps([{"keyword": OLD, "role": "theme", "decision": "accepted", "decided_at": AT}])
    )
    with open_store(store) as conn:
        conn.execute("DELETE FROM enrichment WHERE value = ?", (OLD,))
        conn.commit()
        prune_keyword_verdicts(conn)
        assert fold_role_decisions(conn, decisions).unmatched == 1
        _carry(conn, OLD)
        _judge(conn, OLD)
        conn.commit()
        fold_role_decisions(conn, decisions)
        restored = conn.execute(
            "SELECT decision FROM keyword_role_decisions WHERE keyword = ?", (OLD,)
        ).fetchone()
    assert tuple(restored) == ("accepted",)


def test_a_value_carried_only_as_a_spoiler_keyword_keeps_its_rows(tmp_path: Path) -> None:
    store = _judged_store(tmp_path)
    with open_store(store) as conn:
        conn.execute("UPDATE enrichment SET key = 'spoiler_keyword' WHERE value = 'heist'")
        conn.commit()
        stats = prune_keyword_verdicts(conn)
        kept = conn.execute("SELECT COUNT(*) FROM keyword_roles WHERE keyword = 'heist'").fetchone()
    assert kept[0] == 1
    assert stats.keywords_pruned == []


def test_the_film_suffix_rule_pairs_x_film_with_a_stored_x_and_drops_an_undecided_stale_pair(
    tmp_path: Path,
) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'x')")
        for value in ("christmas", "christmas film", "short film", "art", "art film", "horror"):
            _carry(conn, value)
        conn.executemany(
            "INSERT INTO keyword_pairs "
            "(keyword_a, keyword_b, jev_score, jev_model, judged_at, decision, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("christmas", "christmas film", 0.09, "jev-1", AT, "rejected", AT),
                ("horror", "horror film", 1.0, FILM_SUFFIX_MODEL, AT, None, None),
                ("heist", "heist film", 1.0, FILM_SUFFIX_MODEL, AT, "rejected", AT),
            ],
        )
        conn.commit()

        stats = write_film_suffix_verdicts(conn, "2026-10-09T00:00:00+00:00")
        rows = conn.execute(
            "SELECT keyword_a, keyword_b, jev_score, jev_model, decision FROM keyword_pairs"
        ).fetchall()

    assert (stats.pairs, stats.pairs_written, stats.pairs_removed) == (1, 1, 1)
    assert sorted(tuple(r) for r in rows) == [
        ("christmas", "christmas film", 1.0, FILM_SUFFIX_MODEL, "rejected"),
        ("heist", "heist film", 1.0, FILM_SUFFIX_MODEL, "rejected"),
    ]
