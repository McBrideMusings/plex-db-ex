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
    LATENT_USER_FLOOR,
    MIN_IP_OCCURRENCES,
    DiscountedJoinIp,
    KeywordProfile,
    account_ids_with_plays,
    account_units,
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


def _seed_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    item_type: str = "movie",
    show_item_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO items (item_id, type, title, show_item_id) VALUES (?, ?, ?, ?)",
        (item_id, item_type, item_id, show_item_id),
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


def _seed_plays_at_floor(
    conn: sqlite3.Connection,
    *,
    history_key_prefix: str,
    item_id: str,
    count: int = LATENT_USER_FLOOR,
    account: int = ACCOUNT,
    client_identifier: str | None = None,
    ip: str | None = None,
    platform: str | None = None,
) -> None:
    """Seed `count` plays of the same item/device so a test cluster clears
    `LATENT_USER_FLOOR` and is reported as a latent user rather than folded
    into the `unattributed` bucket (issue #28). Defaults to exactly the
    floor; a cluster this module reports on the boundary is at the floor,
    never one under it — see `_split_by_floor`."""
    for i in range(count):
        _seed_play(
            conn,
            history_key=f"{history_key_prefix}-{i}",
            item_id=item_id,
            account=account,
            client_identifier=client_identifier,
            ip=ip,
            platform=platform,
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


def test_devices_sharing_a_recurring_ip_are_joined_into_one_cluster(
    store: sqlite3.Connection,
) -> None:
    """Issue #27: two devices at the same recurring, single-account IP merge
    into one cluster, and the report can name the IP that joined them."""
    assert MIN_IP_OCCURRENCES == 2, "test assumes the documented floor"
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.5"
    )

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.key_tier == "client_identifier"
    assert cluster.key_value == "device-a"  # canonical: alphabetically smallest member
    assert cluster.client_identifiers == ("device-a", "device-b")
    assert cluster.joined_by_ips == ("10.0.0.5",)
    assert cluster.play_count == 2


def test_a_device_seen_at_only_one_ip_is_not_joined_to_anything(
    store: sqlite3.Connection,
) -> None:
    """Two devices that never share an IP stay two clusters, and neither
    carries a `joined_by_ips` — joining is a bridge, not a default merge."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.6"
    )

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 2
    assert {c.key_tier for c in result.clusters} == {"client_identifier"}
    assert all(c.joined_by_ips == () for c in result.clusters)


def test_joining_is_transitive_across_two_different_ips(store: sqlite3.Connection) -> None:
    """device-a and device-b share ip1; device-b and device-c share ip2 — all
    three land in one cluster, joined by both IPs, even though device-a and
    device-c never shared an address directly."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_item(store, "item:3")
    _seed_item(store, "item:4")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h3", item_id="item:3", client_identifier="device-b", ip="10.0.0.6"
    )
    _seed_play(
        store, history_key="h4", item_id="item:4", client_identifier="device-c", ip="10.0.0.6"
    )

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.client_identifiers == ("device-a", "device-b", "device-c")
    assert cluster.joined_by_ips == ("10.0.0.5", "10.0.0.6")


def test_a_join_ip_seen_under_more_than_one_account_is_discounted(
    store: sqlite3.Connection,
) -> None:
    """An IP that also shows up under a different Plex account is shared
    infrastructure, not household evidence for this account — it must not
    join anything, and the report says it was discounted."""
    OTHER_ACCOUNT = 2
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_item(store, "item:3")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.5"
    )
    _seed_play(
        store,
        history_key="h3",
        item_id="item:3",
        account=OTHER_ACCOUNT,
        client_identifier="device-z",
        ip="10.0.0.5",
    )

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 2
    assert all(c.joined_by_ips == () for c in result.clusters)
    assert result.discounted_join_ips == (
        DiscountedJoinIp(
            ip="10.0.0.5",
            reason="seen under 2 different accounts, so treated as shared infrastructure "
            "rather than a household",
        ),
    )


def test_join_merge_is_deterministic_regardless_of_union_order(store: sqlite3.Connection) -> None:
    """The canonical group key is the alphabetically smallest member, not
    whichever id a union-find pass happens to leave as its root — so this is
    stable across repeated runs, matching #10's determinism guarantee."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-z", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-a", ip="10.0.0.5"
    )

    first = cluster_account_plays(store, ACCOUNT)
    second = cluster_account_plays(store, ACCOUNT)

    assert first == second
    assert first.clusters[0].key_value == "device-a"


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


def test_episode_plays_roll_up_to_one_unit_keyed_by_show_item_id(
    store: sqlite3.Connection,
) -> None:
    """Issue #25's decided rule: an episode's unit is its show, and depth is
    the number of episode plays that rolled into it."""
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "ep:office:s1e1", item_type="episode", show_item_id="show:office")
    _seed_item(store, "ep:office:s1e2", item_type="episode", show_item_id="show:office")
    _seed_play(store, history_key="h1", item_id="ep:office:s1e1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="ep:office:s1e2", client_identifier="device-a")

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    # Two distinct episodes literally played...
    assert len(cluster.item_ids) == 2
    # ...but one unit (the show), with depth 2.
    assert cluster.units == (("show:office", 2),)


def test_a_movie_is_its_own_unit_with_depth_from_rewatches(store: sqlite3.Connection) -> None:
    _seed_item(store, "movie:1")
    _seed_play(store, history_key="h1", item_id="movie:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="movie:1", client_identifier="device-a")

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    assert result.clusters[0].units == (("movie:1", 2),)


def test_a_mixed_cluster_units_shows_and_movies_separately(store: sqlite3.Connection) -> None:
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "ep:office:s1e1", item_type="episode", show_item_id="show:office")
    _seed_item(store, "movie:1")
    _seed_play(store, history_key="h1", item_id="ep:office:s1e1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="movie:1", client_identifier="device-a")

    result = cluster_account_plays(store, ACCOUNT)

    assert len(result.clusters) == 1
    assert result.clusters[0].units == (("movie:1", 1), ("show:office", 1))


# --- build_keyword_profile / profile_overlap ----------------------------


def test_keyword_profile_counts_each_unit_at_most_once_per_keyword(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    _seed_keyword(store, "item:1", "gotham city")
    _seed_keyword(store, "item:2", "superhero")

    profile = build_keyword_profile(store, [("item:1", 1), ("item:2", 1)])

    assert profile.units_total == 2
    assert profile.units_with_coverage == 2
    assert dict(profile.top_keywords) == {"superhero": 2, "gotham city": 1}
    # Ranked by unit-count desc, then alphabetically.
    assert profile.top_keywords[0] == ("superhero", 2)


def test_keyword_profile_reports_zero_coverage_rather_than_an_empty_profile(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    # No enrichment row at all for item:1 — never enriched.

    profile = build_keyword_profile(store, [("item:1", 1)])

    assert profile.units_total == 1
    assert profile.units_with_coverage == 0
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

    profile = build_keyword_profile(store, [("item:1", 1)])

    assert profile.units_with_coverage == 0
    assert profile.has_coverage is False


def test_keyword_profile_of_no_units_is_empty_not_an_error(store: sqlite3.Connection) -> None:
    profile = build_keyword_profile(store, [])

    assert profile == KeywordProfile(
        units_total=0, units_with_coverage=0, total_depth=0, covered_depth=0, top_keywords=()
    )


def test_keyword_profile_counts_a_show_once_regardless_of_episode_depth(
    store: sqlite3.Connection,
) -> None:
    """The decided rule (issue #25 comment): a show contributes its keyword
    set once per cluster no matter how many episodes were played — a depth
    of 10 must not make the show's keywords outweigh a movie's."""
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "movie:1")
    _seed_keyword(store, "show:office", "workplace comedy")
    _seed_keyword(store, "movie:1", "heist")

    # The show has depth 10 (ten episode plays); the movie has depth 1.
    profile = build_keyword_profile(store, [("show:office", 10), ("movie:1", 1)])

    assert profile.units_total == 2
    assert profile.units_with_coverage == 2
    assert profile.total_depth == 11
    assert profile.covered_depth == 11
    # Each unit contributes its keyword(s) exactly once, so both keywords
    # tie at a count of 1 despite the tenfold difference in depth.
    assert dict(profile.top_keywords) == {"workplace comedy": 1, "heist": 1}


def test_keyword_profile_covered_depth_reflects_only_covered_units(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "show:uncovered", item_type="show")
    _seed_keyword(store, "show:office", "workplace comedy")
    # show:uncovered never enriched.

    profile = build_keyword_profile(store, [("show:office", 10), ("show:uncovered", 5)])

    assert profile.total_depth == 15
    assert profile.covered_depth == 10


def _profile(*keywords: str) -> KeywordProfile:
    """A one-unit, one-play-deep profile carrying `keywords`, each at a count
    of 1. `profile_overlap` reads only `top_keywords` and `has_coverage`, so
    the coverage fields follow from whether any keyword was given: no
    keywords means the unit carries no `tmdb_keywords` at all."""
    covered = 1 if keywords else 0
    return KeywordProfile(
        units_total=1,
        units_with_coverage=covered,
        total_depth=1,
        covered_depth=covered,
        top_keywords=tuple((keyword, 1) for keyword in keywords),
    )


def test_profile_overlap_of_identical_profiles_is_one(store: sqlite3.Connection) -> None:
    assert profile_overlap(_profile("horror"), _profile("horror")) == 1.0


def test_profile_overlap_of_disjoint_profiles_is_zero(store: sqlite3.Connection) -> None:
    assert profile_overlap(_profile("horror"), _profile("romance")) == 0.0


def test_profile_overlap_is_none_when_either_side_has_no_coverage(
    store: sqlite3.Connection,
) -> None:
    covered = _profile("horror")
    uncovered = _profile()

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

    assert render_report(store, shared_account_ids=[ACCOUNT]) == render_report(
        store, shared_account_ids=[ACCOUNT]
    )


def test_render_report_states_coverage_for_a_cluster_with_none(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_plays_at_floor(
        store, history_key_prefix="h", item_id="item:1", client_identifier="device-a"
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "no enrichment coverage" in report
    assert "0/1 unit(s) carry tmdb_keywords" in report


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

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "203.0.113.9" in report
    assert "transient" in report.lower()


def test_render_report_names_the_ip_that_joined_a_cluster(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    # 10 plays each, all sharing the joining IP, so the merged cluster's
    # total (20) clears LATENT_USER_FLOOR and is reported as a latent user.
    _seed_plays_at_floor(
        store,
        history_key_prefix="h1",
        item_id="item:1",
        count=10,
        client_identifier="device-a",
        ip="10.0.0.5",
    )
    _seed_plays_at_floor(
        store,
        history_key_prefix="h2",
        item_id="item:2",
        count=10,
        client_identifier="device-b",
        ip="10.0.0.5",
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "joined by ip(s): 10.0.0.5" in report
    assert f"1 cluster(s) found, 1 at or above the {LATENT_USER_FLOOR}-play floor" in report


def test_render_report_names_a_discounted_join_ip_and_why(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_item(store, "item:3")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.5"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.5"
    )
    _seed_play(
        store,
        history_key="h3",
        item_id="item:3",
        account=2,
        client_identifier="device-z",
        ip="10.0.0.5",
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "IP(s) discounted from joining devices" in report
    assert "10.0.0.5: seen under 2 different accounts" in report


def test_render_report_says_na_with_fewer_than_two_clusters(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "pairwise overlap: n/a (fewer than two clusters)" in report


def test_render_report_computes_pairwise_overlap_between_clusters(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    _seed_keyword(store, "item:2", "superhero")
    _seed_plays_at_floor(
        store, history_key_prefix="h1", item_id="item:1", client_identifier="device-a"
    )
    _seed_plays_at_floor(
        store, history_key_prefix="h2", item_id="item:2", client_identifier="device-b"
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "cluster 1 vs cluster 2: 1.00" in report


def test_render_report_reports_na_overlap_when_one_cluster_has_no_coverage(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    # item:2 never enriched.
    _seed_plays_at_floor(
        store, history_key_prefix="h1", item_id="item:1", client_identifier="device-a"
    )
    _seed_plays_at_floor(
        store, history_key_prefix="h2", item_id="item:2", client_identifier="device-b"
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "no enrichment coverage on one or both" in report


def test_render_report_covers_an_episode_only_cluster_via_its_shows_keywords(
    store: sqlite3.Connection,
) -> None:
    """Before issue #25's fix this cluster read 'no enrichment coverage' —
    the episodes it played never carry `tmdb_keywords` themselves. After
    the rollup to `show_item_id`, the show's keywords cover it."""
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "ep:office:s1e1", item_type="episode", show_item_id="show:office")
    _seed_item(store, "ep:office:s1e2", item_type="episode", show_item_id="show:office")
    _seed_keyword(store, "show:office", "workplace comedy")
    # 10 plays of each episode — 20 plays total, one unit — clears the floor.
    _seed_plays_at_floor(
        store,
        history_key_prefix="h1",
        item_id="ep:office:s1e1",
        count=10,
        client_identifier="device-a",
    )
    _seed_plays_at_floor(
        store,
        history_key_prefix="h2",
        item_id="ep:office:s1e2",
        count=10,
        client_identifier="device-a",
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "no enrichment coverage" not in report
    assert "workplace comedy" in report
    assert "1/1 unit(s) with tmdb_keywords coverage, spanning 20/20 play(s) deep" in report


def test_render_report_overlap_is_real_between_two_episode_only_clusters(
    store: sqlite3.Connection,
) -> None:
    """The issue's headline complaint: pairwise overlap between two
    all-episode clusters printed 'n/a' before the rollup. It must be a real
    number once both clusters' shows carry keywords."""
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "ep:office:s1e1", item_type="episode", show_item_id="show:office")
    _seed_item(store, "show:parks", item_type="show")
    _seed_item(store, "ep:parks:s1e1", item_type="episode", show_item_id="show:parks")
    _seed_keyword(store, "show:office", "workplace comedy")
    _seed_keyword(store, "show:parks", "workplace comedy")
    _seed_plays_at_floor(
        store, history_key_prefix="h1", item_id="ep:office:s1e1", client_identifier="device-a"
    )
    _seed_plays_at_floor(
        store, history_key_prefix="h2", item_id="ep:parks:s1e1", client_identifier="device-b"
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "no enrichment coverage on one or both" not in report
    assert "cluster 1 vs cluster 2: 1.00" in report


def test_render_report_scopes_to_the_requested_accounts(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", account=1, client_identifier="d1")
    _seed_play(store, history_key="h2", item_id="item:1", account=2, client_identifier="d2")

    report = render_report(store, [2], shared_account_ids=[])

    assert "account 2" in report
    assert "account 1" not in report


# --- render_report: the latent-user floor (issue #28) ---------------------


def test_render_report_does_not_list_a_sub_floor_cluster_as_a_latent_user(
    store: sqlite3.Connection,
) -> None:
    """A cluster with fewer than LATENT_USER_FLOOR plays never gets its own
    'cluster N' section — the decided acceptance criterion."""
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "cluster 1 [" not in report


def test_render_report_folds_sub_floor_clusters_into_one_unattributed_bucket(
    store: sqlite3.Connection,
) -> None:
    """Sub-floor plays are never dropped: they are named as one bucket per
    account, stating the play count and the device count it covers."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_item(store, "item:3")
    # Three separate one-play devices, none reaching the floor on its own.
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-b")
    _seed_play(store, history_key="h3", item_id="item:3", client_identifier="device-c")

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert (
        f"unattributed: 3 play(s) across 3 cluster(s) under the {LATENT_USER_FLOOR}-play "
        "floor (3 device(s))" in report
    )


def test_render_report_unattributed_bucket_carries_no_keyword_profile_or_overlap(
    store: sqlite3.Connection,
) -> None:
    """The unattributed bucket is not a latent user: it must never gain a
    keyword-profile line or enter the pairwise-overlap section, even when
    the folded clusters' items carry tmdb_keywords."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "superhero")
    _seed_keyword(store, "item:2", "superhero")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="item:2", client_identifier="device-b")

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert "unattributed" in report
    assert "superhero" not in report
    # No per-cluster keyword-profile line was rendered for either folded
    # cluster (the fixed "no keyword profile" phrase inside the bucket
    # summary line itself is expected and is not this).
    assert "unit(s) with tmdb_keywords" not in report
    assert "unit(s) carry tmdb_keywords" not in report
    assert "pairwise overlap: n/a (fewer than two clusters)" in report


def test_render_report_states_both_cluster_counts_in_the_structural_baseline(
    store: sqlite3.Connection,
) -> None:
    """The gap between clusters found and clusters reported as latent users
    must be visible, not implied — one below the floor, one at it."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_plays_at_floor(
        store, history_key_prefix="h2", item_id="item:2", client_identifier="device-b"
    )

    report = render_report(store, shared_account_ids=[ACCOUNT])

    assert f"2 cluster(s) found, 1 at or above the {LATENT_USER_FLOOR}-play floor" in report


def test_render_report_of_an_account_entirely_under_the_floor_reports_the_bucket_not_nothing(
    store: sqlite3.Connection,
) -> None:
    """An account whose every cluster is sub-floor still prints a real
    report — the header, baseline, and unattributed bucket — never an empty
    section, per the decided acceptance criterion."""
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store, [ACCOUNT], shared_account_ids=[ACCOUNT])

    assert "== account 1 ==" in report
    assert "structural baseline" in report
    assert "unattributed: 1 play(s) across 1 cluster(s)" in report
    assert "cluster 1 [" not in report


def test_render_report_floor_is_twenty(store: sqlite3.Connection) -> None:
    """The decided floor value (issue #28) — a bare literal elsewhere in
    this test file assumes this, so pin it explicitly."""
    assert LATENT_USER_FLOOR == 20


def test_render_report_is_stable_across_repeated_calls_with_sub_floor_clusters(
    store: sqlite3.Connection,
) -> None:
    """Running the report twice over unchanged plays produces the same
    output, including the unattributed bucket line."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")
    _seed_plays_at_floor(
        store, history_key_prefix="h2", item_id="item:2", client_identifier="device-b"
    )

    first = render_report(store, shared_account_ids=[ACCOUNT])
    second = render_report(store, shared_account_ids=[ACCOUNT])

    assert first == second


# --- account_units --------------------------------------------------------


def test_account_units_ignores_the_fingerprint_chain_entirely(store: sqlite3.Connection) -> None:
    """Unlike `cluster_account_plays`, `account_units` never partitions by
    device — the same show watched from two different client_identifiers
    is still one unit with depth 2, because a personal account's profile is
    built over every play it ever recorded, not per device."""
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "ep:office:s1e1", item_type="episode", show_item_id="show:office")
    _seed_item(store, "ep:office:s1e2", item_type="episode", show_item_id="show:office")
    _seed_play(store, history_key="h1", item_id="ep:office:s1e1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="ep:office:s1e2", client_identifier="device-b")

    assert account_units(store, ACCOUNT) == (("show:office", 2),)


def test_account_units_of_no_plays_is_empty(store: sqlite3.Connection) -> None:
    assert account_units(store, ACCOUNT) == ()


# --- render_report: personal vs. shared accounts (issue #27) -------------


def test_render_report_reports_a_personal_account_as_one_user_with_no_device_or_cluster_numbers(
    store: sqlite3.Connection,
) -> None:
    """The decided acceptance criterion: an account not in
    `shared_account_ids` prints name, play count, and one keyword profile —
    nothing that implies clustering happened."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_keyword(store, "item:1", "heist")
    _seed_play(
        store, history_key="h1", item_id="item:1", client_identifier="device-a", ip="10.0.0.1"
    )
    _seed_play(
        store, history_key="h2", item_id="item:2", client_identifier="device-b", ip="10.0.0.2"
    )

    report = render_report(store, [ACCOUNT], shared_account_ids=[])

    assert "plays: 2" in report
    assert "heist" in report
    assert "structural baseline" not in report
    assert "cluster" not in report
    assert "distinct client_identifier" not in report
    assert "pairwise" not in report
    assert "device-a" not in report
    assert "device-b" not in report


def test_render_report_personal_account_profile_covers_every_play_not_one_device(
    store: sqlite3.Connection,
) -> None:
    """A personal account's one keyword profile is built over the whole
    account, so a show watched from two devices still counts as one
    covered unit at depth 2 — not split into two partial profiles."""
    _seed_item(store, "show:office", item_type="show")
    _seed_item(store, "ep:office:s1e1", item_type="episode", show_item_id="show:office")
    _seed_item(store, "ep:office:s1e2", item_type="episode", show_item_id="show:office")
    _seed_keyword(store, "show:office", "workplace comedy")
    _seed_play(store, history_key="h1", item_id="ep:office:s1e1", client_identifier="device-a")
    _seed_play(store, history_key="h2", item_id="ep:office:s1e2", client_identifier="device-b")

    report = render_report(store, [ACCOUNT], shared_account_ids=[])

    assert "1/1 unit(s) with tmdb_keywords coverage, spanning 2/2 play(s) deep" in report
    assert "workplace comedy" in report


def test_render_report_personal_account_with_no_coverage_says_so(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store, [ACCOUNT], shared_account_ids=[])

    assert "keyword profile: no enrichment coverage (0/1 unit(s) carry tmdb_keywords)" in report


def test_render_report_shared_account_keeps_the_full_structural_report(
    store: sqlite3.Connection,
) -> None:
    """A configured shared account's report is unchanged by issue #27: the
    structural baseline, per-cluster breakdown, and pairwise overlap all
    still print — for clusters at or above the floor (issue #28)."""
    _seed_item(store, "item:1")
    _seed_item(store, "item:2")
    _seed_plays_at_floor(
        store, history_key_prefix="h1", item_id="item:1", client_identifier="device-a"
    )
    _seed_plays_at_floor(
        store, history_key_prefix="h2", item_id="item:2", client_identifier="device-b"
    )

    report = render_report(store, [ACCOUNT], shared_account_ids=[ACCOUNT])

    assert (
        "structural baseline: 40 play(s), 2 distinct client_identifier(s), "
        "2 cluster(s) found, 2 at or above the 20-play floor" in report
    )
    assert "cluster 1 [client_identifier='device-a']" in report
    assert "cluster 2 [client_identifier='device-b']" in report
    assert "pairwise" in report


def test_render_report_labels_an_account_by_name_when_given(store: sqlite3.Connection) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store, [ACCOUNT], shared_account_ids=[], account_names={ACCOUNT: "Madi"})

    assert "== account 1 (Madi) ==" in report


def test_render_report_prints_the_bare_id_when_no_name_is_known(
    store: sqlite3.Connection,
) -> None:
    _seed_item(store, "item:1")
    _seed_play(store, history_key="h1", item_id="item:1", client_identifier="device-a")

    report = render_report(store, [ACCOUNT], shared_account_ids=[])

    assert "== account 1 ==" in report
