//! The clock the list steps read, in the spellings Python writes it
//! (`datetime.now(UTC).isoformat(...)`), and the comparisons they make between two
//! stored times (`list_decisions.is_before`, `staleness.is_stale`).

use std::time::{SystemTime, UNIX_EPOCH};

use crate::pyiso::{days_from_civil, isoformat_micros};

const MICROS_PER_DAY: i128 = 86_400 * 1_000_000;

/// Microseconds since the Unix epoch.
pub fn now_micros() -> i128 {
    let since = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    i128::try_from(since.as_micros()).unwrap_or(0)
}

/// `(year, month, day)` of a count of days since 1970-01-01 (Howard Hinnant's
/// `civil_from_days`).
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

fn parts(micros: i128) -> (i64, i64, i64, i64, i64, i64, i64) {
    let days = micros.div_euclid(MICROS_PER_DAY);
    let rest = micros.rem_euclid(MICROS_PER_DAY);
    let (year, month, day) = civil_from_days(i64::try_from(days).unwrap_or(0));
    let micro = i64::try_from(rest % 1_000_000).unwrap_or(0);
    let seconds = i64::try_from(rest / 1_000_000).unwrap_or(0);
    (
        year,
        month,
        day,
        seconds / 3600,
        seconds % 3600 / 60,
        seconds % 60,
        micro,
    )
}

/// `isoformat(timespec="seconds")` of a UTC time: `2026-10-26T12:00:00+00:00`.
pub fn iso_seconds(micros: i128) -> String {
    let (y, mo, d, h, mi, s, _) = parts(micros);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}+00:00")
}

/// `isoformat(timespec="microseconds")` of a UTC time.
pub fn iso_micros(micros: i128) -> String {
    let (y, mo, d, h, mi, s, us) = parts(micros);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{us:06}+00:00")
}

/// `datetime.now(UTC).isoformat(timespec="seconds")`.
pub fn now_iso() -> String {
    iso_seconds(now_micros())
}

/// Whether the ISO 8601 time `earlier` is before `later`; a time that does not parse
/// is not before anything. A time with no offset is UTC.
pub fn is_before(earlier: &str, later: &str) -> bool {
    match (isoformat_micros(earlier), isoformat_micros(later)) {
        (Some(first), Some(second)) => first < second,
        _ => false,
    }
}

/// The instant `days` before now, which a stored time must not predate to count as
/// fresh.
pub fn cutoff(days: i64) -> i128 {
    now_micros() - i128::from(days) * MICROS_PER_DAY
}

/// `staleness.is_stale`: a stored time that predates `cutoff` is due again. Python
/// raises on text it cannot read, and so does this.
pub fn is_stale(fetched_at: &str, cutoff: i128) -> Result<bool, String> {
    isoformat_micros(fetched_at)
        .map(|at| at < cutoff)
        .ok_or_else(|| format!("Invalid isoformat string: {fetched_at:?}"))
}

/// A date's day number, for tests that build a time by hand.
pub fn micros_of(year: i64, month: i64, day: i64, hour: i64, minute: i64, second: i64) -> i128 {
    (i128::from(days_from_civil(year, month, day)) * 86_400
        + i128::from(hour * 3600 + minute * 60 + second))
        * 1_000_000
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_time_round_trips_through_its_spelling() {
        let at = micros_of(2026, 10, 26, 12, 34, 56);
        assert_eq!(iso_seconds(at), "2026-10-26T12:34:56+00:00");
        assert_eq!(isoformat_micros(&iso_seconds(at)), Some(at));
        assert_eq!(iso_micros(at + 42), "2026-10-26T12:34:56.000042+00:00");
    }

    #[test]
    fn leap_days_and_the_epoch_come_out_right() {
        assert_eq!(iso_seconds(0), "1970-01-01T00:00:00+00:00");
        assert_eq!(
            iso_seconds(micros_of(2024, 2, 29, 23, 59, 59)),
            "2024-02-29T23:59:59+00:00"
        );
    }

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
