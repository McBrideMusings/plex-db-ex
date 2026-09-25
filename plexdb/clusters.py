"""Clustering a shared Plex account's plays into latent users (issue #10).

**This module reports; it does not judge and it does not persist.** Nothing
here writes to the store — it reads `plays` and `enrichment` and returns
numbers a human reads to decide whether the clustering is good enough to
build on. That call, and any future latent-user table, are explicitly out of
scope (see the issue).

**The fingerprint tuple, applied as a fallback chain for a play with no
`client_identifier` — plus one deliberate merge on top (issue #27).** Per
`docs/CONTEXT.md`'s "Fingerprint" entry: client machine id first, then IP as
a coarse household bucket, then platform as a weak tiebreak, with device
display name excluded everywhere (the schema does not even carry Plex's
device display name past `plexdb.plays`, so there is nothing to exclude in
code). A play's cluster key is its `client_identifier` when present; only a
play with none falls through to IP, and only a play with neither falls
through to platform.

**Two different `client_identifier` values *do* merge, but only across one
specific bridge: a recurring IP neither of them shares with any other Plex
account.** Issue #10 built the chain as a strict fallback and never merged
two real devices; issue #27 asks for exactly that merge, because within a
shared account two devices seen at the same recurring address are probably
one household member, not two strangers. This is still not a general
merge — platform never bridges two `client_identifier`s, and an IP that
recurs for only one device (the fallback case above) never gains a second
one retroactively. See "Joining devices on a recurring IP" below for the
eligibility rule and why it reuses `MIN_IP_OCCURRENCES`. Two caveats from
#10 remain genuinely unsolved, not addressed by the IP bridge: a client
machine id is stable per *install*, so a reinstall forks an identity into
two clusters the IP bridge may or may not happen to reconnect; and two
people sharing one physical client still collapse into one cluster no
matter what IP it used.

**Transient IPs are excluded from the fallback tier, not down-weighted in
the profile.** An IP that appears on exactly one client-identifier-less play
for an account carries no repeat signal — it reads the same as a hotel or
cellular address passing through once. `MIN_IP_OCCURRENCES` is the floor
below which an IP is treated as though it were absent, falling through to
the platform tier (or the unclustered bucket) instead of becoming its own
one-play cluster.

**Joining devices on a recurring IP (issue #27), and the two ways an IP is
disqualified from doing so.** This is a second, independent use of IP data
from the fallback tier above — it looks at *every* play with a
`client_identifier`, not only the ones missing one, asking a different
question: did two distinct devices both recur at the same address? An IP
qualifies to join the devices it connects only if both hold:

1. **It recurs at least `MIN_IP_OCCURRENCES` times within this account.**
   The join threshold reuses the fallback tier's constant rather than
   defining its own. Both ask the identical question — has this IP shown up
   enough to be trusted as signal, rather than one passing touch — just
   applied to a different situation, and measured against the live store
   (2,563 plays on account 1, 7,551 on account 3670670) there is no cliff in
   the data arguing for a different number: every genuine multi-device IP
   that survives criterion 2 below already clears `MIN_IP_OCCURRENCES` by at
   least one full play. The risk specific to joining — that a coincidental
   or generic IP pairs two devices that are not actually the same person —
   is not what a higher occurrence floor would catch anyway; it is caught by
   criterion 2.
2. **It is not seen under more than `MAX_IP_ACCOUNTS_TO_JOIN` distinct
   `plex_account_id`s anywhere in the store.** An IP one account's household
   router assigns is, by construction, that account's alone; an IP that
   turns up under a second account is either shared upstream infrastructure
   (CGNAT, a VPN exit) or, on the live store, `127.0.0.1` and default
   `192.168.0.x` router ranges that unrelated households happen to reuse —
   in both cases, evidence about a different account, not about this one.
   Measured on the live store: 807 of 850 IPs used by more than one client
   belong to exactly one `plex_account_id`; only 44 cross an account
   boundary, and every one inspected was either loopback, an RFC1918
   default, or a genuine public IP shared between the *two* shared accounts
   themselves (plausibly the same physical household running both). None of
   the 44 is trustworthy evidence that two devices *within one account* are
   the same person, so the bar is the strictest one that still admits every
   IP actually private to one account: more than one account disqualifies
   it, full stop.

An IP that connects two or more devices but fails either check is reported
as discounted, with which check it failed, rather than silently dropped —
per the acceptance criteria. In practice, on the live store, every
multi-device IP that fails does so on criterion 2; criterion 1 can only ever
discount a multi-device IP if `MIN_IP_OCCURRENCES` is raised above 2, since
two distinct devices imply at least two plays by construction — it is kept
for that future, not because it fires today.

A join is a true merge, not a new tier: the merged devices' plays still key
on `"client_identifier"`, just on a canonical representative (the
alphabetically smallest `client_identifier` in the merged group) instead of
each device's own id, so `key_tier` and the tier-ordering/sort behavior are
unchanged. Which IP(s) did the joining is carried separately, on
`Cluster.joined_by_ips`, so the report can name them without disturbing
`key_value`. The canonical representative — not whichever id a union-find
pass happens to leave as its internal root — is what makes the merge
deterministic across runs regardless of dict/set iteration order.

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

**A cluster under `LATENT_USER_FLOOR` plays is not reported as a latent
user (issue #28).** `bboy2448` clusters into 126 groups and
`McBrideMusings` into 39 — half of `bboy2448`'s are a handful of plays: a
friend's TV signed in once, a borrowed browser session. A human reading 126
rows cannot tell which ones are household members, so the *report* (never
the clustering — `cluster_account_plays` computes the identical clusters
either way, and nothing is persisted regardless) declines to call a
sub-floor cluster a person. Its plays are not dropped: every cluster under
the floor is folded into one `unattributed` bucket per account, printed
with the play count and device count it covers, so a reader can see the
size of what the report declined to name. The bucket is not itself a
latent user — it carries no keyword profile and enters no pairwise overlap
(there is nothing coherent to profile: it is a grab-bag of unrelated
sub-floor clusters, not one person's plays). See `LATENT_USER_FLOOR` for
the measurement behind 20 specifically, and `_split_by_floor` for the
device-count definition.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

#: An IP must recur on at least this many plays before it is trusted as
#: signal for that account — below this, it is excluded entirely. Two
#: independent uses share this one floor (see the module docstring): as a
#: household-bucket fallback key for plays lacking a `client_identifier`
#: (issue #10), and as the recurrence check for joining two different
#: `client_identifier`s that share an IP (issue #27). Both ask the same
#: question — has this IP shown up enough to be trusted — so one constant
#: covers both rather than a second, undemonstrated number.
MIN_IP_OCCURRENCES = 2

#: An IP may join devices within an account only if it appears under no more
#: than this many distinct `plex_account_id`s anywhere in the store. Above
#: this, it reads as shared infrastructure (CGNAT, a VPN exit, a
#: coincidentally reused private-range default) rather than one account's
#: household — see the module docstring's "Joining devices on a recurring
#: IP" section for the live-store numbers behind this bar.
MAX_IP_ACCOUNTS_TO_JOIN = 1

#: A cluster with fewer than this many plays is not reported as a latent
#: user — its plays are folded into the account's `unattributed` bucket
#: instead (issue #28). Measured on the live store, comparing candidate
#: floors by people reported vs. viewing discarded:
#:
#:     bboy2448 (126 clusters, 7,551 plays)          McBrideMusings (39 clusters, 2,563 plays)
#:     floor  people  plays kept  % of viewing        floor  people  plays kept  % of viewing
#:         5      77       7,452         98%              5      23       2,533         98%
#:        20      50       7,177         95%             20      15       2,458         95%
#:        50      31       6,497         86%             50       8       2,161         84%
#:       100      19       5,773         76%            100       6       2,039         79%
#:
#: 100 gives the most plausible people count but discards 24% of
#: `bboy2448`'s viewing — too much thrown away for a tidier number. 20 keeps
#: 95% of viewing on both accounts while still cutting the one-off-device
#: noise a reader cannot tell from a household member. Decided in the issue;
#: not reopened here.
LATENT_USER_FLOOR = 20

#: How many keywords a cluster's profile keeps, ranked by how many distinct
#: units in the cluster carry them (ties broken alphabetically, so the
#: profile — and therefore the overlap computed from it — is stable across
#: runs over unchanged data).
TOP_N_KEYWORDS = 20

#: The namespace and key every keyword source writes actual keyword values
#: under (issue #4; renamed from the TMDB-specific `tmdb_keywords` by
#: ADR-0016, which moved the source into its own column). Read-only here —
#: this module never writes enrichment.
_TMDB_KEYWORDS_NAMESPACE = "keywords"
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

    `joined_by_ips` is every IP that caused two or more `client_identifier`s
    to merge into this cluster (issue #27) — empty for a cluster that is a
    single, unmerged device, or that landed here via the `"ip"`, `"platform"`,
    or `"unclustered"` tier instead. Sorted for determinism; see the module
    docstring's "Joining devices on a recurring IP" section for how an IP
    earns a place here.
    """

    key_tier: str
    key_value: str | None
    client_identifiers: tuple[str, ...]
    ips: tuple[str, ...]
    platforms: tuple[str, ...]
    item_ids: tuple[str, ...]
    units: tuple[tuple[str, int], ...]
    play_count: int
    joined_by_ips: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscountedJoinIp:
    """An IP seen at two or more `client_identifier`s within an account, but
    excluded from joining them anyway — reported so a reader can see what
    was discounted and why, per issue #27's acceptance criteria. `reason` is
    a complete, human-readable sentence fragment, not a code — the two
    reasons (too rare, seen under more than one account) are prose because
    there are only two of them and a symbolic reason code would just be
    re-encoding this same string elsewhere."""

    ip: str
    reason: str


@dataclass(frozen=True)
class UnattributedBucket:
    """Every cluster under `LATENT_USER_FLOOR` plays, folded into one bucket
    per account instead of being listed as its own latent user or silently
    dropped (issue #28). Not a latent user: it carries no `KeywordProfile`
    and is never a party to pairwise overlap — see `_split_by_floor`.

    `device_count` sums, per folded cluster, `len(client_identifiers)` if
    the cluster has any, else 1 — a `"client_identifier"`-tier cluster
    already knows how many distinct devices merged into it (issue #27); an
    `"ip"`/`"platform"`/`"unclustered"`-tier cluster has no client id to
    count, but is still one device-shaped grouping whose identity chain
    fell through the fingerprint tiers, so it contributes exactly one."""

    cluster_count: int
    play_count: int
    device_count: int


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
    #: IPs that connected two or more devices but were discounted from
    #: joining them — see `DiscountedJoinIp`. Sorted by ip for determinism.
    discounted_join_ips: tuple[DiscountedJoinIp, ...] = ()


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


def _ip_account_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many distinct `plex_account_id`s each IP appears under, across
    every account in the store — not just the one being clustered. This is
    the cross-account guard issue #27 asks for: an IP tied to more than one
    account is shared infrastructure, not one account's household, and must
    never join two devices even if it recurs plenty within a single account.
    See the module docstring for the live-store numbers behind this check.
    """
    rows = conn.execute(
        "SELECT ip, COUNT(DISTINCT plex_account_id) AS n FROM plays "
        "WHERE ip IS NOT NULL GROUP BY ip"
    ).fetchall()
    return {row["ip"]: int(row["n"]) for row in rows}


def _find(parent: dict[str, str], x: str) -> str:
    """Union-find root lookup with path compression."""
    parent.setdefault(x, x)
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _union(parent: dict[str, str], a: str, b: str) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[ra] = rb


def _join_devices_on_recurring_ips(
    rows: list[sqlite3.Row], ip_account_counts: Mapping[str, int]
) -> tuple[dict[str, str], dict[str, tuple[str, ...]], tuple[DiscountedJoinIp, ...]]:
    """Decide which `client_identifier`s merge, on which IPs, and which
    candidate IPs were discounted — the issue #27 half of clustering. See
    the module docstring's "Joining devices on a recurring IP" section for
    the eligibility rule.

    Returns `(canonical, joined_by, discounted)`:
    - `canonical` maps every `client_identifier` seen in `rows` to its
      merged group's representative (itself, if it merged with nothing) —
      the alphabetically smallest id in the group, chosen after union-find
      settles so it does not depend on union call order.
    - `joined_by` maps each representative to the sorted IPs that produced
      its merge (absent, or empty, for a group that never merged).
    - `discounted` is every candidate IP excluded from joining, sorted by ip.
    """
    distinct_client_identifiers = {r["client_identifier"] for r in rows if r["client_identifier"]}

    ip_join_occurrences: Counter[str] = Counter()
    ip_join_clients: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["client_identifier"] is not None and r["ip"] is not None:
            ip_join_occurrences[r["ip"]] += 1
            ip_join_clients[r["ip"]].add(r["client_identifier"])

    parent: dict[str, str] = {}
    joining_ips_by_client: dict[str, set[str]] = defaultdict(set)
    discounted: list[DiscountedJoinIp] = []
    for ip, clients in sorted(ip_join_clients.items()):
        if len(clients) < 2:
            continue  # not a join candidate at all — only one device ever used it
        if ip_join_occurrences[ip] < MIN_IP_OCCURRENCES:
            discounted.append(
                DiscountedJoinIp(
                    ip=ip, reason=f"seen on only {ip_join_occurrences[ip]} play(s) across devices"
                )
            )
            continue
        if ip_account_counts.get(ip, 0) > MAX_IP_ACCOUNTS_TO_JOIN:
            discounted.append(
                DiscountedJoinIp(
                    ip=ip,
                    reason=(
                        f"seen under {ip_account_counts[ip]} different accounts, so treated "
                        "as shared infrastructure rather than a household"
                    ),
                )
            )
            continue
        ordered = sorted(clients)
        for other in ordered[1:]:
            _union(parent, ordered[0], other)
        for client in ordered:
            joining_ips_by_client[client].add(ip)

    # Canonical representative per merged group: the alphabetically smallest
    # member, independent of which id union-find happened to leave as root —
    # see the module docstring for why this is what keeps the merge
    # deterministic across runs.
    groups: dict[str, set[str]] = defaultdict(set)
    for client in distinct_client_identifiers:
        groups[_find(parent, client)].add(client)
    canonical: dict[str, str] = {}
    for members in groups.values():
        rep = min(members)
        for member in members:
            canonical[member] = rep

    joined_by: dict[str, tuple[str, ...]] = defaultdict(tuple)
    joined_by_sets: dict[str, set[str]] = defaultdict(set)
    for client, ips in joining_ips_by_client.items():
        joined_by_sets[canonical[client]].update(ips)
    for rep, ips in joined_by_sets.items():
        joined_by[rep] = tuple(sorted(ips))

    return canonical, joined_by, tuple(sorted(discounted, key=lambda d: d.ip))


def cluster_account_plays(conn: sqlite3.Connection, plex_account_id: int) -> AccountClusters:
    """Cluster one account's plays on the fingerprint fallback chain, then
    merge devices that share a recurring, single-account IP.

    Deterministic: the only inputs are `plays` rows for this account (plus,
    for the cross-account join check, the whole store's `ip` -> account-count
    map), and the output ordering never depends on `plays`' own row order
    (queried without one, then sorted explicitly below) — running this twice
    over an unchanged store produces byte-identical `Cluster` tuples.
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

    ip_account_counts = _ip_account_counts(conn)
    canonical, joined_by, discounted_join_ips = _join_devices_on_recurring_ips(
        rows, ip_account_counts
    )

    buckets: dict[tuple[str, str | None], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        if row["client_identifier"] is not None:
            key: tuple[str, str | None] = (
                "client_identifier",
                canonical[row["client_identifier"]],
            )
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
            joined_by_ips=(
                joined_by.get(value, ())
                if tier == "client_identifier" and value is not None
                else ()
            ),
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
        discounted_join_ips=discounted_join_ips,
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
    # DISTINCT so a keyword two different sources both list on the same item
    # (ADR-0016's `source` column lets both rows exist) contributes to this
    # unit's profile once, not once per source — a unit's keyword set is what
    # is being counted, not how many sources agree on each entry in it.
    rows = conn.execute(
        "SELECT DISTINCT item_id, value FROM enrichment "
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


def _split_by_floor(
    clusters: Sequence[Cluster],
) -> tuple[tuple[Cluster, ...], UnattributedBucket]:
    """Partition an account's clusters at `LATENT_USER_FLOOR` (issue #28):
    clusters at or above the floor, reported individually as latent users,
    and everything under it, folded into one `UnattributedBucket`. Pure
    report-layer filtering — `cluster_account_plays` itself is untouched, so
    the clusters computed are identical whether or not this runs, per the
    module docstring."""
    kept = tuple(c for c in clusters if c.play_count >= LATENT_USER_FLOOR)
    sub_floor = tuple(c for c in clusters if c.play_count < LATENT_USER_FLOOR)
    bucket = UnattributedBucket(
        cluster_count=len(sub_floor),
        play_count=sum(c.play_count for c in sub_floor),
        device_count=sum(max(len(c.client_identifiers), 1) for c in sub_floor),
    )
    return kept, bucket


def _render_shared_account(
    conn: sqlite3.Connection, plex_account_id: int, account_names: Mapping[int, str]
) -> str:
    """The full report for a configured shared account: structural
    baseline, the `unattributed` bucket for clusters under
    `LATENT_USER_FLOOR` plays (issue #28), per-cluster keyword profile, and
    pairwise overlap, plus (issue #27) which IPs joined devices into a
    cluster and which candidate IPs were discounted from doing so.

    Every cluster and pairwise-overlap line below is drawn from `kept` — the
    clusters at or above the floor — never from `account.clusters` directly,
    so a sub-floor cluster is never listed as its own latent user."""
    account = cluster_account_plays(conn, plex_account_id)
    kept, unattributed = _split_by_floor(account.clusters)
    lines = [
        _account_header(plex_account_id, account_names),
        f"structural baseline: {account.play_count} play(s), "
        f"{account.distinct_client_identifiers} distinct client_identifier(s), "
        f"{len(account.clusters)} cluster(s) found, {len(kept)} at or above the "
        f"{LATENT_USER_FLOOR}-play floor",
    ]
    if unattributed.cluster_count:
        lines.append(
            f"unattributed: {unattributed.play_count} play(s) across "
            f"{unattributed.cluster_count} cluster(s) under the {LATENT_USER_FLOOR}-play "
            f"floor ({unattributed.device_count} device(s)) — not a latent user, no "
            "keyword profile, no pairwise overlap"
        )
    if account.transient_ips:
        lines.append(
            f"transient IP(s) excluded from clustering (fewer than {MIN_IP_OCCURRENCES} "
            "plays with no client_identifier, so treated as passing traffic rather than "
            f"a household): {', '.join(account.transient_ips)}"
        )
    if account.discounted_join_ips:
        lines.append("IP(s) discounted from joining devices:")
        for discounted in account.discounted_join_ips:
            lines.append(f"  {discounted.ip}: {discounted.reason}")

    profiles: dict[int, KeywordProfile] = {}
    for index, cluster in enumerate(kept, start=1):
        profile = build_keyword_profile(conn, cluster.units)
        profiles[index] = profile

        lines.append("")
        lines.append(f"cluster {index} [{cluster.key_tier}={cluster.key_value!r}]")
        devices = ", ".join(cluster.client_identifiers) or "(none)"
        lines.append(f"  client_identifier(s): {devices}")
        if cluster.joined_by_ips:
            lines.append(f"  joined by ip(s): {', '.join(cluster.joined_by_ips)}")
        lines.append(f"  ip(s) seen: {', '.join(cluster.ips) or '(none)'}")
        lines.append(f"  platform(s): {', '.join(cluster.platforms) or '(none)'}")
        lines.append(
            f"  plays: {cluster.play_count}, distinct items: {len(cluster.item_ids)}, "
            f"distinct units (a show counts once, not once per episode): {len(cluster.units)}"
        )
        lines.extend(_render_keyword_profile_lines(profile, indent="  "))

    lines.append("")
    if len(kept) < 2:
        lines.append("pairwise overlap: n/a (fewer than two clusters)")
    else:
        lines.append(
            f"pairwise keyword-profile overlap (Jaccard over top-{TOP_N_KEYWORDS} keywords):"
        )
        for i, j in combinations(range(1, len(kept) + 1), 2):
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
