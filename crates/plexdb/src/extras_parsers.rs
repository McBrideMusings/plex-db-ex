//! Filename parsers for extras libraries (ADR-0030).
//!
//! A library's `parsers` is a list of rules. A rule's `match` is a regex with named groups,
//! run on a file's stem; its `set` maps a field name (or `source_url`) to a template over
//! those groups, such as `https://example.com/watch?v={id}`. Nothing here knows what a
//! library holds or where a file came from: the rules in `extras.json` say so.

use std::collections::BTreeSet;

use regex::Regex;
use serde_json::{Map, Value};

use crate::extras_declaration::template_fields;

/// One rule as declared: the regex source and the `set` pairs in file order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parser {
    pub pattern: String,
    pub set: Vec<(String, String)>,
}

/// The rules in `value`, or the first rule they break. `fields` are the library's declared
/// field names; `source_url` is always allowed.
pub fn parse(value: &Value, at: &str, fields: &[&str]) -> Result<Vec<Parser>, String> {
    let Value::Array(rules) = value else {
        return Err(format!("{at} has parsers that is not a list"));
    };
    let mut parsers = Vec::new();
    for (index, rule) in rules.iter().enumerate() {
        let at = format!("{at} parser {index}");
        let Some(pattern) = rule.get("match").and_then(Value::as_str) else {
            return Err(format!("{at} needs a text match"));
        };
        let regex = Regex::new(pattern)
            .map_err(|err| format!("{at} has a match that is not a regex: {err}"))?;
        let groups: BTreeSet<&str> = regex.capture_names().flatten().collect();
        let Some(set) = rule.get("set").and_then(Value::as_object) else {
            return Err(format!("{at} needs a set object"));
        };
        if set.is_empty() {
            return Err(format!("{at} has an empty set"));
        }
        let mut pairs = Vec::new();
        for (key, template) in set {
            if key != "source_url" && !fields.contains(&key.as_str()) {
                return Err(format!(
                    "{at} sets {key:?}, which is neither a declared field nor source_url"
                ));
            }
            let Some(template) = template.as_str() else {
                return Err(format!("{at} set {key:?} is not text"));
            };
            let used = template_fields(template)
                .map_err(|why| format!("{at} set {key:?} template {why}"))?;
            if let Some(name) = used.iter().find(|name| !groups.contains(name.as_str())) {
                return Err(format!(
                    "{at} set {key:?} names {{{name}}}, which is not a group of its match"
                ));
            }
            pairs.push((key.clone(), template.to_string()));
        }
        parsers.push(Parser {
            pattern: pattern.to_string(),
            set: pairs,
        });
    }
    Ok(parsers)
}

/// The JSON text stored in `extra_library.parsers`; `None` for no rules.
pub fn to_json(parsers: &[Parser]) -> Option<String> {
    if parsers.is_empty() {
        return None;
    }
    let rules: Vec<Value> = parsers
        .iter()
        .map(|parser| {
            let set: Map<String, Value> = parser
                .set
                .iter()
                .map(|(key, template)| (key.clone(), Value::String(template.clone())))
                .collect();
            let mut rule = Map::new();
            rule.insert("match".into(), Value::String(parser.pattern.clone()));
            rule.insert("set".into(), Value::Object(set));
            Value::Object(rule)
        })
        .collect();
    Some(Value::Array(rules).to_string())
}

/// The rules stored by [`to_json`].
pub fn from_json(text: Option<&str>) -> Result<Vec<Parser>, String> {
    let Some(text) = text else {
        return Ok(Vec::new());
    };
    let value: Value =
        serde_json::from_str(text).map_err(|err| format!("stored parsers are not JSON: {err}"))?;
    let Value::Array(rules) = &value else {
        return Err("stored parsers are not a list".into());
    };
    let mut parsers = Vec::new();
    for rule in rules {
        let (Some(pattern), Some(set)) = (
            rule.get("match").and_then(Value::as_str),
            rule.get("set").and_then(Value::as_object),
        ) else {
            return Err("a stored parser lacks its match or set".into());
        };
        let mut pairs = Vec::new();
        for (key, template) in set {
            let Some(template) = template.as_str() else {
                return Err(format!("a stored parser's set {key:?} is not text"));
            };
            pairs.push((key.clone(), template.to_string()));
        }
        parsers.push(Parser {
            pattern: pattern.to_string(),
            set: pairs,
        });
    }
    Ok(parsers)
}

/// A rule ready to run.
pub struct Compiled {
    regex: Regex,
    set: Vec<(String, String)>,
}

/// Compile stored rules. A rule that no longer compiles is skipped: `fold-extras` checked it
/// before storing it.
pub fn compile(parsers: &[Parser]) -> Vec<Compiled> {
    parsers
        .iter()
        .filter_map(|parser| {
            Some(Compiled {
                regex: Regex::new(&parser.pattern).ok()?,
                set: parser.set.clone(),
            })
        })
        .collect()
}

/// The `(field, value)` pairs every matching rule proposes for `stem`. A rule listed first
/// wins a field a later rule also sets. A template whose group did not take part in the
/// match proposes nothing.
pub fn propose(rules: &[Compiled], stem: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for rule in rules {
        let Some(caps) = rule.regex.captures(stem) else {
            continue;
        };
        for (key, template) in &rule.set {
            if out.iter().any(|(seen, _)| seen == key) {
                continue;
            }
            if let Some(value) = fill(template, &caps) {
                out.push((key.clone(), value));
            }
        }
    }
    out
}

fn fill(template: &str, caps: &regex::Captures) -> Option<String> {
    let mut out = String::new();
    let mut name: Option<String> = None;
    for c in template.chars() {
        match (c, &mut name) {
            ('{', None) => name = Some(String::new()),
            ('}', Some(_)) => out.push_str(caps.name(&name.take()?)?.as_str()),
            (c, Some(name)) => name.push(c),
            (c, None) => out.push(c),
        }
    }
    Some(out)
}
