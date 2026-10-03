"""`keywords.ROLES` against the role set the frozen v14 migration CHECKs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb.keywords import ROLES, state_role
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
