//! Comparing two stored times (`list_decisions.is_before`). Reading the clock, and whether
//! a fetch time is stale, are `staleness`.

use crate::pyiso::isoformat_micros;

/// Whether the ISO 8601 time `earlier` is before `later`; a time that does not parse
/// is not before anything. A time with no offset is UTC.
pub fn is_before(earlier: &str, later: &str) -> bool {
    match (isoformat_micros(earlier), isoformat_micros(later)) {
        (Some(first), Some(second)) => first < second,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_time_without_an_offset_is_utc_and_junk_is_not_before_anything() {
        assert!(is_before(
            "2026-10-01T00:00:00",
            "2026-10-01T00:00:01+00:00"
        ));
        assert!(!is_before(
            "2026-10-01T00:00:00+00:00",
            "2026-10-01T00:00:00Z"
        ));
        assert!(!is_before("not a time", "2026-10-01T00:00:00+00:00"));
        assert!(is_before(
            "2026-10-01T05:00:00+05:00",
            "2026-10-01T00:00:01+00:00"
        ));
    }
}
