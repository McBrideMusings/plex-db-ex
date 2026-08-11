"""Reconciling `item_id` against `etv-station`'s `entry_id` (issue #5).

`etv-station`'s side of the join is faked here with the minimal subset of
`catalog.db`'s schema `reconcile_etv.reconcile` actually reads —
`entries.title`, `entry_sources.source`/`source_id`/`entry_id`, and
`entry_external_ids.namespace`/`value`/`entry_id` — rather than driven from a
real Rust-built catalog. That subset is pinned by
`crates/etv-station/src/catalog/schema.rs`'s v1 migration; nothing here
depends on any column this module doesn't touch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb.reconcile_etv import reconcile
from plexdb.store import init as init_plexdb_store
from plexdb.store import open_readonly, open_store

_ETV_DDL = """
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    primary_source TEXT NOT NULL
);
CREATE TABLE entry_sources (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id),
    playback_path TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE entry_external_ids (
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id),
    PRIMARY KEY (namespace, value)
);
"""


def _etv_store(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(_ETV_DDL)


def _add_entry(
    path: Path,
    entry_id: str,
    title: str,
    rating_key: str,
    guids: list[tuple[str, str]] | None = None,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO entries (entry_id, type, title, primary_source) "
            "VALUES (?, 'movie', ?, 'plex')",
            (entry_id, title),
        )
        conn.execute(
            "INSERT INTO entry_sources (source, source_id, entry_id, playback_path) "
            "VALUES ('plex', ?, ?, '')",
            (rating_key, entry_id),
        )
        for ns, value in guids or []:
            conn.execute(
                "INSERT INTO entry_external_ids (namespace, value, entry_id) VALUES (?, ?, ?)",
                (ns, value, entry_id),
            )


def _plexdb_store(path: Path) -> None:
    init_plexdb_store(path)


def _add_item(
    path: Path,
    item_id: str,
    title: str,
    rating_key: str,
    guids: list[tuple[str, str]] | None = None,
) -> None:
    with open_store(path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)", (item_id, title)
        )
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES (?, ?, '1', '2024-01-01T00:00:00+00:00')",
            (rating_key, item_id),
        )
        for ns, value in guids or []:
            conn.execute(
                "INSERT INTO external_ids (item_id, ns, value, kind) VALUES (?, ?, ?, 'movie')",
                (item_id, ns, value),
            )
        conn.commit()


def test_matching_titles_agree(tmp_path: Path) -> None:
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "imdb:tt1", "Die Hard", "100", [("imdb", "tt1")])
    _add_entry(etv, "imdb:tt1", "Die Hard", "100", [("imdb", "tt1")])

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        report = reconcile(p, e)

    assert report.compared == 1
    assert report.agree == 1
    assert report.mismatches == ()
    assert report.only_in_plexdb == ()
    assert report.only_in_etv == ()


def test_a_guid_present_in_only_one_store_is_reported_with_that_reason(tmp_path: Path) -> None:
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "imdb:tt1", "Die Hard", "100", [("imdb", "tt1"), ("tmdb", "562")])
    # etv-station only ever recorded the tmdb guid for this rating key.
    _add_entry(etv, "tmdb:562", "Die Hard", "100", [("tmdb", "562")])

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        report = reconcile(p, e)

    assert report.compared == 1
    assert report.agree == 0
    assert len(report.mismatches) == 1
    mismatch = report.mismatches[0]
    assert mismatch.rating_key == "100"
    assert mismatch.item_id == "imdb:tt1"
    assert mismatch.entry_id == "tmdb:562"
    assert "imdb GUID is present in plex-db-ex but absent from etv-station" in mismatch.reason


def test_an_identity_covering_two_rating_keys_is_reported_as_inherited_not_drifted(
    tmp_path: Path,
) -> None:
    """Issue #23. When one `item_id` covers several rating keys, the later
    ones never ran the derivation rule at all — they inherited an identity
    (ADR-0008). Reporting that as the two implementations having drifted is
    the most alarming message this tool can emit and it is the wrong one."""
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "imdb:tt0047034", "Godzilla", "5550", [("imdb", "tt0047034")])
    with open_store(plexdb) as conn:
        # A second rating key that inherited Godzilla's identity — the show
        # Plex reports as The Golden Girls.
        conn.execute(
            "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
            "VALUES ('141718', 'imdb:tt0047034', '2', '2024-01-01T00:00:00+00:00')"
        )
        conn.commit()
    _add_entry(etv, "imdb:tt0047034", "Godzilla", "5550", [("imdb", "tt0047034")])
    _add_entry(etv, "imdb:tt0088526", "The Golden Girls", "141718", [("imdb", "tt0088526")])

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        report = reconcile(p, e)

    assert len(report.mismatches) == 1
    mismatch = report.mismatches[0]
    assert mismatch.rating_key == "141718"
    assert "covers 2 Plex rating keys" in mismatch.reason
    assert "inherited an identity" in mismatch.reason
    assert "the derivation rule itself disagrees" not in mismatch.reason


def test_both_falling_back_to_a_path_hash_is_reported_as_a_canonical_path_disagreement(
    tmp_path: Path,
) -> None:
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "fs:aaaaaaaaaaaaaaaa", "Home Video", "200")
    _add_entry(etv, "fs:bbbbbbbbbbbbbbbb", "Home Video", "200")

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        report = reconcile(p, e)

    assert len(report.mismatches) == 1
    assert "canonical path disagreed" in report.mismatches[0].reason


def test_a_guid_win_against_a_path_hash_is_reported_as_a_missing_guid(tmp_path: Path) -> None:
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "imdb:tt9", "Unmatched Elsewhere", "300", [("imdb", "tt9")])
    _add_entry(etv, "fs:cccccccccccccccc", "Unmatched Elsewhere", "300")

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        report = reconcile(p, e)

    assert len(report.mismatches) == 1
    reason = report.mismatches[0].reason
    assert "plex-db-ex recorded a imdb GUID" in reason
    assert "etv-station does not have" in reason


def test_titles_present_in_only_one_store_are_counted_and_listed(tmp_path: Path) -> None:
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "imdb:tt1", "Only In Plexdb", "100", [("imdb", "tt1")])
    _add_entry(etv, "imdb:tt2", "Only In Etv", "200", [("imdb", "tt2")])

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        report = reconcile(p, e)

    assert report.compared == 0
    assert report.mismatches == ()
    assert report.only_in_plexdb == (("100", "Only In Plexdb"),)
    assert report.only_in_etv == (("200", "Only In Etv"),)


def test_running_it_twice_produces_the_same_report(tmp_path: Path) -> None:
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)
    _add_item(plexdb, "imdb:tt1", "A", "100", [("imdb", "tt1")])
    _add_item(plexdb, "tmdb:2", "B", "101", [("tmdb", "2")])
    _add_entry(etv, "tvdb:9", "A", "100", [("tvdb", "9")])
    _add_entry(etv, "tmdb:2", "B", "101", [("tmdb", "2")])

    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        first = reconcile(p, e)
    with open_readonly(plexdb) as p, open_readonly(etv) as e:
        second = reconcile(p, e)

    assert first == second
    assert first.compared == 2
    assert first.agree == 1
    assert len(first.mismatches) == 1


def test_a_write_through_either_readonly_handle_raises(tmp_path: Path) -> None:
    """The acceptance criterion that matters: this command must never be
    capable of writing to the other store."""
    plexdb, etv = tmp_path / "plexdb.db", tmp_path / "catalog.db"
    _plexdb_store(plexdb)
    _etv_store(etv)

    with open_readonly(etv) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO entries (entry_id, type, title, primary_source) "
                "VALUES ('x', 'movie', 'X', 'plex')"
            )
