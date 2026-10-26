//! The command-line arguments a step takes, read the way `argparse` reads them for the
//! shapes this binary has: `--flag`, `--option VALUE` or `--option=VALUE`, and
//! positionals. An option may be given once; the last occurrence wins.

use std::collections::HashMap;

/// What a step accepts.
pub struct Spec {
    /// Options that take a value.
    pub options: &'static [&'static str],
    /// Flags that take none.
    pub flags: &'static [&'static str],
    /// How many positionals, exactly.
    pub positionals: usize,
}

#[derive(Debug, Default)]
pub struct Parsed {
    options: HashMap<String, String>,
    flags: Vec<String>,
    pub positionals: Vec<String>,
}

impl Parsed {
    pub fn option(&self, name: &str) -> Option<&str> {
        self.options.get(name).map(String::as_str)
    }

    pub fn flag(&self, name: &str) -> bool {
        self.flags.iter().any(|f| f == name)
    }
}

/// Parse `args` against `spec`; the error is the sentence `argparse` would print after
/// `error:`.
pub fn parse(args: &[String], spec: &Spec) -> Result<Parsed, String> {
    let mut parsed = Parsed::default();
    let mut rest = args.iter();
    let mut only_positionals = false;
    while let Some(arg) = rest.next() {
        if only_positionals || !arg.starts_with('-') || arg == "-" {
            parsed.positionals.push(arg.clone());
            continue;
        }
        if arg == "--" {
            only_positionals = true;
            continue;
        }
        let (name, inline) = match arg.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_string())),
            _ => (arg.as_str(), None),
        };
        if spec.flags.contains(&name) {
            if inline.is_some() {
                return Err(format!("argument {name}: ignored explicit argument"));
            }
            parsed.flags.push(name.to_string());
        } else if spec.options.contains(&name) {
            let value = match inline {
                Some(value) => value,
                None => rest
                    .next()
                    .cloned()
                    .ok_or_else(|| format!("argument {name}: expected one argument"))?,
            };
            parsed.options.insert(name.to_string(), value);
        } else {
            return Err(format!("unrecognized arguments: {arg}"));
        }
    }
    if parsed.positionals.len() < spec.positionals {
        return Err("the following arguments are required".into());
    }
    if parsed.positionals.len() > spec.positionals {
        return Err(format!(
            "unrecognized arguments: {}",
            parsed.positionals[spec.positionals..].join(" ")
        ));
    }
    Ok(parsed)
}

/// `--name N` as `argparse`'s `type=int`.
pub fn int_option(parsed: &Parsed, name: &str) -> Result<Option<i64>, String> {
    parsed
        .option(name)
        .map(|raw| {
            crate::pyval::py_int_text(raw).ok_or_else(|| {
                format!(
                    "argument {name}: invalid int value: {}",
                    crate::decisions::py_repr(raw)
                )
            })
        })
        .transpose()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SPEC: Spec = Spec {
        options: &["--limit"],
        flags: &["--rewipe"],
        positionals: 1,
    };

    fn words(text: &str) -> Vec<String> {
        text.split_whitespace().map(str::to_string).collect()
    }

    #[test]
    fn options_flags_and_positionals_mix_in_any_order() {
        let parsed = parse(&words("--rewipe url --limit=3"), &SPEC).expect("parses");
        assert!(parsed.flag("--rewipe"));
        assert_eq!(parsed.option("--limit"), Some("3"));
        assert_eq!(parsed.positionals, ["url"]);
        assert_eq!(int_option(&parsed, "--limit"), Ok(Some(3)));
    }

    #[test]
    fn a_missing_value_or_an_unknown_flag_is_refused() {
        assert!(parse(&words("url --limit"), &SPEC).is_err());
        assert!(parse(&words("url --nope"), &SPEC).is_err());
        assert!(parse(&words("a b"), &SPEC).is_err());
        assert!(parse(&[], &SPEC).is_err());
    }

    #[test]
    fn a_non_integer_is_named() {
        let parsed = parse(&words("url --limit x"), &SPEC).expect("parses");
        assert_eq!(
            int_option(&parsed, "--limit"),
            Err("argument --limit: invalid int value: 'x'".to_string())
        );
    }
}
