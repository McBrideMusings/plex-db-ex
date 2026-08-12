"""Moving item_id values still keyed on the disk mount point (issue #55).

Seeds a store the way it looked before `PLEX_SOURCE_ROOTS` was set — an `fs:`
id hashed from the raw, unstripped path — then drives the repair against a
`PlexSource` fake holding the real record, and checks the id, and everything
that referenced it, land on what `identity.derive_item_id` produces today.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from plex_fixtures import FakeSource, recorded_source

from plexdb.cli import main
from plexdb.commands import repair_fs_identities as repair_fs_identities_cmd
from plexdb.identity import canonical_path, derive_item_id
from plexdb.plex_client import PLEX_TYPE_MOVIE, Section
from plexdb.repair_fs_identities import (
    apply_fs_repairs,
    fs_item_ids,
    plan_fs_repairs,
    repair_fs_identities,
)
from plexdb.store import init as init_store
from plexdb.store import open_store

#: A movie recorded live with no external GUID at all — real fixture data, so
#: this is the exact shape issue #55 describes: nothing but a path to hash.
AIR_MATER_RATING_KEY = "70936"
AIR_MATER_PATH = "/media/movies/Air Mater (2011) {imdb-tt2100093}/Air Mater (2011).mkv"
#: The id every `fs:` title in the store held before PLEX_SOURCE_ROOTS was
#: set — the raw path hashed with no root stripped off.
OLD_WRONG_ID = derive_item_id([], canonical_path(AIR_MATER_PATH, ()))
#: What the same title derives to once `/media` is configured as a source
#: root, which is the walk's real behaviour today.
NEW_CORRECT_ID = derive_item_id([], canonical_path(AIR_MATER_PATH, ("/media",)))


def _items_with_id(conn: sqlite3.Connection, *item_ids: str) -> int:
    placeholders = ", ".join("?" for _ in item_ids)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM items WHERE item_id IN ({placeholders})", item_ids
        ).fetchone()[0]
    )


def _seed_fs_item(
    conn: sqlite3.Connection,
    item_id: str,
    rating_key: str,
    *,
    title: str = "Air Mater",
    with_play: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title, year) VALUES (?, 'movie', ?, 2011)",
        (item_id, title),
    )
    conn.execute(
        "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) "
        "VALUES (?, ?, '1', '2024-01-01T00:00:00+00:00')",
        (rating_key, item_id),
    )
    if with_play:
        conn.execute(
            "INSERT INTO plays (history_key, item_id, rating_key, plex_account_id, viewed_at, "
            "seconds_watched) VALUES (?, ?, ?, 1, 1000, 300)",
            (f"h-{rating_key}", item_id, rating_key),
        )


def test_an_fs_id_hashed_from_the_raw_path_is_found_and_planned_to_move(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, OLD_WRONG_ID, AIR_MATER_RATING_KEY, with_play=False)
        conn.commit()

        assert fs_item_ids(conn) == [OLD_WRONG_ID]
        plan = plan_fs_repairs(conn, recorded_source(), source_roots=("/media",))

    assert plan.fs_found == 1
    assert plan.moves == {OLD_WRONG_ID: NEW_CORRECT_ID}
    assert plan.unchanged == 0
    assert plan.not_in_plex == 0


def test_repair_moves_the_identity_and_its_play_follows(tmp_path: Path) -> None:
    """The exact scenario issue #55 asks a test to cover: a title whose
    stored id disagrees with its derivation, with a play attached, and the
    play must follow the identity to its corrected id."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, OLD_WRONG_ID, AIR_MATER_RATING_KEY, with_play=True)
        conn.commit()

        stats = repair_fs_identities(conn, recorded_source(), source_roots=("/media",))

        assert stats.fs_found == 1
        assert stats.moved == 1
        assert stats.unchanged == 0
        assert stats.not_in_plex == 0
        assert stats.rows_carried["plex_items"] == 1
        assert stats.rows_carried["plays"] == 1
        assert "items" not in stats.rows_carried, "items itself is not a 'referencing' row"

        item = conn.execute(
            "SELECT item_id, title FROM items WHERE item_id = ?", (NEW_CORRECT_ID,)
        ).fetchone()
        assert item is not None
        assert item["title"] == "Air Mater"
        assert _items_with_id(conn, OLD_WRONG_ID) == 0, "the old fs: row must not survive the move"

        plex_item = conn.execute(
            "SELECT item_id FROM plex_items WHERE rating_key = ?", (AIR_MATER_RATING_KEY,)
        ).fetchone()
        assert plex_item["item_id"] == NEW_CORRECT_ID

        play = conn.execute(
            "SELECT item_id, seconds_watched FROM plays WHERE history_key = ?",
            (f"h-{AIR_MATER_RATING_KEY}",),
        ).fetchone()
        assert play["item_id"] == NEW_CORRECT_ID, "the play must follow its identity"
        assert play["seconds_watched"] == 300, "columns other than item_id survive the move"


def test_repair_on_a_clean_store_changes_nothing(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        stats = repair_fs_identities(conn, recorded_source(), source_roots=("/media",))

    assert stats.fs_found == 0
    assert stats.moved == 0
    assert stats.rows_carried == {}


def test_repair_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, OLD_WRONG_ID, AIR_MATER_RATING_KEY)
        conn.commit()

        first = repair_fs_identities(conn, recorded_source(), source_roots=("/media",))
        second = repair_fs_identities(conn, recorded_source(), source_roots=("/media",))

    assert first.moved == 1
    assert second.moved == 0
    assert second.fs_found == 1, "the corrected id is still fs: — there is no external GUID"
    assert second.unchanged == 1


def test_a_title_no_longer_in_plex_is_skipped_and_counted_not_guessed_at(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, OLD_WRONG_ID, "999999", with_play=False)
        conn.commit()

        stats = repair_fs_identities(conn, recorded_source(), source_roots=("/media",))

        assert stats.fs_found == 1
        assert stats.moved == 0
        assert stats.not_in_plex == 1
        assert _items_with_id(conn, OLD_WRONG_ID) == 1, (
            "an identity that cannot be re-derived is left exactly where it is"
        )


def test_two_fs_ids_converging_on_the_same_corrected_path_are_merged(tmp_path: Path) -> None:
    """Two rating keys with no external GUID whose Plex paths canonicalise to
    the exact same string — a duplicate library entry, the same shape #19
    already merges for a GUID'd title. Both converge on one corrected id, and
    every row either one carried survives under it."""
    movie = {
        "ratingKey": "5550",
        "type": "movie",
        "title": "Dup",
        "Media": [{"Part": [{"file": "/media/movies/Dup/dup.mkv"}]}],
    }
    duplicate = dict(movie, ratingKey="5551")
    source = FakeSource(
        section_list=[
            Section(key="1", type="movie", title="Movies"),
            Section(key="3", type="movie", title="More Movies"),
        ],
        records={
            ("1", PLEX_TYPE_MOVIE): [movie],
            ("3", PLEX_TYPE_MOVIE): [duplicate],
        },
    )
    old_a = derive_item_id([], canonical_path("/media/movies/Dup/dup.mkv", ()))
    # A distinct wrong id for the second copy — as if it had been walked
    # under a slightly different raw mount path before the root was fixed.
    old_b = derive_item_id([], "/mnt/plex" + canonical_path("/media/movies/Dup/dup.mkv", ()))
    new_id = derive_item_id([], canonical_path("/media/movies/Dup/dup.mkv", ("/media",)))
    assert old_a != old_b
    assert new_id not in (old_a, old_b)

    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, old_a, "5550", title="Dup A")
        _seed_fs_item(conn, old_b, "5551", title="Dup B")
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')",
            (old_a,),
        )
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')",
            (old_b,),
        )
        conn.commit()

        plan = plan_fs_repairs(conn, source, source_roots=("/media",))
        result = apply_fs_repairs(conn, plan)

        assert len(plan.moves) == 2
        assert result.merges == 1, "one of the two moves merges into the other's items row"
        assert result.duplicates_dropped.get("enrichment") == 1, (
            "the second identical enrichment fact is dropped, not duplicated"
        )
        assert _items_with_id(conn, old_a, old_b) == 0
        assert _items_with_id(conn, new_id) == 1, "both rows land under the same corrected identity"

        plex_items = {
            r["rating_key"]: r["item_id"]
            for r in conn.execute("SELECT rating_key, item_id FROM plex_items")
        }
        assert plex_items == {"5550": new_id, "5551": new_id}, (
            "both plex_items rows carry across even though they merged"
        )

        # The duplicate enrichment fact folds into one row rather than erroring.
        enrichment_rows = conn.execute(
            "SELECT item_id, namespace, key, value FROM enrichment WHERE item_id = ?",
            (new_id,),
        ).fetchall()
        assert len(enrichment_rows) == 1

        plays = {
            r["history_key"]: r["item_id"]
            for r in conn.execute("SELECT history_key, item_id FROM plays")
        }
        assert plays == {"h-5550": new_id, "h-5551": new_id}, "both plays carry across the merge"


def test_plan_fs_repairs_is_read_only(tmp_path: Path) -> None:
    """The command relies on this to decide whether a backup is warranted
    before writing anything — planning must never itself write."""
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, OLD_WRONG_ID, AIR_MATER_RATING_KEY)
        conn.commit()

        plan_fs_repairs(conn, recorded_source(), source_roots=("/media",))

        assert fs_item_ids(conn) == [OLD_WRONG_ID], "planning must not move anything"


def test_apply_fs_repairs_with_no_moves_is_a_no_op(tmp_path: Path) -> None:
    store = tmp_path / "plexdb.db"
    init_store(store)
    with open_store(store) as conn:
        plan = plan_fs_repairs(conn, recorded_source(), source_roots=("/media",))
        result = apply_fs_repairs(conn, plan)

    assert result.rows_carried == {}
    assert result.merges == 0
    assert result.duplicates_dropped == {}


def test_the_command_does_not_roll_back_a_legitimate_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression test: the command's backup/rollback guard once compared raw
    `items`/`enrichment` row counts before and after, and a merge — which
    correctly shrinks both by design — looked identical to real data loss.
    Drives the actual CLI command, backup and all, through a merge and
    confirms it succeeds rather than restoring the backup it just took."""
    movie = {
        "ratingKey": "5550",
        "type": "movie",
        "title": "Dup",
        "Media": [{"Part": [{"file": "/media/movies/Dup/dup.mkv"}]}],
    }
    duplicate = dict(movie, ratingKey="5551")
    source = FakeSource(
        section_list=[
            Section(key="1", type="movie", title="Movies"),
            Section(key="3", type="movie", title="More Movies"),
        ],
        records={
            ("1", PLEX_TYPE_MOVIE): [movie],
            ("3", PLEX_TYPE_MOVIE): [duplicate],
        },
    )
    old_a = derive_item_id([], canonical_path("/media/movies/Dup/dup.mkv", ()))
    old_b = derive_item_id([], "/mnt/plex" + canonical_path("/media/movies/Dup/dup.mkv", ()))
    new_id = derive_item_id([], canonical_path("/media/movies/Dup/dup.mkv", ("/media",)))

    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEX_URL", "http://plex.example:32400")
    monkeypatch.setenv("PLEX_TOKEN", "test-token")
    monkeypatch.setenv("PLEX_SOURCE_ROOTS", "/media")
    monkeypatch.setattr(repair_fs_identities_cmd, "LivePlexClient", lambda *a, **k: source)

    init_store(store)
    with open_store(store) as conn:
        _seed_fs_item(conn, old_a, "5550", title="Dup A")
        _seed_fs_item(conn, old_b, "5551", title="Dup B")
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')",
            (old_a,),
        )
        conn.execute(
            "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
            "VALUES (?, 'mdblist', 'list', 'top250', '2024-01-01T00:00:00+00:00')",
            (old_b,),
        )
        conn.commit()

    assert main(["repair-fs-identities"]) == 0, (
        "a legitimate merge must not be treated as data loss"
    )

    captured = capsys.readouterr()
    assert "rolled back" not in captured.err
    with open_store(store) as conn:
        assert _items_with_id(conn, old_a, old_b) == 0
        assert _items_with_id(conn, new_id) == 1
