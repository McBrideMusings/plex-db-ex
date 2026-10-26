//! Whether Python's `datetime.fromisoformat` accepts a string. A decisions file's
//! `decided_at` must parse there (`DecisionsFile.parse`), and a file one side
//! refuses while the other folds it puts different rows in the store. The value
//! is stored as written, so this only answers yes or no.
//!
//! CPython runs the C implementation (`Modules/_datetimemodule.c`), which accepts
//! more than the pure-Python one: a fraction with no decimal mark, junk between a
//! fraction and its offset, a separator that is any one character. This follows
//! the C of Python 3.12, the version the image runs and `.python-version` pins,
//! byte for byte over the UTF-8 text with a trailing NUL, the buffer it reads.
//! 3.13 and later differ: they accept `T24:00:00` as the next midnight and refuse
//! a decimal mark on the hour or minute. `tests/fixtures/decided_at.json` is
//! checked against the running Python, so a version bump fails a test here.

const MAX_YEAR: i32 = 9999;

struct Buf<'a>(&'a [u8]);

impl Buf<'_> {
    /// The byte at `at`; the C reads its NUL terminator past the end.
    fn at(&self, at: usize) -> u8 {
        self.0.get(at).copied().unwrap_or(0)
    }

    /// `parse_digits`: `count` ASCII digits from `at`, and where they end.
    fn digits(&self, at: usize, count: usize) -> Option<(i32, usize)> {
        let mut value: i32 = 0;
        for offset in 0..count {
            let digit = self.at(at + offset).wrapping_sub(b'0');
            if digit > 9 {
                return None;
            }
            value = value.checked_mul(10)?.checked_add(i32::from(digit))?;
        }
        Some((value, at + count))
    }
}

fn is_leap(year: i32) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in_month(year: i32, month: i32) -> i32 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn days_before_year(year: i32) -> i64 {
    let y = i64::from(year) - 1;
    y * 365 + y / 4 - y / 100 + y / 400
}

/// `ymd_to_ord`: 1 January of year 1 is day 1.
fn ordinal(year: i32, month: i32, day: i32) -> i64 {
    let before: i64 = (1..month).map(|m| i64::from(days_in_month(year, m))).sum();
    days_before_year(year) + before + i64::from(day)
}

/// `ord_to_ymd`.
fn ymd(ordinal: i64) -> (i32, i32, i32) {
    let mut year = i32::try_from((ordinal - 1) / 366 + 1).unwrap_or(i32::MAX);
    while days_before_year(year + 1) < ordinal {
        year += 1;
    }
    let mut rest = ordinal - days_before_year(year);
    let mut month = 1;
    while month < 12 && rest > i64::from(days_in_month(year, month)) {
        rest -= i64::from(days_in_month(year, month));
        month += 1;
    }
    (year, month, i32::try_from(rest).unwrap_or(i32::MAX))
}

/// `iso_to_ymd`.
fn iso_to_ymd(year: i32, week: i32, day: i32) -> Option<(i32, i32, i32)> {
    if !(1..=MAX_YEAR).contains(&year) {
        return None;
    }
    // Monday is 0.
    let jan1_weekday = (ordinal(year, 1, 1) + 6) % 7;
    if !(1..53).contains(&week) {
        let long_year = jan1_weekday == 3 || (jan1_weekday == 2 && is_leap(year));
        if !(week == 53 && long_year) {
            return None;
        }
    }
    if !(1..8).contains(&day) {
        return None;
    }
    let jan1 = ordinal(year, 1, 1);
    let mut week1_monday = jan1 - jan1_weekday;
    if jan1_weekday > 3 {
        week1_monday += 7;
    }
    Some(ymd(week1_monday + i64::from((week - 1) * 7 + day - 1)))
}

/// `_find_isoformat_datetime_separator`, over byte positions.
fn separator_at(s: &Buf, len: usize) -> Option<usize> {
    if len == 7 {
        return Some(7);
    }
    if s.at(4) == b'-' {
        if s.at(5) == b'W' {
            if len < 8 {
                return None;
            }
            if len > 8 && s.at(8) == b'-' {
                if len == 9 {
                    return None;
                }
                if len > 10 && s.at(10).is_ascii_digit() {
                    return Some(8);
                }
                return Some(10);
            }
            return Some(8);
        }
        return Some(10);
    }
    if s.at(4) == b'W' {
        let mut idx = 7;
        while idx < len && s.at(idx).is_ascii_digit() {
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        return Some(if idx % 2 == 0 { 7 } else { 8 });
    }
    Some(8)
}

/// `parse_isoformat_date` over the first `len` bytes.
fn parse_date(s: &Buf, len: usize) -> Option<(i32, i32, i32)> {
    let (year, mut p) = s.digits(0, 4)?;
    let uses_separator = s.at(p) == b'-';
    if uses_separator {
        p += 1;
    }
    if s.at(p) == b'W' {
        let (week, after) = s.digits(p + 1, 2)?;
        p = after;
        let mut day = 1;
        if p < len {
            if uses_separator {
                if s.at(p) != b'-' {
                    return None;
                }
                p += 1;
            }
            day = s.digits(p, 1)?.0;
        }
        return iso_to_ymd(year, week, day);
    }
    let (month, mut p) = s.digits(p, 2)?;
    if uses_separator {
        if s.at(p) != b'-' {
            return None;
        }
        p += 1;
    }
    let (day, _) = s.digits(p, 2)?;
    Some((year, month, day))
}

/// What `parse_hh_mm_ss_ff` returns: its parts, and whether it stopped short of
/// the end of the string (its return of 1).
struct Clock {
    hour: i32,
    minute: i32,
    second: i32,
    micro: i32,
    trailing: bool,
}

/// `parse_hh_mm_ss_ff` from `start` up to `end`.
fn parse_clock(s: &Buf, start: usize, end: usize) -> Option<Clock> {
    let mut vals = [0i32; 3];
    let mut p = start;
    let mut has_separator = true;
    for (i, val) in vals.iter_mut().enumerate() {
        let (value, after) = s.digits(p, 2)?;
        *val = value;
        p = after;
        let c = s.at(p);
        p += 1;
        if i == 0 {
            has_separator = c == b':';
        }
        if p >= end {
            let [hour, minute, second] = vals;
            return Some(Clock {
                hour,
                minute,
                second,
                micro: 0,
                trailing: c != 0,
            });
        } else if has_separator && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_separator {
            p -= 1;
        } else {
            return None;
        }
    }
    // Every path out of the loop leaves `p` before `end`, so at least one digit.
    let to_parse = end.saturating_sub(p).min(6);
    let (mut micro, mut p) = s.digits(p, to_parse)?;
    if let Some(scale) = [100_000, 10_000, 1_000, 100, 10].get(to_parse.wrapping_sub(1)) {
        micro *= scale;
    }
    while s.at(p).is_ascii_digit() {
        p += 1;
    }
    let [hour, minute, second] = vals;
    Some(Clock {
        hour,
        minute,
        second,
        micro,
        trailing: s.at(p) != 0,
    })
}

/// `parse_isoformat_time` from `start` to `end`: the clock, and the offset in
/// microseconds when one was given.
fn parse_time(s: &Buf, start: usize, end: usize) -> Option<(Clock, Option<i64>)> {
    let mut tz = start;
    loop {
        if matches!(s.at(tz), b'Z' | b'+' | b'-') {
            break;
        }
        tz += 1;
        if tz >= end {
            break;
        }
    }
    let clock = parse_clock(s, start, tz)?;
    if tz == end {
        return (!clock.trailing).then_some((clock, None));
    }
    if s.at(tz) == b'Z' {
        return (s.at(tz + 1) == 0).then_some((clock, Some(0)));
    }
    let sign = if s.at(tz) == b'-' { -1 } else { 1 };
    let offset = parse_clock(s, tz + 1, end)?;
    if offset.trailing {
        return None;
    }
    let seconds =
        i64::from(offset.hour) * 3600 + i64::from(offset.minute) * 60 + i64::from(offset.second);
    Some((
        clock,
        Some(sign * (seconds * 1_000_000 + i64::from(offset.micro))),
    ))
}

/// Whether `datetime.fromisoformat(text)` returns rather than raises.
pub fn is_isoformat(text: &str) -> bool {
    parse(text).is_some()
}

/// `datetime.fromisoformat(text)` as microseconds since the Unix epoch; a time with
/// no offset is read as UTC, as `list_decisions.is_before` reads it. `None` where
/// Python raises.
pub fn isoformat_micros(text: &str) -> Option<i128> {
    let (year, month, day, hour, minute, second, micro, offset) = parse(text)?;
    let days = days_from_civil(i64::from(year), i64::from(month), i64::from(day));
    let seconds = i128::from(days) * 86_400
        + i128::from(hour) * 3600
        + i128::from(minute) * 60
        + i128::from(second);
    Some(seconds * 1_000_000 + i128::from(micro) - i128::from(offset.unwrap_or(0)))
}

/// Days from 1970-01-01 to a proleptic Gregorian date (Howard Hinnant's algorithm).
pub fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

type Parsed = (i32, i32, i32, i32, i32, i32, i32, Option<i64>);

fn parse(text: &str) -> Option<Parsed> {
    if text.chars().count() < 7 {
        return None;
    }
    let bytes = text.as_bytes();
    let s = Buf(bytes);
    let len = bytes.len();
    let separator = separator_at(&s, len)?;
    let (year, month, day) = parse_date(&s, separator)?;
    let (mut hour, mut minute, mut second, mut micro) = (0, 0, 0, 0);
    let mut utc_offset = None;
    if len > separator {
        // Step over the separator, one UTF-8 character as its lead byte says.
        let lead = s.at(separator);
        let width = if lead & 0x80 == 0 {
            1
        } else {
            match lead & 0xf0 {
                0xe0 => 3,
                0xf0 => 4,
                _ => 2,
            }
        };
        let (clock, offset) = parse_time(&s, separator + width, len)?;
        if offset.is_some_and(|micros| micros.abs() >= 86_400 * 1_000_000) {
            return None;
        }
        utc_offset = offset;
        (hour, minute, second, micro) = (clock.hour, clock.minute, clock.second, clock.micro);
    }
    let valid = (1..=MAX_YEAR).contains(&year)
        && (1..=12).contains(&month)
        && day >= 1
        && day <= days_in_month(year, month)
        && (0..=23).contains(&hour)
        && (0..=59).contains(&minute)
        && (0..=59).contains(&second)
        && (0..=999_999).contains(&micro);
    valid.then_some((year, month, day, hour, minute, second, micro, utc_offset))
}
