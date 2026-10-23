//! The slice of Python's `urllib.parse` a list URL goes through: `urlparse`'s
//! scheme, hostname and path, and `unquote`. `canonical_list_url` builds a stored
//! key from these, and `plexdb list-decide` (still Python) writes the entries the
//! fold resolves, so both must split a URL the same way;
//! `tests/fixtures/canonical_list_url.json` holds them to it.

/// What `urlparse` returns that a list URL needs.
#[derive(Debug, PartialEq, Eq)]
pub struct Parsed {
    pub scheme: String,
    /// `ParseResult.hostname`: lowercased, without userinfo or port; `None` when empty.
    pub hostname: Option<String>,
    /// `ParseResult.path`, without `;params` after its last `/`.
    pub path: String,
}

const C0_CONTROL_OR_SPACE: &[char] = &[
    '\x00', '\x01', '\x02', '\x03', '\x04', '\x05', '\x06', '\x07', '\x08', '\t', '\n', '\x0b',
    '\x0c', '\r', '\x0e', '\x0f', '\x10', '\x11', '\x12', '\x13', '\x14', '\x15', '\x16', '\x17',
    '\x18', '\x19', '\x1a', '\x1b', '\x1c', '\x1d', '\x1e', '\x1f', ' ',
];

/// Characters whose NFKC form holds one of `/?#@:` (Unicode 16.0.0, the version
/// Python's `unicodedata` carries). `_checknetloc` refuses a netloc holding one.
const NFKC_DELIMITERS: &[char] = &[
    '\u{2047}', '\u{2048}', '\u{2049}', '\u{2100}', '\u{2101}', '\u{2105}', '\u{2106}', '\u{2A74}',
    '\u{FE13}', '\u{FE16}', '\u{FE55}', '\u{FE56}', '\u{FE5F}', '\u{FE6B}', '\u{FF03}', '\u{FF0F}',
    '\u{FF1A}', '\u{FF1F}', '\u{FF20}',
];

fn is_scheme_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')
}

/// `urllib.parse.urlparse(raw)`, or `None` where it raises `ValueError`.
pub fn urlparse(raw: &str) -> Option<Parsed> {
    let mut url: String = raw
        .trim_start_matches(C0_CONTROL_OR_SPACE)
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut scheme = String::new();
    if let Some(colon) = url.find(':') {
        let head = &url[..colon];
        let first = head.chars().next();
        if colon > 0
            && first.is_some_and(|c| c.is_ascii_alphabetic())
            && head.chars().all(is_scheme_char)
        {
            scheme = head.to_ascii_lowercase();
            url = url[colon + 1..].to_string();
        }
    }
    let mut netloc = String::new();
    if url.starts_with("//") {
        let end = url[2..]
            .find(['/', '?', '#'])
            .map_or(url.len(), |at| at + 2);
        netloc = url[2..end].to_string();
        url = url[end..].to_string();
        let (open, close) = (netloc.contains('['), netloc.contains(']'));
        if open != close {
            return None;
        }
        if open && close && !bracketed_netloc_ok(&netloc) {
            return None;
        }
    }
    if let Some(at) = url.find('#') {
        url.truncate(at);
    }
    if let Some(at) = url.find('?') {
        url.truncate(at);
    }
    if !netloc.is_ascii() && netloc.contains(NFKC_DELIMITERS) {
        return None;
    }
    // `uses_params`: of the schemes a list URL can carry, http and https split
    // `;params` off the last path segment.
    if matches!(scheme.as_str(), "" | "http" | "https") && url.contains(';') {
        let from = url.rfind('/').unwrap_or(0);
        if let Some(at) = url[from..].find(';') {
            url.truncate(from + at);
        }
    }
    Some(Parsed {
        scheme,
        hostname: hostname(&netloc),
        path: url,
    })
}

fn after_userinfo(netloc: &str) -> &str {
    netloc.rsplit_once('@').map_or(netloc, |(_, host)| host)
}

/// `_check_bracketed_netloc`.
fn bracketed_netloc_ok(netloc: &str) -> bool {
    let hostinfo = after_userinfo(netloc);
    let host = match hostinfo.split_once('[') {
        Some((before, bracketed)) => {
            if !before.is_empty() {
                return false;
            }
            let (host, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            if !port.is_empty() && !port.starts_with(':') {
                return false;
            }
            host
        }
        None => hostinfo.split_once(':').map_or(hostinfo, |(host, _)| host),
    };
    bracketed_host_ok(host)
}

/// `_check_bracketed_host`: an IPvFuture literal or an IPv6 address.
fn bracketed_host_ok(host: &str) -> bool {
    if let Some(rest) = host.strip_prefix(['v', 'V']) {
        let Some((hex, tail)) = rest.split_once('.') else {
            return false;
        };
        return !hex.is_empty()
            && hex.chars().all(|c| c.is_ascii_hexdigit())
            && !tail.is_empty()
            && !tail.contains('\n');
    }
    // `ipaddress.ip_address` takes a scope id after `%`.
    let address = match host.split_once('%') {
        Some((address, scope)) if !scope.is_empty() && !scope.contains('%') => address,
        Some(_) => return false,
        None => host,
    };
    address.parse::<std::net::Ipv6Addr>().is_ok()
}

/// `_NetlocResultMixinStr._hostinfo` and `.hostname`.
fn hostname(netloc: &str) -> Option<String> {
    let hostinfo = after_userinfo(netloc);
    let host = match hostinfo.split_once('[') {
        Some((_, bracketed)) => bracketed
            .split_once(']')
            .map_or(bracketed, |(host, _)| host),
        None => hostinfo.split_once(':').map_or(hostinfo, |(host, _)| host),
    };
    if host.is_empty() {
        return None;
    }
    // A scoped IPv6 address keeps its zone's case.
    Some(match host.split_once('%') {
        Some((address, zone)) => format!("{}%{zone}", address.to_lowercase()),
        None => host.to_lowercase(),
    })
}

fn hex_value(byte: u8) -> Option<u8> {
    (byte as char).to_digit(16).map(|d| d as u8)
}

/// Percent-decoding of an ASCII run to bytes (`_unquote_impl`).
fn unquote_to_bytes(ascii: &str) -> Vec<u8> {
    let bytes = ascii.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && let (Some(&hi), Some(&lo)) = (bytes.get(i + 1), bytes.get(i + 2))
            && let (Some(hi), Some(lo)) = (hex_value(hi), hex_value(lo))
        {
            out.push(hi << 4 | lo);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    out
}

/// `urllib.parse.unquote`: each run of ASCII is percent-decoded and read as UTF-8
/// with invalid bytes replaced; anything else is kept as it is.
pub fn unquote(text: &str) -> String {
    if !text.contains('%') {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut run_start = 0;
    let mut in_ascii = text.chars().next().is_some_and(|c| c.is_ascii());
    let flush = |run: &str, ascii: bool, out: &mut String| {
        if ascii {
            out.push_str(&String::from_utf8_lossy(&unquote_to_bytes(run)));
        } else {
            out.push_str(run);
        }
    };
    for (at, c) in text.char_indices() {
        if c.is_ascii() != in_ascii {
            flush(&text[run_start..at], in_ascii, &mut out);
            run_start = at;
            in_ascii = c.is_ascii();
        }
    }
    flush(&text[run_start..], in_ascii, &mut out);
    out
}
