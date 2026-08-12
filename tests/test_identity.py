"""`item_id` derivation, driven entirely from the published fixture.

The cases live in `tests/fixtures/item_id.json` rather than in this file because
that file is the rule's specification, not this module's private test data
(ADR-0006). A case written here instead of there is coverage nothing else can
read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from plexdb.identity import PRIORITY, canonical_path, derive_item_id, fnv1a_64

FIXTURE = Path(__file__).parent / "fixtures" / "item_id.json"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _cases(section: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = _fixture()[section]
    return cases


def _ids(section: str) -> list[str]:
    return [case["name"] for case in _cases(section)]


@pytest.mark.parametrize("case", _cases("item_id_cases"), ids=_ids("item_id_cases"))
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


def test_the_two_independent_stores_really_are_independent() -> None:
    """Guard the fixture's headline claim.

    Two stores agreeing while each is told about both mounts is a weaker claim
    than the one #24 is about. If somebody adds the other side's root to either
    case's `source_roots`, both assertions above still pass and the deployment
    shape stops being tested.
    """
    by_name = {case["name"]: case for case in _cases("end_to_end_cases")}
    side_a = by_name[
        "two independent stores, each stripping only its own single configured root, "
        "still agree — side A's own view of the file"
    ]
    side_b = by_name[
        "two independent stores, each stripping only its own single configured root, "
        "still agree — side B's own, differently-mounted view of the same file"
    ]

    assert len(side_a["source_roots"]) == 1, "side A must know only its own mount"
    assert len(side_b["source_roots"]) == 1, "side B must know only its own mount"
    assert not set(side_a["source_roots"]) & set(side_b["source_roots"]), (
        "neither store may be configured with the other's mount"
    )
    assert side_a["raw"] != side_b["raw"], "the two sides must reach the file by different paths"
    assert side_a["expect"] == side_b["expect"], "two views of one file must give one id"
    assert side_a["expect"].startswith("fs:"), "this pair is about the path-hash fallback"


def test_fnv1a_matches_the_published_vectors() -> None:
    """The hash is a documented algorithm, not ours.

    Pinning it against FNV's own published vectors is what makes "this is
    FNV-1a" a checkable claim rather than whatever this file happens to compute
    today — which matters because the `fs:` ids in the store are only
    reproducible by something implementing the real thing.
    """
    assert fnv1a_64("") == 0xCBF29CE484222325
    assert fnv1a_64("a") == 0xAF63DC4C8601EC8C
    assert fnv1a_64("foobar") == 0x85944171F73967E8


def test_priority_order_is_the_rule() -> None:
    assert PRIORITY == ("imdb", "tmdb", "tvdb", "plex")


def test_every_priority_namespace_is_covered_by_a_fixture_case() -> None:
    """A namespace with no case is a rule nobody is checking."""
    winning = {case["expect"].split(":", 1)[0] for case in _cases("item_id_cases")}

    assert set(PRIORITY) <= winning, f"no fixture case resolves to {set(PRIORITY) - winning}"
