"""The Rust writer keeps its own copies of two limits Python's `lists.py` still states."""

from __future__ import annotations

from pathlib import Path

from plexdb import lists

CRATE = Path(__file__).parent.parent / "crates" / "plexdb" / "src"


def test_the_rust_list_harvest_stops_at_the_failure_limit_lists_py_states() -> None:
    limit = (CRATE / "collections.rs").read_text(encoding="utf-8")
    assert f"MAX_FAILURES: i64 = {lists.MAX_FAILURES};" in limit
