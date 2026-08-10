"""Deriving `item_id` — the identity every table in the store keys on.

**This rule exists twice.** The other copy is Rust, in `etv-station` at
`crates/etv-station/src/catalog/identity.rs`, where it is called `entry_id`. The
two must agree byte for byte: if they drift, every join between this store and
that consumer silently returns nothing and neither side raises
([ADR-0002](../docs/adr/0002-item-id-is-the-entry-id-string.md)).

Nothing enforces that at runtime, so it is enforced by test: both sides read the
same `tests/fixtures/entry_id.json` and check they produce the same answers, and
both pin its SHA-256 so an edit on one side fails on both
([ADR-0006](../docs/adr/0006-the-identity-fixture-is-duplicated-and-guarded-by-a-hash.md)).

Change nothing here without changing the Rust and the fixture in the same breath.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: Strongest first. The order *is* the rule — an item carrying both an IMDb and
#: a TMDb id is `imdb:…`, always, regardless of which arrived first.
PRIORITY: tuple[str, ...] = ("imdb", "tmdb", "tvdb", "plex")

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x00000100000001B3
_U64 = 0xFFFFFFFFFFFFFFFF


def fnv1a_64(text: str) -> int:
    """FNV-1a, 64-bit, over the UTF-8 bytes of `text`.

    Chosen on the Rust side over a language-provided hasher because an
    `item_id` is persisted and must stay stable across toolchain upgrades. That
    reasoning is why this is reimplementable here at all — a documented
    algorithm has one right answer in any language.
    """
    hash_ = _FNV_OFFSET
    for byte in text.encode("utf-8"):
        hash_ ^= byte
        hash_ = (hash_ * _FNV_PRIME) & _U64
    return hash_


def canonical_path(raw: str, source_roots: Sequence[str]) -> str:
    """Normalise a filesystem path for identity, so one file is one identity.

    Strips whichever configured source root the path sits under and normalises
    separators, so the same file reached through two different mounts resolves
    the same way.

    Longest root first, and a root only matches at a path boundary: given roots
    `/Volumes` and `/Volumes/media`, the file `/Volumes/mediacache/x.mkv` must
    not have `/Volumes/media` stripped off it — that would reparent an unrelated
    file and risk an id collision. A root that matches as a bare string prefix
    but not at a boundary is skipped and the next root is tried.

    This is the deterministic, string-only half of the rule. Resolving symlinks
    is a filesystem operation and belongs to whatever is walking the library.
    """
    normalized = raw.replace("\\", "/")
    roots = sorted(
        (root.replace("\\", "/").rstrip("/") for root in source_roots),
        key=len,
        reverse=True,
    )
    for root in roots:
        if not normalized.startswith(root):
            continue
        rest = normalized[len(root) :]
        if not rest:
            return ""
        if rest.startswith("/"):
            return rest[1:]
        # Matched as a string but not at a boundary — keep looking.
    return normalized


def derive_item_id(
    external_ids: Iterable[tuple[str, str]],
    canonical: str,
) -> str:
    """The `item_id` for a title, first hit wins.

    Args:
        external_ids: `(namespace, value)` pairs in any order, possibly with
            duplicates and namespaces this rule does not recognise.
        canonical: the already-canonicalised path, used only when no recognised
            namespace is present.

    Returns:
        `"{namespace}:{value}"` for the strongest namespace present — e.g.
        `imdb:tt1375666` — else `"fs:"` and the path hash as 16 hex digits.
    """
    pairs = list(external_ids)
    for namespace in PRIORITY:
        for ns, value in pairs:
            if ns == namespace:
                return f"{namespace}:{value}"
    return f"fs:{fnv1a_64(canonical):016x}"
