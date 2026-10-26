//! `json.dumps(value, indent=2)` as Python spells it: keys in the order they were
//! built, `ensure_ascii` on (a non-ASCII character becomes `\uXXXX`, an astral one a
//! surrogate pair), and `"key": value` with a newline and two spaces per level.
//! `serde_json` sorts object keys, which would reorder a report people read and
//! scripts compare against the Python output, so a report is built from [`Json`].

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    /// An integer past `i64` that a file already held.
    Uint(u64),
    /// A float, spelled as Python's `repr` spells it.
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    pub fn str(text: impl Into<String>) -> Json {
        Json::Str(text.into())
    }

    pub fn object<const N: usize>(pairs: [(&str, Json); N]) -> Json {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    /// An object from pairs known only at run time.
    pub fn object_from(pairs: Vec<(&str, Json)>) -> Json {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    pub fn opt_str(text: Option<String>) -> Json {
        text.map_or(Json::Null, Json::Str)
    }
}

impl From<&serde_json::Value> for Json {
    /// A parsed file, in the key order it was read in (`serde_json` is built with
    /// `preserve_order`), ready to be written back the way Python writes it.
    fn from(value: &serde_json::Value) -> Json {
        use serde_json::Value;
        match value {
            Value::Null => Json::Null,
            Value::Bool(flag) => Json::Bool(*flag),
            Value::Number(n) => match (n.as_i64(), n.as_u64()) {
                (Some(int), _) => Json::Int(int),
                (None, Some(big)) => Json::Uint(big),
                _ => Json::Float(n.as_f64().unwrap_or(f64::NAN)),
            },
            Value::String(text) => Json::Str(text.clone()),
            Value::Array(items) => Json::Array(items.iter().map(Json::from).collect()),
            Value::Object(map) => Json::Object(
                map.iter()
                    .map(|(key, item)| (key.clone(), Json::from(item)))
                    .collect(),
            ),
        }
    }
}

/// `repr(x)` for a finite float: the shortest digits that read back as `x`, laid out
/// as Python lays them out (fixed from `1e-4` up to `1e16`, exponent form outside).
pub fn float_repr(x: f64) -> String {
    if !x.is_finite() {
        return if x.is_nan() {
            "NaN".into()
        } else if x < 0.0 {
            "-Infinity".into()
        } else {
            "Infinity".into()
        };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    let sci = format!("{:e}", x.abs());
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let decpt = exp.parse::<i32>().unwrap_or(0) + 1;
    let sign = if x < 0.0 { "-" } else { "" };
    let len = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    let body = if decpt > 16 || decpt <= -4 {
        let fraction = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let e = decpt - 1;
        format!(
            "{}{fraction}e{}{:02}",
            &digits[..1],
            if e < 0 { '-' } else { '+' },
            e.abs()
        )
    } else if decpt <= 0 {
        format!(
            "0.{}{digits}",
            "0".repeat(usize::try_from(-decpt).unwrap_or(0))
        )
    } else if decpt >= len {
        format!(
            "{digits}{}.0",
            "0".repeat(usize::try_from(decpt - len).unwrap_or(0))
        )
    } else {
        let at = usize::try_from(decpt).unwrap_or(0);
        format!("{}.{}", &digits[..at], &digits[at..])
    };
    format!("{sign}{body}")
}

fn quote(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (' '..='~').contains(&c) => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

fn write(out: &mut String, value: &Json, depth: usize) {
    let pad = |out: &mut String, depth: usize| {
        out.push('\n');
        out.push_str(&"  ".repeat(depth));
    };
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(n) => {
            let _ = write!(out, "{n}");
        }
        Json::Uint(n) => {
            let _ = write!(out, "{n}");
        }
        Json::Float(x) => out.push_str(&float_repr(*x)),
        Json::Str(text) => quote(out, text),
        Json::Array(items) if items.is_empty() => out.push_str("[]"),
        Json::Array(items) => {
            out.push('[');
            for (ix, item) in items.iter().enumerate() {
                if ix > 0 {
                    out.push(',');
                }
                pad(out, depth + 1);
                write(out, item, depth + 1);
            }
            pad(out, depth);
            out.push(']');
        }
        Json::Object(pairs) if pairs.is_empty() => out.push_str("{}"),
        Json::Object(pairs) => {
            out.push('{');
            for (ix, (key, item)) in pairs.iter().enumerate() {
                if ix > 0 {
                    out.push(',');
                }
                pad(out, depth + 1);
                quote(out, key);
                out.push_str(": ");
                write(out, item, depth + 1);
            }
            pad(out, depth);
            out.push('}');
        }
    }
}

fn write_compact(out: &mut String, value: &Json) {
    match value {
        Json::Array(items) => {
            out.push('[');
            for (ix, item) in items.iter().enumerate() {
                if ix > 0 {
                    out.push(',');
                }
                write_compact(out, item);
            }
            out.push(']');
        }
        Json::Object(pairs) => {
            out.push('{');
            for (ix, (key, item)) in pairs.iter().enumerate() {
                if ix > 0 {
                    out.push(',');
                }
                quote(out, key);
                out.push(':');
                write_compact(out, item);
            }
            out.push('}');
        }
        scalar => write(out, scalar, 0),
    }
}

/// The text `json.dumps(value, separators=(",", ":"))` prints.
pub fn dumps_compact(value: &Json) -> String {
    let mut out = String::new();
    write_compact(&mut out, value);
    out
}

/// The text `json.dumps(value, indent=2)` prints.
pub fn dumps_indent2(value: &Json) -> String {
    let mut out = String::new();
    write(&mut out, value, 0);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_indent_two_with_ascii_escapes() {
        let value = Json::object([
            ("b", Json::Int(1)),
            (
                "a",
                Json::Array(vec![Json::str("é😀"), Json::Null, Json::Bool(true)]),
            ),
            ("empty", Json::Array(vec![])),
        ]);
        assert_eq!(
            dumps_indent2(&value),
            "{\n  \"b\": 1,\n  \"a\": [\n    \"\\u00e9\\ud83d\\ude00\",\n    null,\n    true\n  ],\n  \"empty\": []\n}"
        );
    }

    #[test]
    fn compact_has_no_spaces() {
        let value = Json::object([("a", Json::Array(vec![Json::Int(1), Json::Float(2.0)]))]);
        assert_eq!(dumps_compact(&value), r#"{"a":[1,2.0]}"#);
    }

    #[test]
    fn floats_read_as_python_reprs_them() {
        for (x, repr) in [
            (1.0, "1.0"),
            (0.5, "0.5"),
            (-2.25, "-2.25"),
            (123456789.125, "123456789.125"),
            (1e16, "1e+16"),
            (1.5e300, "1.5e+300"),
            (1e15, "1000000000000000.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.234e-7, "1.234e-07"),
            (0.1 + 0.2, "0.30000000000000004"),
        ] {
            assert_eq!(float_repr(x), repr);
        }
    }
}
