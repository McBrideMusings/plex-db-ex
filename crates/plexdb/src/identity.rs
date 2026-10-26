//! Deriving `item_id` — the identity every table in the store keys on
//! (`plexdb/identity.py`, ADR-0002).
//!
//! First hit wins over the external GUIDs Plex reports — `imdb:` → `tmdb:` →
//! `tvdb:` → `plex:` — falling back to `fs:<fnv1a hex of canonical path>`. The
//! rule is published, so changing it is a schema change: its specification is
//! `tests/fixtures/item_id.json`, which `tests/spec.rs` runs against this code.

use plexdb_store::py_strip;

/// Strongest first. The order is the rule.
pub const PRIORITY: [&str; 4] = ["imdb", "tmdb", "tvdb", "plex"];

const FNV_OFFSET: u64 = 0xCBF2_9CE4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01B3;

/// FNV-1a, 64-bit, over the UTF-8 bytes of `text`.
pub fn fnv1a_64(text: &str) -> u64 {
    text.bytes().fold(FNV_OFFSET, |hash, byte| {
        (hash ^ u64::from(byte)).wrapping_mul(FNV_PRIME)
    })
}

/// Normalise a filesystem path for identity, so one file is one identity: strip
/// whichever configured source root the path sits under, longest root first, and
/// only at a path boundary.
pub fn canonical_path(raw: &str, source_roots: &[String]) -> String {
    let normalized = raw.replace('\\', "/");
    let mut roots: Vec<String> = source_roots
        .iter()
        .map(|root| root.replace('\\', "/").trim_end_matches('/').to_string())
        .collect();
    // Python's `sorted(..., key=len, reverse=True)` is stable and counts characters.
    roots.sort_by_key(|root| std::cmp::Reverse(root.chars().count()));
    for root in &roots {
        let Some(rest) = normalized.strip_prefix(root.as_str()) else {
            continue;
        };
        if rest.is_empty() {
            return String::new();
        }
        if let Some(below) = rest.strip_prefix('/') {
            return below.to_string();
        }
        // Matched as a string but not at a boundary — keep looking.
    }
    normalized
}

/// The `item_id` for a title, first hit wins. An empty or whitespace-only value is
/// absent; any other value is used verbatim, surrounding whitespace included.
pub fn derive_item_id(external_ids: &[(String, String)], canonical: &str) -> String {
    for namespace in PRIORITY {
        for (ns, value) in external_ids {
            if ns == namespace && !py_strip(value).is_empty() {
                return format!("{namespace}:{value}");
            }
        }
    }
    format!("fs:{:016x}", fnv1a_64(canonical))
}
