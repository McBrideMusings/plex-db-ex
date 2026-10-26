//! How long a `Retry-After` header asks a client to wait, read the way the Python clients
//! read it (`_retry_after`): a number of seconds, else 60, and no wait over [`MAX_WAIT`].

/// A `Retry-After` longer than this fails the request rather than stalling a sweep.
pub const MAX_WAIT: f64 = 120.0;

/// The seconds `raw` asks for; 60 when it is not a number.
pub fn requested_seconds(raw: &str) -> f64 {
    raw.trim()
        .parse::<f64>()
        .ok()
        .filter(|wait| !wait.is_nan())
        .unwrap_or(60.0)
}

/// The wait to honour: at least a second, or `Err(requested)` when it is over [`MAX_WAIT`].
pub fn honoured_seconds(raw: &str) -> Result<f64, f64> {
    let wait = requested_seconds(raw);
    if wait > MAX_WAIT {
        return Err(wait);
    }
    Ok(wait.max(1.0))
}

/// Sleep for `seconds`, when positive.
pub fn pause(seconds: f64) {
    if seconds > 0.0 {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
}

/// The pause after every request: the number `var` holds, else `default`.
pub fn pause_from_env(var: &str, default: f64) -> f64 {
    std::env::var(var)
        .ok()
        .and_then(|raw| raw.trim().parse::<f64>().ok())
        .filter(|pause| pause.is_finite() && *pause >= 0.0)
        .unwrap_or(default)
}

/// The server `var` names with no trailing slash, else `default`.
pub fn base_from_env(var: &str, default: &str) -> String {
    std::env::var(var)
        .ok()
        .map(|raw| raw.trim().trim_end_matches('/').to_string())
        .filter(|raw| !raw.is_empty())
        .unwrap_or_else(|| default.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_wait_is_at_least_a_second_and_never_over_the_cap() {
        assert_eq!(honoured_seconds("0"), Ok(1.0));
        assert_eq!(honoured_seconds("junk"), Ok(60.0));
        assert_eq!(honoured_seconds("30"), Ok(30.0));
        assert_eq!(honoured_seconds("121"), Err(121.0));
    }
}
