"""The CLI creates the store where configuration says, and reports what it did."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest

from plexdb.cli import _register_commands, build_parser, main
from plexdb.commands import iter_command_modules
from plexdb.config import Config, ConfigError

EXPECTED_COMMANDS = {
    "check",
    "idle",
    "steps",
    "sweep",
    "check-files",
    "explore",
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
    # Not a step of the sweep: a read-only browser view of what the steps above
    # wrote (`plexdb/commands/explore.py`).
    "explore",
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    snapshot = tmp_path / "plexdb.snapshot.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("PLEXDB_SNAPSHOT_PATH", str(snapshot))
    main(["migrate"])
    capfd.readouterr()

    assert main(["publish"]) == 0

    out = capfd.readouterr().out
    assert "published snapshot" in out
    assert str(snapshot.resolve()) in out
    assert snapshot.exists()


def test_publish_without_a_snapshot_path_configured_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.delenv("PLEXDB_SNAPSHOT_PATH", raising=False)
    main(["migrate"])
    capfd.readouterr()

    assert main(["publish"]) == 1
    err = capfd.readouterr().err
    assert err.startswith("error: ")
    assert "PLEXDB_SNAPSHOT_PATH" in err
