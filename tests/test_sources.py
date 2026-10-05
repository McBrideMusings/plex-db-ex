"""The Gated Source contract: what a source declares, and what it no longer has
to write out itself."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from plexdb.commands import enrich_anilist as enrich_anilist_cmd
from plexdb.commands import enrich_letterboxd as enrich_letterboxd_cmd
from plexdb.commands import enrich_mdblist_ratings as enrich_mdblist_ratings_cmd
from plexdb.commands import enrich_tmdb_edges as enrich_tmdb_edges_cmd
from plexdb.commands import enrich_tmdb_keywords as enrich_tmdb_keywords_cmd
from plexdb.commands import enrich_wikidata as enrich_wikidata_cmd
from plexdb.commands import harvest_mdblist as harvest_mdblist_cmd
from plexdb.errors import ConfigError
from plexdb.sources import GatedSource
from plexdb.staleness import DEFAULT_STALE_DAYS

# Every Gated Source in the package, with the variable each one derives.
SOURCES = [
    (enrich_tmdb_keywords_cmd.SOURCE, "TMDB_KEYWORDS_STALE_DAYS"),
    (enrich_tmdb_edges_cmd.SOURCE, "TMDB_EDGES_STALE_DAYS"),
    (harvest_mdblist_cmd.SOURCE, "MDBLIST_STALE_DAYS"),
    (enrich_wikidata_cmd.SOURCE, "WIKIDATA_STALE_DAYS"),
    (enrich_anilist_cmd.SOURCE, "ANILIST_STALE_DAYS"),
    (enrich_letterboxd_cmd.SOURCE, "LETTERBOXD_STALE_DAYS"),
    (enrich_mdblist_ratings_cmd.SOURCE, "MDBLIST_RATINGS_STALE_DAYS"),
]


def _a_source(**overrides: Any) -> GatedSource:
    defaults: dict[str, Any] = {
        "name": "example",
        "unit": "title",
        "credential": "EXAMPLE_API_KEY",
        "credential_purpose": "fetch examples",
        "make_client": lambda key: key,
        "refresh": lambda conn, client, stale_days: None,
        "wipe": lambda conn: [],
        "report": lambda stats: [],
    }
    return GatedSource(**(defaults | overrides))


@pytest.mark.parametrize(("source", "expected"), SOURCES, ids=lambda v: getattr(v, "name", v))
def test_each_source_derives_its_own_staleness_variable(source: GatedSource, expected: str) -> None:
    """The three variables that used to be hand-written in `config.py` now fall
    out of each source's `name`, so adding a source adds no setting there."""
    assert source.stale_days_var == expected


def test_the_windows_stay_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapsing to one shared knob was the thing not to do: `config.py` said
    the separate knobs were deliberate so edges and keywords can be dialed
    apart. Deriving the name keeps that while removing the duplication."""
    monkeypatch.setenv("TMDB_KEYWORDS_STALE_DAYS", "10")
    monkeypatch.setenv("TMDB_EDGES_STALE_DAYS", "90")

    assert enrich_tmdb_keywords_cmd.SOURCE.resolve_stale_days(None) == 10
    assert enrich_tmdb_edges_cmd.SOURCE.resolve_stale_days(None) == 90


def test_the_flag_beats_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_STALE_DAYS", "10")

    assert _a_source().resolve_stale_days(3) == 3


def test_an_unset_variable_falls_back_to_the_one_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXAMPLE_STALE_DAYS", raising=False)

    assert _a_source().resolve_stale_days(None) == DEFAULT_STALE_DAYS


def test_an_empty_variable_means_the_default_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `.env` carries an empty value as a placeholder. Reading that as zero
    would make every row stale and re-fetch the whole library against a rate
    limit."""
    monkeypatch.setenv("EXAMPLE_STALE_DAYS", "   ")

    assert _a_source().resolve_stale_days(None) == DEFAULT_STALE_DAYS


def test_a_non_numeric_variable_is_rejected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_STALE_DAYS", "soon")

    with pytest.raises(ConfigError, match="EXAMPLE_STALE_DAYS"):
        _a_source().resolve_stale_days(None)


def test_a_missing_credential_is_rejected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set, not deleted: `Config.from_env` calls `load_dotenv(override=False)`,
    which fills a variable that is *absent*, so deleting would let the repo's
    own `.env` supply a real key and mask the case under test."""
    monkeypatch.setenv("EXAMPLE_API_KEY", "")

    with pytest.raises(ConfigError, match="EXAMPLE_API_KEY must be set in .env to fetch examples"):
        _a_source().run(_args())


def _args(stale_days: int | None = None, rewipe: bool = False) -> Any:
    class _Namespace:
        pass

    args = _Namespace()
    args.stale_days = stale_days  # type: ignore[attr-defined]
    args.rewipe = rewipe  # type: ignore[attr-defined]
    return args


def test_the_client_is_built_from_the_credential_and_the_window_reaches_refresh(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass through `run` proving the four things it threads together:
    the credential reaches the client, the window reaches refresh, the store is
    opened, and the report is printed."""
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("EXAMPLE_API_KEY", "a-key")
    monkeypatch.setenv("EXAMPLE_STALE_DAYS", "7")
    from plexdb.store import init as init_store

    init_store(store)
    seen: dict[str, Any] = {}

    def refresh(conn: sqlite3.Connection, client: Any, *, stale_days: int) -> str:
        seen["client"] = client
        seen["stale_days"] = stale_days
        return "stats"

    source = _a_source(refresh=refresh, report=lambda stats: [f"reported {stats}"])

    assert source.run(_args()) == 0
    assert seen == {"client": "a-key", "stale_days": 7}


def test_rewipe_off_does_not_wipe(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("EXAMPLE_API_KEY", "a-key")
    from plexdb.store import init as init_store

    init_store(store)
    wiped: list[bool] = []

    def _wipe(conn: sqlite3.Connection) -> list[str]:
        wiped.append(True)
        return []

    source = _a_source(refresh=lambda conn, client, stale_days: None, wipe=_wipe)

    source.run(_args(rewipe=False))
    assert wiped == []

    source.run(_args(rewipe=True))
    assert wiped == [True]


def test_a_source_declared_without_a_credential_runs_with_none_set(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wikidata needs no key. A keyless source builds its client from nothing
    and never looks for a credential variable."""
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("EXAMPLE_API_KEY", "")
    from plexdb.store import init as init_store

    init_store(store)
    seen: dict[str, Any] = {}

    def refresh(conn: sqlite3.Connection, client: Any, *, stale_days: int) -> None:
        seen["client"] = client

    source = _a_source(
        credential=None,
        credential_purpose=None,
        make_client=lambda: "keyless",
        refresh=refresh,
    )

    assert source.run(_args()) == 0
    assert seen == {"client": "keyless"}


def test_a_capped_source_resolves_its_limit_flag_then_variable_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _a_source(default_limit=100)
    assert source.limit_var == "EXAMPLE_MAX_TITLES"

    monkeypatch.setenv("EXAMPLE_MAX_TITLES", "")
    assert source.resolve_limit(None) == 100
    monkeypatch.setenv("EXAMPLE_MAX_TITLES", "20")
    assert source.resolve_limit(None) == 20
    assert source.resolve_limit(5) == 5
    with pytest.raises(ConfigError, match="cannot be negative"):
        source.resolve_limit(-1)


def test_a_cap_on_another_unit_names_its_variable_after_that_unit() -> None:
    """MDBList ratings gate per title but the quota counts requests."""
    assert enrich_mdblist_ratings_cmd.SOURCE.limit_var == "MDBLIST_RATINGS_MAX_REQUESTS"
    assert enrich_mdblist_ratings_cmd.SOURCE.stale_days_var == "MDBLIST_RATINGS_STALE_DAYS"


def test_a_capped_source_passes_the_limit_and_its_exit_code_decides(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "plexdb.db"
    monkeypatch.setenv("PLEXDB_PATH", str(store))
    monkeypatch.setenv("EXAMPLE_MAX_TITLES", "")
    from plexdb.store import init as init_store

    init_store(store)
    seen: dict[str, Any] = {}

    def refresh(conn: sqlite3.Connection, client: Any, *, stale_days: int, limit: int) -> int:
        seen["limit"] = limit
        return limit

    source = _a_source(
        credential=None,
        credential_purpose=None,
        make_client=lambda: None,
        refresh=refresh,
        default_limit=100,
        exit_code=lambda stats: 3 if stats == 7 else 0,
    )
    args = _args()
    args.limit = 7

    assert source.run(args) == 3
    assert seen == {"limit": 7}


def test_a_credential_without_its_purpose_is_a_declaration_error() -> None:
    with pytest.raises(ValueError, match="go together"):
        _a_source(credential_purpose=None)
