"""The CLI creates the store where configuration says, and reports what it did."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from plex_fixtures import (
    FakeHistorySource,
    FakeSource,
    recorded_history_source,
    recorded_source,
)
from tmdb_fixtures import FakeTMDbSource

from plexdb import cli
from plexdb.cli import main
from plexdb.config import Config, ConfigError
from plexdb.store import init as init_store
from plexdb.store import open_store
from plexdb.walk import walk_all


def _fixture_backed_client(base_url: str, token: str) -> FakeSource:
    """Stands in for `LivePlexClient` in a CLI test: same call shape
    (`base_url`, `token`), returns the same recorded fixtures every other walk
    test uses. No network."""
    assert base_url, "walk must pass the configured PLEX_URL through"
    assert token, "walk must pass the configured PLEX_TOKEN through"
    return recorded_source()


def _fixture_backed_history_client(base_url: str, token: str) -> FakeHistorySource:
    """Stands in for `LivePlexClient` in an ingest-plays CLI test: same call
    shape, returns the recorded history fixtures every `test_plays.py` test
    uses. No network."""
    assert base_url, "ingest-plays must pass the configured PLEX_URL through"
    assert token, "ingest-plays must pass the configured PLEX_TOKEN through"
    return recorded_history_source()


def _configure_ingest_plays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a store under `tmp_path`, already walked from the
    recorded library fixtures — so `plex_items` can resolve the history
    fixture's rating keys — with the recorded history fixtures standing in
    for Plex's history and device endpoints. Returns the store path."""
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEX_URL", "http://plex.example:32400")
    monkeypatch.setenv("PLEX_TOKEN", "test-token")
    init_store(store)
    with open_store(store) as conn:
        walk_all(conn, recorded_source())
    monkeypatch.setattr(cli, "LivePlexClient", _fixture_backed_history_client)
    return store


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


def _configure_enrich(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeTMDbSource
) -> Path:
    """Point the CLI at a store under `tmp_path`, with a TMDB key set and
    `fake` standing in for `LiveTMDbClient`. No network."""
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("TMDB_API_KEY", "test-tmdb-key")

    def _fixture_backed_client(api_key: str) -> FakeTMDbSource:
        assert api_key == "test-tmdb-key", "enrich-tmdb-keywords must pass the configured key"
        return fake

    monkeypatch.setattr(cli, "LiveTMDbClient", _fixture_backed_client)
    return store


def _seed_one_movie(store: Path) -> None:
    with sqlite3.connect(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES "
            "('imdb:tt0468569', 'movie', 'The Dark Knight')"
        )
        conn.execute(
            "INSERT INTO external_ids (item_id, ns, value) VALUES ('imdb:tt0468569', 'tmdb', '155')"
        )


def test_enrich_tmdb_keywords_writes_rows_and_reports_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero", "gotham city"]})
    store = _configure_enrich(tmp_path, monkeypatch, fake)
    main(["init"])
    _seed_one_movie(store)
    capsys.readouterr()

    assert main(["enrich-tmdb-keywords"]) == 0

    out = capsys.readouterr().out
    assert "1 title(s) seen" in out
    assert "1 fetched" in out
    assert "2 keyword(s) written" in out

    with sqlite3.connect(store) as conn:
        count = conn.execute(
            "SELECT count(*) FROM enrichment WHERE namespace = 'tmdb_keywords'"
        ).fetchone()[0]
    assert count == 3  # the sentinel row plus the two keyword rows


def test_a_second_enrich_tmdb_keywords_run_does_not_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero"]})
    store = _configure_enrich(tmp_path, monkeypatch, fake)
    main(["init"])
    _seed_one_movie(store)
    capsys.readouterr()
    main(["enrich-tmdb-keywords"])
    capsys.readouterr()

    assert main(["enrich-tmdb-keywords"]) == 0

    out = capsys.readouterr().out
    assert "0 fetched" in out
    assert "1 already cached" in out
    assert fake.calls == [("155", "movie")]


def test_enrich_tmdb_keywords_rewipe_forces_a_full_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTMDbSource(keywords_by_id={("155", "movie"): ["superhero"]})
    store = _configure_enrich(tmp_path, monkeypatch, fake)
    main(["init"])
    _seed_one_movie(store)
    main(["enrich-tmdb-keywords"])
    capsys.readouterr()

    assert main(["enrich-tmdb-keywords", "--rewipe"]) == 0

    out = capsys.readouterr().out
    assert "wiped" in out
    assert "1 fetched" in out
    assert fake.calls == [("155", "movie"), ("155", "movie")]


def test_ingest_plays_writes_rows_and_reports_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _configure_ingest_plays(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["ingest-plays"]) == 0

    out = capsys.readouterr().out
    assert "ingested 3 play(s) from 4 event(s) seen, 0 already recorded" in out
    assert "1 event(s) had a rating key not in the walk's map" in out
    assert "1 event(s) had a device id not in Plex's device list" in out

    with sqlite3.connect(store) as conn:
        count = conn.execute("SELECT count(*) FROM plays").fetchone()[0]
    assert count == 3


def test_ingest_plays_a_second_time_adds_no_duplicate_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _configure_ingest_plays(tmp_path, monkeypatch)
    main(["ingest-plays"])
    capsys.readouterr()

    assert main(["ingest-plays"]) == 0

    out = capsys.readouterr().out
    assert "ingested 0 play(s)" in out

    with sqlite3.connect(store) as conn:
        count = conn.execute("SELECT count(*) FROM plays").fetchone()[0]
    assert count == 3


def test_enrich_tmdb_keywords_without_an_api_key_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    # Set (not delete): see the matching comment on the walk test above —
    # deleting would let the worktree's own `.env` leak a real key back in.
    monkeypatch.setenv("TMDB_API_KEY", "")
    main(["init"])
    capsys.readouterr()

    assert main(["enrich-tmdb-keywords"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "TMDB_API_KEY" in err
    assert "Traceback" not in err


def test_config_reads_the_tmdb_stale_days_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_KEYWORDS_STALE_DAYS", "10")

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.tmdb_keywords_stale_days == 10


def test_config_defaults_the_tmdb_stale_days_to_45(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_KEYWORDS_STALE_DAYS", raising=False)

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.tmdb_keywords_stale_days == 45


def test_config_rejects_a_non_numeric_tmdb_stale_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_KEYWORDS_STALE_DAYS", "soon")

    with pytest.raises(ConfigError):
        Config.from_env(env_file=Path("/nonexistent/.env"))


def test_ingest_plays_without_plex_credentials_configured_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    # Set (not delete): see the same note on `test_walk_without_plex_credentials…`.
    monkeypatch.setenv("PLEX_URL", "")
    monkeypatch.setenv("PLEX_TOKEN", "")
    main(["init"])
    capsys.readouterr()

    assert main(["ingest-plays"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "PLEX_URL" in err
    assert "PLEX_TOKEN" in err
    assert "Traceback" not in err


def test_ingest_plays_without_a_store_says_so_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", str(tmp_path / "plexdb.db"))
    monkeypatch.setenv("PLEX_URL", "http://plex.example:32400")
    monkeypatch.setenv("PLEX_TOKEN", "test-token")
    monkeypatch.setattr(cli, "LivePlexClient", _fixture_backed_history_client)
    # no `init` — the store never gets created

    assert main(["ingest-plays"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "plexdb init" in err
    assert "Traceback" not in err
