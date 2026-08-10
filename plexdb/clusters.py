"""Clustering a shared Plex account's plays into latent users (issue #10).

**This module reports; it does not judge and it does not persist.** Nothing
here writes to the store — it reads `plays` and `enrichment` and returns
numbers a human reads to decide whether the clustering is good enough to
build on. That call, and any future latent-user table, are explicitly out of
scope (see the issue).

**The fingerprint tuple, applied as a fallback chain, not a merge.** Per
`docs/CONTEXT.md`'s "Fingerprint" entry: client machine id first, then IP as
a coarse household bucket, then platform as a weak tiebreak, with device
display name excluded everywhere (the schema does not even carry Plex's
device display name past `plexdb.plays`, so there is nothing to exclude in
code). A play's cluster key is its `client_identifier` when present; only a
play with none falls through to IP, and only a play with neither falls
through to platform. **This never merges two different `client_identifier`
values into one cluster** — the issue names that as a caveat to encode, not
solve: a reinstall forks an identity, and two people sharing one physical
client still collapse. Trying to bridge either with IP or platform would be
solving what the issue says to leave alone.

**Transient IPs are excluded from the fallback tier, not down-weighted in
the profile.** An IP that appears on exactly one client-identifier-less play
for an account carries no repeat signal — it reads the same as a hotel or
cellular address passing through once. `MIN_IP_OCCURRENCES` is the floor
below which an IP is treated as though it were absent, falling through to
the platform tier (or the unclustered bucket) instead of becoming its own
one-play cluster.

**The unit of taste analysis is the show, not the episode (issue #25).**
`tmdb_keywords` enrichment is only ever written for movies and shows
(`enrich_tmdb_keywords.py`) — an episode's own `item_id` never carries it.
90.5% of plays are episodes, so building a keyword profile from the played
`item_id` directly reads coverage for under 10% of plays. Every episode
carries `items.show_item_id`, so the profile is built from
`coalesce(show_item_id, item_id)` instead: a "unit". A movie has no
`show_item_id` and is its own unit under the same expression.

A unit contributes its keyword set **once** to a cluster's profile no
matter how many episodes of it were played — never once per episode. The
alternative (once per episode play) was rejected: on this server, one show
alone accounts for 5% of all plays ever recorded, and repeating its keyword
set once per episode would let that single binge outweigh every film an
account ever watched, describing the show instead of the person. Depth —
how many plays rolled into each unit — is tracked alongside (`Cluster.units`)
and reported next to coverage, but it never re-weights which keywords rank
in the profile.

**Only a configured shared account is clustered (issue #27).** Which
accounts are genuinely shared by more than one person is a fact the repo
owner stated, not something a device count or IP count can tell apart from
a person who happens to use a lot of clients — `bboy2448` shows 219
distinct devices and is shared; `Natalia` shows 73 and is one person. A
non-shared account already identifies the person by name, so device
fingerprinting there would manufacture distinctions that are not real:
`render_report` reports it as exactly one user — name, play count, one
keyword profile built over every play it ever recorded — with no device
count, no cluster count, no structural baseline, and no pairwise overlap.
The structural baseline and per-cluster breakdown this module was built
for (`cluster_account_plays`, `AccountClusters`) still run in full, but
only for an account in the caller-supplied `shared_account_ids` set; this
module never guesses that set itself; `plexdb latent_users` sources it
from `Config.shared_account_ids`.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

#: An IP must recur on at least this many plays lacking a `client_identifier`
#: before it is trusted as a household bucket for that account. Below this,
#: it is excluded from clustering entirely — see the module docstring.
MIN_IP_OCCURRENCES = 2

#: How many keywords a cluster's profile keeps, ranked by how many distinct
#: units in the cluster carry them (ties broken alphabetically, so the
#: profile — and therefore the overlap computed from it — is stable across
#: runs over unchanged data).
TOP_N_KEYWORDS = 20

#: The namespace and key `enrich_tmdb.py` writes actual keyword values under
#: (issue #4). Read-only here — this module never writes enrichment.
_TMDB_KEYWORDS_NAMESPACE = "tmdb_keywords"
_KEYWORD_KEY = "keyword"


@dataclass(frozen=True)
class Cluster:
    """One latent user within a shared account: the plays that landed on the
    same fingerprint-chain key, and what that key was.

    `key_tier` is which tier of the chain produced this cluster —
    `"client_identifier"`, `"ip"`, `"platform"`, or `"unclustered"` (none of
    the three were usable). `client_identifiers`, `ips`, and `platforms` are
    every distinct value of that field seen among the cluster's plays, sorted
    for determinism — informational for a human reading the report, never
    used as a second clustering pass.

    `item_ids` is every distinct item literally played (an episode and its
    show are different entries here) — structural, unrelated to taste
    analysis. `units` is the taste-analysis view: every distinct
    `coalesce(show_item_id, item_id)` played, each paired with its depth —
    the number of plays that rolled into it (an episode counts its own
    play; a show is the sum of its episodes' plays). Sorted by unit id for
    determinism. See the module docstring for why a unit contributes its
    keywords once regardless of depth.
    """

    key_tier: str
    key_value: str | None
    client_identifiers: tuple[str, ...]
    ips: tuple[str, ...]
    platforms: tuple[str, ...]
    item_ids: tuple[str, ...]
    units: tuple[tuple[str, int], ...]
    play_count: int


@dataclass(frozen=True)
class AccountClusters:
    """The clustering pass's output for one Plex account, plus the
    structural baseline the issue asks be reported alongside it."""

    plex_account_id: int
    play_count: int
    distinct_client_identifiers: int
    clusters: tuple[Cluster, ...]
    #: IPs seen but excluded from clustering as too thin a signal to trust —
    #: reported so a reader can see what was excluded and why, per the
    #: acceptance criteria.
    transient_ips: tuple[str, ...]


@dataclass(frozen=True)
class KeywordProfile:
    """A cluster's top-N keyword profile, built over units (see the module
    docstring), and the coverage it was built from. `units_with_coverage`
    can be zero — that is a real, reportable result (the acceptance
    criteria calls it out explicitly), not an error.

    `total_depth` and `covered_depth` are play counts, not unit counts: how
    many of the cluster's plays rolled into any unit, and how many rolled
    into a unit that actually carries `tmdb_keywords`. This is the figure
    the issue itself measures coverage by (9.3% of *plays* today, 92.4%
    after the rollup) — unit counts alone would understate how much of the
    account's actual viewing the profile now accounts for, since a single
    covered show can carry hundreds of plays.
    """

    units_total: int
    units_with_coverage: int
    total_depth: int
    covered_depth: int
    top_keywords: tuple[tuple[str, int], ...]

    @property
    def has_coverage(self) -> bool:
        return self.units_with_coverage > 0


def account_ids_with_plays(conn: sqlite3.Connection) -> list[int]:
    """Every `plex_account_id` that has ever generated a play, ascending.

    This module clusters every account it is given rather than deciding for
    itself which accounts are "shared" — the structural baseline (cluster
    count vs. distinct machine-id count) is what lets a reader see that,
    without the module having to guess a threshold.
    """
    rows = conn.execute(
        "SELECT DISTINCT plex_account_id FROM plays ORDER BY plex_account_id"
    ).fetchall()
    return [int(row["plex_account_id"]) for row in rows]


def _account_play_rows(conn: sqlite3.Connection, plex_account_id: int) -> list[sqlite3.Row]:
    """Every play row for one account, joined to its unit id
    (`coalesce(show_item_id, item_id)`) — the fetch `cluster_account_plays`
    (which additionally partitions the rows by fingerprint) and
    `account_units` (which does not) both build on, so the query and the
    INNER JOIN reasoning live in exactly one place.
    """
    # INNER JOIN, not LEFT: plays.item_id is NOT NULL REFERENCES items(item_id)
    # ON DELETE CASCADE, so every play row has a matching items row by
    # construction — there is no play whose unit this join could drop.
    return conn.execute(
        "SELECT p.item_id, p.client_identifier, p.ip, p.platform, "
        "COALESCE(i.show_item_id, p.item_id) AS unit_id "
        "FROM plays p JOIN items i ON i.item_id = p.item_id "
        "WHERE p.plex_account_id = ?",
        (plex_account_id,),
    ).fetchall()


def cluster_account_plays(conn: sqlite3.Connection, plex_account_id: int) -> AccountClusters:
    """Cluster one account's plays on the fingerprint fallback chain.

    Deterministic: the only inputs are `plays` rows for this account, and the
    output ordering never depends on `plays`' own row order (queried without
    one, then sorted explicitly below) — running this twice over an unchanged
    store produces byte-identical `Cluster` tuples.
    """
    rows = _account_play_rows(conn, plex_account_id)

    distinct_client_identifiers = {r["client_identifier"] for r in rows if r["client_identifier"]}

    # An IP only ever competes for the fallback tier on plays with no
    # client_identifier — counting occurrences elsewhere would let a
    # widely-shared household IP look "transient" just because most plays
    # carrying it also carry a client_identifier and never consult it.
    ip_occurrences: Counter[str] = Counter(
        r["ip"] for r in rows if r["client_identifier"] is None and r["ip"] is not None
    )
    eligible_ips = {ip for ip, count in ip_occurrences.items() if count >= MIN_IP_OCCURRENCES}
    transient_ips = tuple(sorted(ip for ip in ip_occurrences if ip not in eligible_ips))

    buckets: dict[tuple[str, str | None], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["client_identifier"] is not None:
            key: tuple[str, str | None] = ("client_identifier", row["client_identifier"])
        elif row["ip"] is not None and row["ip"] in eligible_ips:
            key = ("ip", row["ip"])
        elif row["platform"] is not None:
            key = ("platform", row["platform"])
        else:
            key = ("unclustered", None)
        buckets[key].append(row)

    clusters = [
        Cluster(
            key_tier=tier,
            key_value=value,
            client_identifiers=tuple(
                sorted({r["client_identifier"] for r in members if r["client_identifier"]})
            ),
            ips=tuple(sorted({r["ip"] for r in members if r["ip"]})),
            platforms=tuple(sorted({r["platform"] for r in members if r["platform"]})),
            item_ids=tuple(sorted({r["item_id"] for r in members})),
            units=tuple(sorted(Counter(r["unit_id"] for r in members).items())),
            play_count=len(members),
        )
        for (tier, value), members in buckets.items()
    ]

    tier_order = {"client_identifier": 0, "ip": 1, "platform": 2, "unclustered": 3}
    clusters.sort(key=lambda c: (tier_order[c.key_tier], c.key_value or "", -c.play_count))

    return AccountClusters(
        plex_account_id=plex_account_id,
        play_count=len(rows),
        distinct_client_identifiers=len(distinct_client_identifiers),
        clusters=tuple(clusters),
        transient_ips=transient_ips,
    )


def account_units(conn: sqlite3.Connection, plex_account_id: int) -> tuple[tuple[str, int], ...]:
    """Every unit (`coalesce(show_item_id, item_id)`) this account ever
    played, with play depth, computed over *every* play the account has —
    no fingerprint clustering at all.

    This is the personal-account report's basis for its single keyword
    profile (issue #27): once an account already identifies one named
    person, there is nothing to cluster, so the fingerprint chain
    (`cluster_account_plays` above) plays no role here. Sorted by unit id
    for the same determinism `cluster_account_plays` guarantees.
    """
    rows = _account_play_rows(conn, plex_account_id)
    return tuple(sorted(Counter(r["unit_id"] for r in rows).items()))


def build_keyword_profile(
    conn: sqlite3.Connection,
    units: Sequence[tuple[str, int]],
    *,
    top_n: int = TOP_N_KEYWORDS,
) -> KeywordProfile:
    """The top-`top_n` `tmdb_keywords` keywords across `units`, ranked by how
    many distinct units carry each one.

    `units` is a cluster's `(unit_id, depth)` pairs — `unit_id` is
    `coalesce(show_item_id, item_id)`, already deduplicated to one entry per
    show or movie regardless of episode count (see the module docstring for
    why). A unit contributes a keyword at most once no matter its depth, so
    a heavily-binged show cannot crowd out the rest of the profile any more
    than a title played twice could before this change.
    """
    if not units:
        return KeywordProfile(
            units_total=0, units_with_coverage=0, total_depth=0, covered_depth=0, top_keywords=()
        )

    depth_by_unit = dict(units)

    placeholders = ",".join("?" for _ in depth_by_unit)
    rows = conn.execute(
        "SELECT item_id, value FROM enrichment "
        f"WHERE namespace = ? AND key = ? AND item_id IN ({placeholders})",
        (_TMDB_KEYWORDS_NAMESPACE, _KEYWORD_KEY, *depth_by_unit.keys()),
    ).fetchall()

    counts: Counter[str] = Counter()
    covered_units: set[str] = set()
    for row in rows:
        counts[row["value"]] += 1
        covered_units.add(row["item_id"])

    top_keywords = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
    return KeywordProfile(
        units_total=len(depth_by_unit),
        units_with_coverage=len(covered_units),
        total_depth=sum(depth_by_unit.values()),
        covered_depth=sum(depth_by_unit[unit_id] for unit_id in covered_units),
        top_keywords=top_keywords,
    )


def profile_overlap(a: KeywordProfile, b: KeywordProfile) -> float | None:
    """Jaccard overlap of two top-N keyword sets: `|intersection| / |union|`.

    `None` when either profile has no enrichment coverage — an overlap
    against an empty set is undefined, not zero, and reporting it as `0.0`
    would read as "these two diverge completely" when the true answer is
    "nothing was measured."
    """
    if not a.has_coverage or not b.has_coverage:
        return None
    set_a = {keyword for keyword, _ in a.top_keywords}
    set_b = {keyword for keyword, _ in b.top_keywords}
    union = set_a | set_b
    if not union:
        return None
    return len(set_a & set_b) / len(union)


def _account_header(plex_account_id: int, account_names: Mapping[int, str]) -> str:
    name = account_names.get(plex_account_id)
    return (
        f"== account {plex_account_id} ({name}) ==" if name else f"== account {plex_account_id} =="
    )


def _render_keyword_profile_lines(profile: KeywordProfile, *, indent: str = "") -> list[str]:
    """The one or two lines describing a keyword profile, shared by the
    per-cluster (shared-account) and per-account (personal-account) report
    paths so the wording never drifts between them."""
    if profile.has_coverage:
        top = ", ".join(f"{keyword} ({count})" for keyword, count in profile.top_keywords)
        return [
            f"{indent}keyword profile ({profile.units_with_coverage}/{profile.units_total} "
            f"unit(s) with tmdb_keywords coverage, spanning "
            f"{profile.covered_depth}/{profile.total_depth} play(s) deep): {top}"
        ]
    return [
        f"{indent}keyword profile: no enrichment coverage "
        f"(0/{profile.units_total} unit(s) carry tmdb_keywords)"
    ]


def _render_shared_account(
    conn: sqlite3.Connection, plex_account_id: int, account_names: Mapping[int, str]
) -> str:
    """The full report for a configured shared account: structural
    baseline, per-cluster keyword profile, and pairwise overlap — unchanged
    from before issue #27, which only narrowed *which* accounts reach this
    path."""
    account = cluster_account_plays(conn, plex_account_id)
    lines = [
        _account_header(plex_account_id, account_names),
        f"structural baseline: {account.play_count} play(s), "
        f"{account.distinct_client_identifiers} distinct client_identifier(s), "
        f"{len(account.clusters)} cluster(s)",
    ]
    if account.transient_ips:
        lines.append(
            f"transient IP(s) excluded from clustering (fewer than {MIN_IP_OCCURRENCES} "
            "plays with no client_identifier, so treated as passing traffic rather than "
            f"a household): {', '.join(account.transient_ips)}"
        )

    profiles: dict[int, KeywordProfile] = {}
    for index, cluster in enumerate(account.clusters, start=1):
        profile = build_keyword_profile(conn, cluster.units)
        profiles[index] = profile

        lines.append("")
        lines.append(f"cluster {index} [{cluster.key_tier}={cluster.key_value!r}]")
        devices = ", ".join(cluster.client_identifiers) or "(none)"
        lines.append(f"  client_identifier(s): {devices}")
        lines.append(f"  ip(s) seen: {', '.join(cluster.ips) or '(none)'}")
        lines.append(f"  platform(s): {', '.join(cluster.platforms) or '(none)'}")
        lines.append(
            f"  plays: {cluster.play_count}, distinct items: {len(cluster.item_ids)}, "
            f"distinct units (a show counts once, not once per episode): {len(cluster.units)}"
        )
        lines.extend(_render_keyword_profile_lines(profile, indent="  "))

    lines.append("")
    if len(account.clusters) < 2:
        lines.append("pairwise overlap: n/a (fewer than two clusters)")
    else:
        lines.append(
            f"pairwise keyword-profile overlap (Jaccard over top-{TOP_N_KEYWORDS} keywords):"
        )
        for i, j in combinations(range(1, len(account.clusters) + 1), 2):
            overlap = profile_overlap(profiles[i], profiles[j])
            if overlap is None:
                lines.append(
                    f"  cluster {i} vs cluster {j}: n/a (no enrichment coverage on one or both)"
                )
            else:
                lines.append(f"  cluster {i} vs cluster {j}: {overlap:.2f}")

    return "\n".join(lines)


def _render_personal_account(
    conn: sqlite3.Connection, plex_account_id: int, account_names: Mapping[int, str]
) -> str:
    """The report for every account not in `shared_account_ids`: one user,
    identified by name, with a play count and one keyword profile built
    over every play it ever recorded. No device count, no cluster count,
    no structural baseline, no pairwise overlap — the account already
    identifies the person, so none of that would mean anything (issue
    #27)."""
    units = account_units(conn, plex_account_id)
    play_count = sum(depth for _, depth in units)
    profile = build_keyword_profile(conn, units)
    lines = [
        _account_header(plex_account_id, account_names),
        f"plays: {play_count}",
    ]
    lines.extend(_render_keyword_profile_lines(profile))
    return "\n".join(lines)


def render_report(
    conn: sqlite3.Connection,
    plex_account_ids: Sequence[int] | None = None,
    *,
    shared_account_ids: Sequence[int],
    account_names: Mapping[int, str] | None = None,
) -> str:
    """The full human-readable, diffable report: one section per account.

    A `plex_account_id` in `shared_account_ids` gets the full
    fingerprint-clustered report (structural baseline, per-cluster keyword
    profile, pairwise overlap). Every other account gets exactly one user:
    name, play count, one keyword profile — see `_render_personal_account`
    for why nothing else is printed. `shared_account_ids` is required and
    never defaulted here — which accounts are shared is configuration the
    caller supplies (`Config.shared_account_ids`), not something this
    module infers from device or IP counts.

    `plex_account_ids` defaults to every account with any plays at all
    (`account_ids_with_plays`). `account_names` maps `plex_account_id` to
    the name Plex reports for it; an id missing from the map prints by id
    alone.
    """
    account_ids = (
        list(plex_account_ids) if plex_account_ids is not None else account_ids_with_plays(conn)
    )
    shared = set(shared_account_ids)
    names = account_names or {}

    sections = [
        _render_shared_account(conn, plex_account_id, names)
        if plex_account_id in shared
        else _render_personal_account(conn, plex_account_id, names)
        for plex_account_id in account_ids
    ]

    return "\n\n".join(sections) + "\n"
