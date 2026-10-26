"""The CLI creates the store where configuration says, and reports what it did."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
from typing import Any

import pytest
from fixture_store import init as init_store

from plexdb.cli import _register_commands, build_parser, main
from plexdb.clusters import LATENT_USER_FLOOR
from plexdb.commands import iter_command_modules
from plexdb.commands import latent_users as latent_users_cmd
from plexdb.config import Config, ConfigError
from plexdb.store import open_store

EXPECTED_COMMANDS = {
    "check",
    "idle",
    "steps",
    "sweep",
    "check-files",
    "record-panels",
    "publish",
    "enrich-anilist",
    "enrich-letterboxd",
    "enrich-wikidata",
    "refresh-map",
    "refresh-tagnetwork",
    "enrich-mdblist-ratings",
    "latent-users",
    "explore",
    "frames",
    "search",
    "schedule",
}

# The order someone runs them in, which since ADR-0014 is also the order
# `plexdb sweep` runs them: create the store, fill it with the library and the
# plays, enrich it, publish it last. Each module owns its own ORDER, so this
# list is the only place the sequence is written down.
EXPECTED_COMMAND_ORDER = [
    # First, and not a step of the sweep: the read-only question you ask before
    # deciding to run anything else (`plexdb/commands/check.py`).
    "check",
    # Beside it, and read-only too: whether nothing holds the store now
    # (`plexdb/commands/idle.py`).
    "idle",
    # Every sweep step and which implementation runs it (`plexdb/commands/steps.py`).
    "steps",
    "sweep",
    "check-files",
    "record-panels",
    "enrich-anilist",
    "enrich-letterboxd",
    "enrich-wikidata",
    "enrich-mdblist-ratings",
    "refresh-map",
    "refresh-tagnetwork",
    "latent-users",
    # The Frame index's status, queue and migration (`plexdb/commands/frames.py`).
    "frames",
    # Ranks Shots against a Phrase through `plexdb-read` (`plexdb/commands/search.py`).
    "search",
    # Not a step of the sweep: a read-only browser view of what the steps above
    # wrote (`plexdb/commands/explore.py`).
    "explore",
    "publish",
    # Last, and not a step of the sweep — it wraps the whole run rather than
    # taking part in one (`plexdb/commands/schedule.py`).
    "schedule",
]


def _subcommand_order(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    raise AssertionError("parser has no subparsers action")


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    return set(_subcommand_order(parser))


def test_build_parser_discovers_the_expected_command_set() -> None:
    """Pins the discovered set so a command that stops being found by
    `pkgutil.iter_modules` fails this test rather than silently vanishing
    from `--help`."""
    assert _subcommand_names(build_parser()) == EXPECTED_COMMANDS


def test_help_lists_commands_in_the_order_they_are_run() -> None:
    """`plexdb --help` leads with the command you run first. Sorting by module
    name instead would put `enrich-tmdb-keywords` above `init`."""
    assert _subcommand_order(build_parser()) == EXPECTED_COMMAND_ORDER


def test_a_command_module_without_register_fails_loudly_naming_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = tmp_path / "broken_commands"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "no_register.py").write_text('NAME = "no-register"\nORDER = 10\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    broken_commands = importlib.import_module("broken_commands")

    sub = argparse.ArgumentParser().add_subparsers(dest="command")

    with pytest.raises(RuntimeError, match="broken_commands.no_register"):
        _register_commands(sub, package=broken_commands)


def test_a_command_module_without_order_fails_loudly_naming_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ORDER must not default the command to the end of `--help`,
    where nobody would notice it was never given a position."""
    package_dir = tmp_path / "orderless_commands"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "no_order.py").write_text(
        'NAME = "no-order"\n\n\ndef register(sub):\n    pass\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    orderless_commands = importlib.import_module("orderless_commands")

    sub = argparse.ArgumentParser().add_subparsers(dest="command")

    with pytest.raises(RuntimeError, match="orderless_commands.no_order"):
        _register_commands(sub, package=orderless_commands)


def test_every_module_registers_the_subcommand_its_name_declares() -> None:
    """`plexdb sweep` runs a step by its module's `NAME`, but the subparser is
    added by `register`. If those drift, `parse_args([name])` raises
    `SystemExit(2)` straight through the sweep — no `error:` line, no summary,
    and nothing else in the suite would notice."""
    registered = _subcommand_names(build_parser())
    declared = {module.NAME for module in iter_command_modules()}

    assert declared == registered


def test_a_command_module_without_name_fails_loudly_naming_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NAME` is what `plexdb sweep` runs a step by (ADR-0014). A module
    without one registers a subcommand the sweep cannot name, so it would go
    missing from every sweep while still appearing in `--help`."""
    package_dir = tmp_path / "nameless_commands"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "no_name.py").write_text("ORDER = 10\n\n\ndef register(sub):\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    nameless_commands = importlib.import_module("nameless_commands")

    sub = argparse.ArgumentParser().add_subparsers(dest="command")

    with pytest.raises(RuntimeError, match="nameless_commands.no_name"):
        _register_commands(sub, package=nameless_commands)


def test_init_creates_the_store_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))

    assert main(["migrate"]) == 0

    out = capfd.readouterr().out
    assert "created store" in out
    assert str(store.resolve()) in out
    assert store.exists()


def test_a_second_init_reports_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", str(tmp_path / "plexdb.db"))
    main(["migrate"])
    capfd.readouterr()

    assert main(["migrate"]) == 0
    assert "already current" in capfd.readouterr().out


def test_an_empty_store_path_is_an_error_not_a_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLEXDB_PATH", "   ")

    assert main(["migrate"]) == 1
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
        assert main(["migrate"]) == 1
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


def test_config_defaults_shared_account_ids_to_mcbridemusings_and_bboy2448(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLEXDB_SHARED_ACCOUNT_IDS", raising=False)

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.shared_account_ids == (1, 3670670)


def test_config_splits_shared_account_ids_on_commas_and_trims_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEXDB_SHARED_ACCOUNT_IDS", " 1 , 3670670,, 42 ")

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.shared_account_ids == (1, 3670670, 42)


def test_config_an_explicitly_blank_shared_account_ids_means_none_not_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEXDB_SHARED_ACCOUNT_IDS", "")

    config = Config.from_env(env_file=Path("/nonexistent/.env"))

    assert config.shared_account_ids == ()


def test_config_rejects_a_non_numeric_shared_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEXDB_SHARED_ACCOUNT_IDS", "1,bboy2448")

    with pytest.raises(ConfigError):
        Config.from_env(env_file=Path("/nonexistent/.env"))


def test_publish_writes_a_snapshot_and_says_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))
    main(["migrate"])
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
    main(["migrate"])
    capsys.readouterr()

    assert main(["publish"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "PLEXDB_SNAPSHOT_PATH" in err


class _FakeAccountsSource:
    """Stands in for `LivePlexClient` in a `latent-users` CLI test — same
    `(base_url, token)` construction shape, backed by a fixed `/accounts`
    listing. No network. `accounts` is a class attribute so a test can set
    it right before calling `main`, without needing a per-test subclass."""

    accounts_response: list[dict[str, Any]] = []

    def __init__(self, base_url: str, token: str) -> None:
        assert base_url, "latent-users must pass the configured PLEX_URL through"
        assert token, "latent-users must pass the configured PLEX_TOKEN through"

    def accounts(self) -> list[dict[str, Any]]:
        return self.accounts_response


def _configure_latent_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, accounts: list[dict[str, Any]] | None = None
) -> Path:
    """Point the CLI at a fresh store under `tmp_path` with Plex credentials
    configured and `_FakeAccountsSource` standing in for `LivePlexClient`.
    Returns the store path."""
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEX_URL", "http://plex.example:32400")
    monkeypatch.setenv("PLEX_TOKEN", "test-token")
    init_store(store)
    monkeypatch.setattr(_FakeAccountsSource, "accounts_response", accounts or [])
    monkeypatch.setattr(latent_users_cmd, "LivePlexClient", _FakeAccountsSource)
    return store


def test_latent_users_reports_a_shared_account_with_its_structural_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A configured shared account still gets the full fingerprint-clustered
    report — issue #27 only narrowed which accounts reach this path."""
    store = _configure_latent_users(tmp_path, monkeypatch)
    monkeypatch.setenv("PLEXDB_SHARED_ACCOUNT_IDS", "7")
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt0096734', 'movie', ?)",
            ("The 'Burbs",),
        )
        # LATENT_USER_FLOOR plays, so this cluster clears the floor (issue
        # #28) and is still reported as a latent user, not folded into the
        # unattributed bucket.
        for i in range(LATENT_USER_FLOOR):
            conn.execute(
                "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, "
                "viewed_at) VALUES (?, 'imdb:tt0096734', 7, 'device-alpha-001', 1700000000)",
                (f"h{i}",),
            )
        conn.commit()
    capsys.readouterr()

    assert main(["latent-users"]) == 0

    out = capsys.readouterr().out
    assert "== account 7 ==" in out
    assert (
        f"structural baseline: {LATENT_USER_FLOOR} play(s), 1 distinct client_identifier(s), "
        f"1 cluster(s) found, 1 at or above the {LATENT_USER_FLOOR}-play floor" in out
    )
    assert "cluster 1 [client_identifier='device-alpha-001']" in out


def test_latent_users_reports_a_non_shared_account_as_one_user_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Account 7 is not in the default `PLEXDB_SHARED_ACCOUNT_IDS`, so it
    gets the personal report: no structural baseline, no cluster label."""
    store = _configure_latent_users(tmp_path, monkeypatch)
    monkeypatch.delenv("PLEXDB_SHARED_ACCOUNT_IDS", raising=False)
    with open_store(store) as conn:
        conn.execute(
            "INSERT INTO items (item_id, type, title) VALUES ('imdb:tt0096734', 'movie', ?)",
            ("The 'Burbs",),
        )
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, "
            "viewed_at) VALUES ('h1', 'imdb:tt0096734', 7, 'device-alpha-001', 1700000000)"
        )
        conn.commit()
    capsys.readouterr()

    assert main(["latent-users"]) == 0

    out = capsys.readouterr().out
    assert "== account 7 ==" in out
    assert "plays: 1" in out
    assert "structural baseline" not in out
    assert "cluster" not in out
    assert "device-alpha-001" not in out


def test_latent_users_labels_accounts_by_the_name_plex_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _configure_latent_users(tmp_path, monkeypatch, accounts=[{"id": 7, "name": "Madi"}])
    monkeypatch.delenv("PLEXDB_SHARED_ACCOUNT_IDS", raising=False)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, viewed_at) "
            "VALUES ('h1', 'imdb:tt1', 7, 1700000000)"
        )
        conn.commit()
    capsys.readouterr()

    assert main(["latent-users"]) == 0

    out = capsys.readouterr().out
    assert "== account 7 (Madi) ==" in out


def test_latent_users_never_writes_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guards the read-only claim end to end: a store with zero plays is
    untouched by running the command, not merely "reported as empty"."""
    store = _configure_latent_users(tmp_path, monkeypatch)
    before = store.stat().st_mtime_ns
    capsys.readouterr()

    assert main(["latent-users"]) == 0

    out = capsys.readouterr().out
    assert out == "\n"
    assert store.stat().st_mtime_ns == before


def test_latent_users_can_be_scoped_to_one_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _configure_latent_users(tmp_path, monkeypatch)
    with open_store(store) as conn:
        conn.execute("INSERT INTO items (item_id, type, title) VALUES ('imdb:tt1', 'movie', 'A')")
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, "
            "viewed_at) VALUES ('h1', 'imdb:tt1', 1, 'd1', 1700000000)"
        )
        conn.execute(
            "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, "
            "viewed_at) VALUES ('h2', 'imdb:tt1', 2, 'd2', 1700000000)"
        )
        conn.commit()
    capsys.readouterr()

    assert main(["latent-users", "--account", "2"]) == 0

    out = capsys.readouterr().out
    assert "account 2" in out
    assert "account 1" not in out


def test_latent_users_without_plex_credentials_configured_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEX_URL", "")
    monkeypatch.setenv("PLEX_TOKEN", "")
    init_store(store)
    capsys.readouterr()

    assert main(["latent-users"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "PLEX_URL" in err
    assert "Traceback" not in err
