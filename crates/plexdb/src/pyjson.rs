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

    pub fn opt_str(text: Option<String>) -> Json {
        text.map_or(Json::Null, Json::Str)
    }
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
}
