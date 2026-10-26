//! When a fetched row is due to be fetched again (`plexdb/staleness.py`).
//!
//! Every external source in this store caches the same way: write a timestamp next
//! to what was fetched, and skip a re-fetch while that timestamp is inside a
//! window. Times are compared as instants, as Python compares two aware
//! `datetime`s, so `2026-10-01T00:00:00+00:00` and `2026-09-30T20:00:00-04:00` are
//! one moment.

use std::time::{SystemTime, UNIX_EPOCH};

/// Mid-range of the 30-60 day window `docs/schema.md` sets for every external
/// source's enrichment.
pub const DEFAULT_STALE_DAYS: i64 = 45;

const MICROS: i64 = 1_000_000;
const DAY_MICROS: i64 = 86_400 * MICROS;

/// One run's clock: read once, so every row a run writes carries the same time.
#[derive(Debug, Clone, Copy)]
pub struct Now {
    micros: i64,
}

impl Now {
    pub fn read() -> Self {
        let elapsed = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        Now {
            micros: i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX),
        }
    }

    /// `now.isoformat(timespec="seconds")` in UTC: `2026-10-01T00:00:00+00:00`.
    pub fn iso(&self) -> String {
        let seconds = self.micros.div_euclid(MICROS);
        let days = seconds.div_euclid(86_400);
        let rest = seconds.rem_euclid(86_400);
        let (year, month, day) = civil_from_days(days);
        format!(
            "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
            rest / 3600,
            rest % 3600 / 60,
            rest % 60
        )
    }

    /// The instant a row fetched before is stale: `stale_days` ago.
    pub fn cutoff(&self, stale_days: i64) -> Cutoff {
        Cutoff(
            self.micros
                .saturating_sub(stale_days.saturating_mul(DAY_MICROS)),
        )
    }
}

/// An instant, in microseconds since the epoch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cutoff(i64);

impl Cutoff {
    /// A cutoff at an exact instant, for tests.
    pub fn at_micros(micros: i64) -> Self {
        Cutoff(micros)
    }
}

/// A stored time this module cannot read, which Python's `fromisoformat` would
/// also refuse or could not compare.
#[derive(Debug, thiserror::Error)]
#[error("cannot read the fetch time {0:?}")]
pub struct BadTime(pub String);

/// Whether a stored timestamp predates `cutoff`, so the row is due a re-fetch.
pub fn is_stale(fetched_at: &str, cutoff: Cutoff) -> Result<bool, BadTime> {
    parse_micros(fetched_at)
        .map(|at| at < cutoff.0)
        .ok_or_else(|| BadTime(fetched_at.to_string()))
}

/// Microseconds since the epoch of an ISO 8601 time with an offset:
/// `YYYY-MM-DD[T ]HH:MM:SS[.f{1,6}](Z|+HH:MM[:SS])`.
pub fn parse_micros(text: &str) -> Option<i64> {
    let bytes = text.as_bytes();
    let digits = |from: usize, to: usize| -> Option<i64> {
        let part = bytes.get(from..to)?;
        part.iter()
            .all(u8::is_ascii_digit)
            .then(|| part.iter().fold(0i64, |n, d| n * 10 + i64::from(d - b'0')))
    };
    if bytes.get(4) != Some(&b'-') || bytes.get(7) != Some(&b'-') {
        return None;
    }
    if !matches!(bytes.get(10), Some(b'T' | b' ')) || bytes.get(13) != Some(&b':') {
        return None;
    }
    if bytes.get(16) != Some(&b':') {
        return None;
    }
    let (year, month, day) = (digits(0, 4)?, digits(5, 7)?, digits(8, 10)?);
    let (hour, minute, second) = (digits(11, 13)?, digits(14, 16)?, digits(17, 19)?);
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    let mut at = 19;
    let mut fraction = 0i64;
    if bytes.get(at) == Some(&b'.') {
        let start = at + 1;
        let mut end = start;
        while bytes.get(end).is_some_and(u8::is_ascii_digit) {
            end += 1;
        }
        if end == start || end - start > 6 {
            return None;
        }
        fraction = digits(start, end)? * 10i64.pow(u32::try_from(6 - (end - start)).ok()?);
        at = end;
    }
    let offset = match bytes.get(at..)? {
        b"Z" => 0,
        rest if rest.len() >= 6 && (rest[0] == b'+' || rest[0] == b'-') && rest[3] == b':' => {
            let sign = if rest[0] == b'+' { 1 } else { -1 };
            let hours = digits(at + 1, at + 3)?;
            let minutes = digits(at + 4, at + 6)?;
            let seconds = match rest.len() {
                6 => 0,
                9 if rest[6] == b':' => digits(at + 7, at + 9)?,
                _ => return None,
            };
            sign * (hours * 3600 + minutes * 60 + seconds)
        }
        _ => return None,
    };
    let days = days_from_civil(year, month, day);
    let seconds = days * 86_400 + hour * 3600 + minute * 60 + second - offset;
    Some(seconds * MICROS + fraction)
}

/// Days since 1970-01-01 of a proleptic Gregorian date.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let yoe = year - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_stored_time_is_compared_as_an_instant() {
        let cutoff = Cutoff::at_micros(parse_micros("2026-10-01T00:00:00+00:00").unwrap());
        assert!(is_stale("2026-09-30T23:59:59+00:00", cutoff).unwrap());
        assert!(!is_stale("2026-10-01T00:00:00+00:00", cutoff).unwrap());
        // The same instant, written in another offset, or with a fraction.
        assert!(!is_stale("2026-09-30T20:00:00-04:00", cutoff).unwrap());
        assert!(!is_stale("2026-10-01T00:00:00.000001+00:00", cutoff).unwrap());
        assert!(is_stale("2026-09-30T23:59:59.999999Z", cutoff).unwrap());
    }

    #[test]
    fn the_clock_writes_the_time_python_writes() {
        let now = Now {
            micros: parse_micros("2026-10-26T01:02:03.987654+00:00").unwrap(),
        };
        assert_eq!(now.iso(), "2026-10-26T01:02:03+00:00");
        assert_eq!(
            now.cutoff(45),
            Cutoff::at_micros(parse_micros("2026-09-11T01:02:03.987654+00:00").unwrap())
        );
    }

    #[test]
    fn an_unreadable_time_is_refused() {
        for text in [
            "",
            "yesterday",
            "2026-10-01",
            "2026-10-01T00:00:00",
            "2026-13-01T00:00:00+00:00",
        ] {
            assert!(parse_micros(text).is_none(), "{text}");
        }
    }
}
