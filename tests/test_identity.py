"""`item_id` derivation, driven entirely from the shared fixture.

The cases live in `tests/fixtures/entry_id.json` rather than in this file,
because `etv-station` runs the same ones against its Rust implementation. A case
added here that is not in the fixture proves nothing about the other side.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from plexdb.identity import PRIORITY, canonical_path, derive_item_id, fnv1a_64

FIXTURE = Path(__file__).parent / "fixtures" / "entry_id.json"

#: SHA-256 of the shared fixture. `etv-station` pins the same value, so editing
#: one copy of the file without the other turns BOTH suites red — which is the
#: whole mechanism (ADR-0006). Updating this constant alone defeats it: change
#: the fixture in both repos, then update the hash in both.
FIXTURE_SHA256 = "73fd792cb8e84a88fd577f4d7c0966db1c0240b88db7c939f54c9374fdbb4daf"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _cases(section: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = _fixture()[section]
    return cases


def _ids(section: str) -> list[str]:
    return [case["name"] for case in _cases(section)]


def test_the_shared_fixture_has_not_drifted() -> None:
    """The tripwire.

    If this fails, the fixture changed. That is fine — but it means
    `etv-station`'s copy must change identically and both recorded hashes must
    be updated, or the two implementations have quietly stopped agreeing about
    what a title is called.
    """
    actual = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()

    assert actual == FIXTURE_SHA256, (
        "tests/fixtures/entry_id.json changed.\n"
        "This file is shared with etv-station "
        "(crates/etv-station/tests/fixtures/entry_id.json).\n"
        "Copy the new file there and update the recorded SHA-256 in BOTH repos, "
        f"or the two item_id implementations will silently disagree.\nNew hash: {actual}"
    )


@pytest.mark.parametrize("case", _cases("entry_id_cases"), ids=_ids("entry_id_cases"))
def test_item_id_matches_the_fixture(case: dict[str, Any]) -> None:
    pairs = [(ns, value) for ns, value in case["external_ids"]]

    assert derive_item_id(pairs, case["canonical_path"]) == case["expect"]


@pytest.mark.parametrize("case", _cases("canonical_path_cases"), ids=_ids("canonical_path_cases"))
def test_canonical_path_matches_the_fixture(case: dict[str, Any]) -> None:
    assert canonical_path(case["raw"], case["source_roots"]) == case["expect"]


@pytest.mark.parametrize("case", _cases("end_to_end_cases"), ids=_ids("end_to_end_cases"))
def test_raw_path_through_to_an_id_matches_the_fixture(case: dict[str, Any]) -> None:
    """Canonicalise then derive, which is what a library walk actually does."""
    canonical = canonical_path(case["raw"], case["source_roots"])
    pairs = [(ns, value) for ns, value in case["external_ids"]]

    assert derive_item_id(pairs, canonical) == case["expect"]


def test_the_two_mount_cases_really_do_collapse() -> None:
    """Guard the fixture itself.

    The end-to-end section claims two mounts of one file produce one id. If
    somebody edits those cases so the paths no longer describe the same file,
    every assertion above still passes while the property they exist to prove
    quietly stops being tested.
    """
    by_name = {case["name"]: case for case in _cases("end_to_end_cases")}
    mac = by_name["guid-less file seen through the mac mount"]
    linux = by_name["the same guid-less file seen through the linux mount — same id"]

    assert mac["raw"] != linux["raw"], "the two cases must use different raw paths"
    assert mac["expect"] == linux["expect"], "two mounts of one file must give one id"
    assert mac["expect"].startswith("fs:"), "this pair is about the path-hash fallback"


def test_fnv1a_matches_the_published_vectors() -> None:
    """The hash is a documented algorithm, not ours.

    `etv-station` implements the same one in Rust. Pinning it against published
    vectors is what makes "both sides implement FNV-1a" a checkable claim rather
    than two independent guesses that happen to agree today.
    """
    assert fnv1a_64("") == 0xCBF29CE484222325
    assert fnv1a_64("a") == 0xAF63DC4C8601EC8C
    assert fnv1a_64("foobar") == 0x85944171F73967E8


def test_priority_order_is_the_rule() -> None:
    assert PRIORITY == ("imdb", "tmdb", "tvdb", "plex")


def test_every_priority_namespace_is_covered_by_a_fixture_case() -> None:
    """A namespace with no case is a rule nobody is checking."""
    winning = {case["expect"].split(":", 1)[0] for case in _cases("entry_id_cases")}

    assert set(PRIORITY) <= winning, f"no fixture case resolves to {set(PRIORITY) - winning}"
