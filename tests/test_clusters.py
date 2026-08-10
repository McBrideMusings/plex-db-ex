"""Clustering a shared account's plays into latent users (issue #10).

Drives `plexdb.clusters` directly against a store seeded with plain SQL —
no Plex or TMDB fixture needed, since this module only ever reads `plays`
and `enrichment` rows that are already in the store by the time it runs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plexdb.clusters import (
    MIN_IP_OCCURRENCES,
    KeywordProfile,
    account_ids_with_plays,
    build_keyword_profile,
    cluster_account_plays,
    profile_overlap,
    render_report,
)
from plexdb.store import init as init_store

ACCOUNT = 1


@pytest.fixture
def store(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "plexdb.db"
    init_store(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _seed_item(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title) VALUES (?, 'movie', ?)", (item_id, item_id)
    )


def _seed_play(
    conn: sqlite3.Connection,
    *,
    history_key: str,
    item_id: str,
    account: int = ACCOUNT,
    client_identifier: str | None = None,
    ip: str | None = None,
    platform: str | None = None,
    viewed_at: int = 1700000000,
) -> None:
    conn.execute(
        "INSERT INTO plays (history_key, item_id, plex_account_id, client_identifier, "
        "platform, viewed_at, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (history_key, item_id, account, client_identifier, platform, viewed_at, ip),
    )


def _seed_keyword(conn: sqlite3.Connection, item_id: str, keyword: str) -> None:
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
        "VALUES (?, 'tmdb_keywords', 'keyword', ?, '2024-01-01T00:00:00+00:00')",
        (item_id, keyword),
    )


def _mark_enriched_with_no_keywords(conn: sqlite3.Connection, item_id: str) -> None:
    """The sentinel `enrich_tmdb.py` writes for a title fetched but carrying
    zero keywords — still "coverage" in the sense that TMDB was asked, but
    contributes nothing to a keyword profile."""
    conn.execute(
        "INSERT INTO enrichment (item_id, namespace, key, value, fetched_at) "
        "VALUES (?, 'tmdb_keywords', '_fetched', '1', '2024-01-01T00:00:00+00:00')",
        (item_id,),
    )


# --- cluster_account_plays ---------------------------------------------


def test_client_identifier_is_the_primary_cluster_key(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-b")

    result = cluster_account_plays(store, ACCOUNT)

    assert result.play_count == 2
    assert result.distinct_client_identifiers == 2
    assert len(result.clusters) == 2
    assert {c.key_tier for c in result.clusters} == {"client_identifier"}
    assert {c.key_value for c in result.clusters} == {"device-a", "device-b"}


def test_it_never_merges_two_different_client_identifiers_even_over_the_same_ip(
    store: sqlite3.Connection,
) -> None:
    """The issue names this as a caveat to encode, not solve: two people on
    one household IP still collapse into two clusters, one per device, never
    merged down to one."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.5"
    )

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 2
    assert {c.key_tier for c in result.clusters} == {"client_identifier"}


def test_ip_is_only_a_fallback_for_plays_with_no_client_identifier(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier=None, ip="10.0.0.9")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier=None, ip="10.0.0.9")

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.key_tier == "ip"
    assert cluster.key_value == "10.0.0.9"
    assert cluster.play_count == 2
    assert cluster.client_identifiers == ()


def test_a_one_off_ip_is_transient_and_falls_through_to_platform(
    store: sqlite3.Connection,
) -> None:
    assert MIN_IP_OCCURRENCES == 2, "test assumes the documented floor"
    _seed_item(store, "item:1")
    _seed_play(
        store,
        history_key="h1",
        item_id="item:1",
        client_identifier=None,
        ip="203.0.113.9",
        platform="Roku",
    )

    result = cluster_account_plays(store, ACCOUNT)

    assert result.transient_ips == ("203.0.113.9",)
    assert len(result.clusters) == 1
    assert result.clusters[0].key_tier == "platform"
    assert result.clusters[0].key_value == "Roku"


def test_a_recurring_ip_is_not_transient(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier=None, ip="203.0.113.9")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier=None, ip="203.0.113.9")

    result = cluster_account_plays(store, ACCOUNT)

    assert result.transient_ips == ()
    assert len(result.clusters) == 1
    assert result.clusters[0].key_tier == "ip"


def test_no_fingerprint_data_at_all_lands_in_the_unclustered_bucket(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1")

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    assert result.clusters[0].key_tier == "unclustered"
    assert result.clusters[0].key_value is None


def test_display_name_is_not_a_field_this_module_can_even_see(store: sqlite3.Connection) -> None:
    """`plays` carries no device-display-name column — the exclusion the
    issue demands is structural, not a filter this module has to apply."""
    columns = {row["name"] for row in store.execute("PRAGMA table_info(plays)")}
    assert "device_name" not in columns
    assert "display_name" not in columns


def test_account_ids_with_plays_lists_every_account_ascending(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", account=5, client_identifier="d")
    _seed_play(store, history_key="h2", item_id="item:1", account=2, client_identifier="d")

    assert account_ids_with_plays(store) == [2, 5]


def test_clustering_is_deterministic_across_repeated_calls(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-b")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-a")

    first = cluster_account_plays(store, ACCOUNT)
    second = cluster_account_plays(store, ACCOUNT)

    assert first == second
    # And ordering is stable: alphabetical by key_value within a tier.
    assert [c.key_value for c in first.clusters] == ["device-a", "device-b"]


# --- build_keyword_profile / profile_overlap ----------------------------


def test_keyword_profile_counts_each_item_at_most_once_per_keyword(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    _seed_keyword(store, "item:1", "gotham city")
    _seed_keyword(store, "item:2", "superhero")

    profile = build_keyword_profile(store, ["item:1", "item:2"])

    assert profile.items_total == 2
    assert profile.items_with_coverage == 2
    assert dict(profile.top_keywords) == {"superhero": 2, "gotham city": 1}
    # Ranked by item-count desc, then alphabetically.
    assert profile.top_keywords[0] == ("superhero", 2)


def test_keyword_profile_reports_zero_coverage_rather_than_an_empty_profile(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    # No enrichment row at all for item:1 — never enriched.

    profile = build_keyword_profile(store, ["item:1"])

    assert profile.items_total == 1
    assert profile.items_with_coverage == 0
    assert profile.top_keywords == ()
    assert profile.has_coverage is False


def test_keyword_profile_treats_fetched_but_empty_the_same_as_never_fetched(
    store: sqlite3.Connection,
) -> None:
    """`enrich_tmdb.py` writes a `_fetched` sentinel row for a title TMDB
    returned zero keywords for, so a re-run does not keep re-asking. That
    sentinel is not a `keyword` row, so it must not count as coverage here —
    a title with genuinely no keywords contributes nothing to a taste
    profile, the same as a title that was never enriched at all."""
    _seed_item(store, "item:1")
    _mark_enriched_with_no_keywords(store, "item:1")

    profile = build_keyword_profile(store, ["item:1"])

    assert profile.items_with_coverage == 0
    assert profile.has_coverage is False


def test_keyword_profile_of_no_items_is_empty_not_an_error(store: sqlite3.Connection) -> None:
    profile = build_keyword_profile(store, [])

    assert profile == KeywordProfile(items_total=0, items_with_coverage=0, top_keywords=())


def test_profile_overlap_of_identical_profiles_is_one(store: sqlite3.Connection) -> None:
    a = KeywordProfile(items_total=1, items_with_coverage=1, top_keywords=(("horror", 1),))
    b = KeywordProfile(items_total=1, items_with_coverage=1, top_keywords=(("horror", 1),))

    assert profile_overlap(a, b) == 1.0


def test_profile_overlap_of_disjoint_profiles_is_zero(store: sqlite3.Connection) -> None:
    a = KeywordProfile(items_total=1, items_with_coverage=1, top_keywords=(("horror", 1),))
    b = KeywordProfile(items_total=1, items_with_coverage=1, top_keywords=(("romance", 1),))

    assert profile_overlap(a, b) == 0.0


def test_profile_overlap_is_none_when_either_side_has_no_coverage(
    store: sqlite3.Connection,
) -> None:
    covered = KeywordProfile(items_total=1, items_with_coverage=1, top_keywords=(("horror", 1),))
    uncovered = KeywordProfile(items_total=1, items_with_coverage=0, top_keywords=())

    assert profile_overlap(covered, uncovered) is None
    assert profile_overlap(uncovered, covered) is None
    assert profile_overlap(uncovered, uncovered) is None


# --- render_report -------------------------------------------------------


def test_render_report_is_byte_identical_across_repeated_calls(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-b")

    assert render_report(store) == render_report(store)


def test_render_report_states_coverage_for_a_cluster_with_none(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store)

    assert "no enrichment coverage" in report
    assert "0/1 item(s) carry tmdb_keywords" in report


def test_render_report_names_transient_ips_and_why(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(
        store,
        history_key="h1",
        item_id="item:1",
        client_identifier=None,
        ip="203.0.113.9",
        platform="Roku",
    )

    report = render_report(store)

    assert "203.0.113.9" in report
    assert "transient" in report.lower()


def test_render_report_says_na_with_fewer_than_two_clusters(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store)

    assert "pairwise overlap: n/a (fewer than two clusters)" in report


def test_render_report_computes_pairwise_overlap_between_clusters(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    _seed_keyword(store, "item:2", "superhero")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-b")

    report = render_report(store)

    assert "cluster 1 vs cluster 2: 1.00" in report


def test_render_report_reports_na_overlap_when_one_cluster_has_no_coverage(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    # item:2 never enriched.
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-b")

    report = render_report(store)

    assert "no enrichment coverage on one or both" in report


def test_render_report_scopes_to_the_requested_accounts(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", account=1, client_identifier="d1")
    _seed_play(store, history_key="h2", item_id="item:1", account=2, client_identifier="d2")

    report = render_report(store, [2])

    assert "account 2" in report
    assert "account 1" not in report
