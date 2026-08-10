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
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

#: An IP must recur on at least this many plays lacking a `client_identifier`
#: before it is trusted as a household bucket for that account. Below this,
#: it is excluded from clustering entirely — see the module docstring.
MIN_IP_OCCURRENCES = 2

#: How many keywords a cluster's profile keeps, ranked by how many distinct
#: items in the cluster carry them (ties broken alphabetically, so the
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
    """

    key_tier: str
    key_value: str | None
    client_identifiers: tuple[str, ...]
    ips: tuple[str, ...]
    platforms: tuple[str, ...]
    item_ids: tuple[str, ...]
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
    """A cluster's top-N keyword profile, and the coverage it was built
    from. `items_with_coverage` can be zero — that is a real, reportable
    result (the acceptance criteria calls it out explicitly), not an error."""

    items_total: int
    items_with_coverage: int
    top_keywords: tuple[tuple[str, int], ...]

    @property
    def has_coverage(self) -> bool:
        return self.items_with_coverage > 0


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


def cluster_account_plays(conn: sqlite3.Connection, plex_account_id: int) -> AccountClusters:
    """Cluster one account's plays on the fingerprint fallback chain.

    Deterministic: the only inputs are `plays` rows for this account, and the
    output ordering never depends on `plays`' own row order (queried without
    one, then sorted explicitly below) — running this twice over an unchanged
    store produces byte-identical `Cluster` tuples.
    """
    rows = conn.execute(
        "SELECT item_id, client_identifier, ip, platform FROM plays WHERE plex_account_id = ?",
        (plex_account_id,),
    ).fetchall()

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


def build_keyword_profile(
    conn: sqlite3.Connection,
    item_ids: Sequence[str],
    *,
    top_n: int = TOP_N_KEYWORDS,
) -> KeywordProfile:
    """The top-`top_n` `tmdb_keywords` keywords across `item_ids`, ranked by
    how many distinct items in the set carry each one.

    Each item contributes a keyword at most once regardless of how many
    times that item was replayed — `item_ids` is already the cluster's
    distinct-item set, not its play list, so a rewatched title cannot crowd
    out the rest of the profile.
    """
    if not item_ids:
        return KeywordProfile(items_total=0, items_with_coverage=0, top_keywords=())

    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        "SELECT item_id, value FROM enrichment "
        f"WHERE namespace = ? AND key = ? AND item_id IN ({placeholders})",
        (_TMDB_KEYWORDS_NAMESPACE, _KEYWORD_KEY, *item_ids),
    ).fetchall()

    counts: Counter[str] = Counter()
    covered_items: set[str] = set()
    for row in rows:
        counts[row["value"]] += 1
        covered_items.add(row["item_id"])

    top_keywords = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
    return KeywordProfile(
        items_total=len(item_ids),
        items_with_coverage=len(covered_items),
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


def render_report(conn: sqlite3.Connection, plex_account_ids: Sequence[int] | None = None) -> str:
    """The full human-readable, diffable report: one section per account,
    each with its structural baseline, per-cluster keyword profile, and
    pairwise overlap.

    `plex_account_ids` defaults to every account with any plays at all
    (`account_ids_with_plays`) — this module does not decide which accounts
    are "shared"; the structural baseline it prints is what lets a reader
    see that themselves.
    """
    account_ids = (
        list(plex_account_ids) if plex_account_ids is not None else account_ids_with_plays(conn)
    )

    sections: list[str] = []
    for plex_account_id in account_ids:
        account = cluster_account_plays(conn, plex_account_id)
        lines = [
            f"== account {account.plex_account_id} ==",
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
            profile = build_keyword_profile(conn, cluster.item_ids)
            profiles[index] = profile

            lines.append("")
            lines.append(f"cluster {index} [{cluster.key_tier}={cluster.key_value!r}]")
            devices = ", ".join(cluster.client_identifiers) or "(none)"
            lines.append(f"  client_identifier(s): {devices}")
            lines.append(f"  ip(s) seen: {', '.join(cluster.ips) or '(none)'}")
            lines.append(f"  platform(s): {', '.join(cluster.platforms) or '(none)'}")
            lines.append(f"  plays: {cluster.play_count}, distinct items: {len(cluster.item_ids)}")
            if profile.has_coverage:
                top = ", ".join(f"{keyword} ({count})" for keyword, count in profile.top_keywords)
                lines.append(
                    f"  keyword profile ({profile.items_with_coverage}/{profile.items_total} "
                    f"item(s) with tmdb_keywords coverage): {top}"
                )
            else:
                lines.append(
                    f"  keyword profile: no enrichment coverage "
                    f"(0/{profile.items_total} item(s) carry tmdb_keywords)"
                )

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

        sections.append("\n".join(lines))

    return "\n\n".join(sections) + "\n"
