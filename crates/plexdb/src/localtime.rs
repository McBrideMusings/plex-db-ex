//! The process's wall-clock time, spelled the way Python's `datetime.now().astimezone()`
//! spells it, because `frames.db` stores those strings (`indexed_at`, `queued_at`,
//! `since`) and `PLEXDB_FRAME_HOURS` is read against the process's TZ.

use std::time::{SystemTime, UNIX_EPOCH};

/// A wall-clock moment with its UTC offset.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Local {
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
    pub micro: u32,
    pub offset_secs: i32,
}

impl Local {
    /// Now, in the zone `TZ` (or the system) names.
    pub fn now() -> Local {
        let since = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        Local::from_unix(since.as_secs() as i64, since.subsec_micros())
    }

    pub fn from_unix(secs: i64, micro: u32) -> Local {
        let t: libc::time_t = secs as libc::time_t;
        // SAFETY: `localtime_r` writes only into the `tm` we hand it.
        let tm = unsafe {
            let mut tm: libc::tm = std::mem::zeroed();
            libc::localtime_r(&t, &mut tm);
            tm
        };
        Local {
            year: tm.tm_year + 1900,
            month: (tm.tm_mon + 1) as u32,
            day: tm.tm_mday as u32,
            hour: tm.tm_hour as u32,
            minute: tm.tm_min as u32,
            second: tm.tm_sec as u32,
            micro,
            offset_secs: tm.tm_gmtoff as i32,
        }
    }

    fn offset(&self) -> String {
        let sign = if self.offset_secs < 0 { '-' } else { '+' };
        let abs = self.offset_secs.abs();
        let (h, m, s) = (abs / 3600, abs % 3600 / 60, abs % 60);
        if s != 0 {
            format!("{sign}{h:02}:{m:02}:{s:02}")
        } else {
            format!("{sign}{h:02}:{m:02}")
        }
    }

    fn date_time(&self) -> String {
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        )
    }

    /// `datetime.isoformat()`: microseconds only when they are not zero.
    pub fn isoformat(&self) -> String {
        if self.micro == 0 {
            return self.isoformat_seconds();
        }
        format!("{}.{:06}{}", self.date_time(), self.micro, self.offset())
    }

    /// `datetime.isoformat(timespec="seconds")`.
    pub fn isoformat_seconds(&self) -> String {
        format!("{}{}", self.date_time(), self.offset())
    }

    /// Microseconds since local midnight.
    pub fn micros_of_day(&self) -> u64 {
        ((u64::from(self.hour) * 60 + u64::from(self.minute)) * 60 + u64::from(self.second))
            * 1_000_000
            + u64::from(self.micro)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn isoformat_drops_zero_microseconds_and_keeps_the_offset() {
        let at = Local {
            year: 2026,
            month: 10,
            day: 26,
            hour: 3,
            minute: 4,
            second: 5,
            micro: 0,
            offset_secs: -4 * 3600,
        };
        assert_eq!(at.isoformat(), "2026-10-26T03:04:05-04:00");
        let at = Local {
            micro: 120,
            offset_secs: 0,
            ..at
        };
        assert_eq!(at.isoformat(), "2026-10-26T03:04:05.000120+00:00");
        assert_eq!(at.isoformat_seconds(), "2026-10-26T03:04:05+00:00");
    }
}
