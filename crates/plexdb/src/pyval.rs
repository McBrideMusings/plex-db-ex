//! Python's reading of a JSON value, where a step's rows depend on it: `int(x)`,
//! `str(x)`, truthiness (`a or b`) and `type(x).__name__`.

use serde_json::Value;

/// Python's `bool(value)` of a decoded JSON value.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `type(value).__name__` of a decoded JSON value.
pub fn py_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python's `str(value)` of a decoded JSON value.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        Value::Number(n) => match n.as_f64() {
            Some(f) if n.is_f64() && f.fract() == 0.0 && f.abs() < 1e16 => format!("{f:.1}"),
            _ => n.to_string(),
        },
        other => other.to_string(),
    }
}

/// Python's `int(text)`: surrounding whitespace, a sign, and single underscores
/// between digits.
pub fn py_int_text(text: &str) -> Option<i64> {
    let text = plexdb_store::py_strip(text);
    let (negative, digits) = match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits.chars().all(|c| c.is_ascii_digit() || c == '_')
    {
        return None;
    }
    let value: i64 = digits.replace('_', "").parse().ok()?;
    Some(if negative { -value } else { value })
}

/// Python's `int(value)` of a decoded JSON value, or `None` where it raises.
pub fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(b) => Some(i64::from(*b)),
        Value::Number(n) => n.as_i64().or_else(|| {
            n.as_f64()
                .filter(|f| f.is_finite())
                .map(|f| f.trunc() as i64)
        }),
        Value::String(s) => py_int_text(s),
        _ => None,
    }
}

/// A whole number the source supplied, or `None` where it supplied nothing. Zero is a
/// real answer and stays zero (`mdblist_client._optional_int`).
pub fn optional_int(value: Option<&Value>) -> Option<i64> {
    match value {
        None | Some(Value::Null) => None,
        Some(value) => py_int(value),
    }
}

/// The text the source supplied, or `None` where it supplied none
/// (`mdblist_client._optional_text`).
pub fn optional_text(value: Option<&Value>) -> Option<String> {
    let text = match value {
        None | Some(Value::Null) => return None,
        Some(value) => py_str(value),
    };
    let text = plexdb_store::py_strip(&text);
    (!text.is_empty()).then(|| text.to_string())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn int_reads_what_python_reads() {
        assert_eq!(py_int_text(" 1_000 "), Some(1000));
        assert_eq!(py_int_text("+7"), Some(7));
        assert_eq!(py_int_text("1__0"), None);
        assert_eq!(py_int_text("_1"), None);
        assert_eq!(py_int_text("1.5"), None);
        assert_eq!(py_int(&json!(7.9)), Some(7));
        assert_eq!(py_int(&json!(true)), Some(1));
        assert_eq!(py_int(&json!([1])), None);
    }

    #[test]
    fn optional_keeps_zero_and_drops_nothing_else() {
        assert_eq!(optional_int(Some(&json!(0))), Some(0));
        assert_eq!(optional_int(Some(&json!(null))), None);
        assert_eq!(optional_int(None), None);
        assert_eq!(optional_text(Some(&json!("  "))), None);
        assert_eq!(optional_text(Some(&json!(" Heat "))), Some("Heat".into()));
        assert_eq!(optional_text(Some(&json!(1995))), Some("1995".into()));
    }

    #[test]
    fn str_spells_numbers_and_booleans_as_python_does() {
        assert_eq!(py_str(&json!(27205)), "27205");
        assert_eq!(py_str(&json!(3.0)), "3.0");
        assert_eq!(py_str(&json!(true)), "True");
    }
}
