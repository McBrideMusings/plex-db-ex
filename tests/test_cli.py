"""The CLI creates the store where configuration says, and reports what it did."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from plex_fixtures import FakeSource, recorded_source

from plexdb import cli
from plexdb.cli import main
from plexdb.config import Config, ConfigError


def _fixture_backed_client(base_url: str, token: str) -> FakeSource:
    """Stands in for `LivePlexClient` in a CLI test: same call shape
    (`base_url`, `token`), returns the same recorded fixtures every other walk
    test uses. No network."""
    assert base_url, "walk must pass the configured PLEX_URL through"
    assert token, "walk must pass the configured PLEX_TOKEN through"
    return recorded_source()


def _configure_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a store under `tmp_path`, with Plex credentials set and
    the recorded fixtures standing in for a server. Returns the store path; the
    store itself is not created, so a test can choose whether to `init` first."""
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEX_URL", "http://plex.example:32400")
    monkeypatch.setenv("PLEX_TOKEN", "test-token")
    monkeypatch.setattr(cli, "LivePlexClient", _fixture_backed_client)
    return store


def test_init_creates_the_store_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))

    assert main(["init"]) == 0

    out = capsys.readouterr().out
    assert "created store" in out
    assert str(store.resolve()) in out
    assert store.exists()


def test_a_second_init_reports_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", str(tmp_path / "plexdb.db"))
    main(["init"])
    capsys.readouterr()

    assert main(["init"]) == 0
    assert "already current" in capsys.readouterr().out


def test_an_empty_store_path_is_an_error_not_a_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", "   ")

    assert main(["init"]) == 1
    assert "PLEXDB_PATH" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("directory", "is a directory"),
        ("not-a-database", "not a plexdb store"),
        ("unwritable-parent", "cannot create"),
    ],
)
def test_a_bad_store_path_prints_a_message_not_a_traceback(
    kind: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every one of these used to escape `main()` as a Python stack trace."""
    if kind == "directory":
        target = tmp_path / "adir"
        target.mkdir()
    elif kind == "not-a-database":
        target = tmp_path / "notes.txt"
        target.write_text("this is not a database\n")
    else:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        target = locked / "sub" / "plexdb.db"

    monkeypatch.setenv("PLEXDB_PATH", str(target))
    try:
        assert main(["init"]) == 1
    finally:
        if kind == "unwritable-parent":
            (tmp_path / "locked").chmod(0o700)

    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert expected in err
    assert "Traceback" not in err


def test_config_defaults_the_path_but_never_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLEXDB_PATH", raising=False)

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.store_path == Path("./data/plexdb.db")


def test_config_rejects_a_blank_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEXDB_PATH", "")

    with pytest.raises(ConfigError):
        Config.from_env(env_file=Path("/nonexistent/.env"))


def test_config_defaults_the_snapshot_path_to_nothing_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLEXDB_SNAPSHOT_PATH", raising=False)

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.snapshot_path is None


def test_config_defaults_source_roots_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLEX_SOURCE_ROOTS", raising=False)

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.source_roots == ()


def test_config_splits_source_roots_on_commas_and_trims_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_SOURCE_ROOTS", " /media/movies ,/media/television,, /data ")

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.source_roots == ("/media/movies", "/media/television", "/data")


def test_config_reads_plex_url_and_token_and_never_defaults_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLEX_URL", raising=False)
    monkeypatch.delenv("PLEX_TOKEN", raising=False)

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.plex_url == ""
    assert config.plex_token == ""


def test_publish_writes_a_snapshot_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))
    main(["init"])
    capsys.readouterr()

    assert main(["publish"]) == 0

    out = capsys.readouterr().out
    assert "published snapshot" in out
    assert str(snapshot.resolve()) in out
    assert snapshot.exists()


def test_publish_without_a_snapshot_path_configured_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.delenv("PLEXDB_SNAPSHOT_PATH", raising=False)
    main(["init"])
    capsys.readouterr()

    assert main(["publish"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "PLEXDB_SNAPSHOT_PATH" in err


def test_walk_writes_items_and_reports_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _configure_walk(tmp_path, monkeypatch)
    main(["init"])
    capsys.readouterr()

    assert main(["walk"]) == 0

    out = capsys.readouterr().out
    assert "walked 4 section(s)" in out
    assert "6 title(s) seen, 6 written" in out
    assert "1 fell back to a path-derived id" in out

    with sqlite3.connect(store) as conn:
        count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    assert count == 6


def test_walk_scoped_to_one_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure_walk(tmp_path, monkeypatch)
    main(["init"])
    capsys.readouterr()

    assert main(["walk", "--section", "1"]) == 0

    out = capsys.readouterr().out
    assert "walked 1 section(s)" in out
    assert "2 title(s) seen, 2 written" in out


def test_walk_without_plex_credentials_configured_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    # Set (not delete): `Config.from_env` calls `load_dotenv(override=False)`,
    # which only fills a var that is *absent* from the environment — deleting
    # these would let the worktree's own `.env` (real PLEX_URL/PLEX_TOKEN,
    # needed for `verify`) leak back in and mask the case under test.
    monkeypatch.setenv("PLEX_URL", "")
    monkeypatch.setenv("PLEX_TOKEN", "")
    main(["init"])
    capsys.readouterr()

    assert main(["walk"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "PLEX_URL" in err
    assert "PLEX_TOKEN" in err
    assert "Traceback" not in err


def test_walk_without_a_store_says_so_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure_walk(tmp_path, monkeypatch)  # no `init` — the store never gets created

    assert main(["walk"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "plexdb init" in err
    assert "Traceback" not in err
