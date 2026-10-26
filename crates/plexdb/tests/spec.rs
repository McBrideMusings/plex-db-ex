//! The specifications Python and Rust both follow while the writer moves
//! (ADR-0001). Python writes a decisions file and keys lists; the Rust folds read
//! that file and key the same lists. Each fixture under `tests/fixtures/` is run
//! by both suites, so a case the two read differently fails here or there. The
//! sweep plans a Rust step from `plexdb/rust_steps.json`, so this binary must run
//! exactly the steps that file declares.

use std::path::{Path, PathBuf};

use plexdb::list_decisions::canonical_list_url;
use plexdb::pyiso::is_isoformat;
use plexdb::steps::STEPS;
use serde_json::Value;

/// Read at run time, as `plexdb-reader`'s tests do: workers sharing one
/// `CARGO_TARGET_DIR` reuse a binary another checkout built.
fn repo_file(relative: &str) -> Value {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("run the tests through cargo");
    let path: PathBuf = Path::new(&manifest).join("../..").join(relative);
    let text = std::fs::read_to_string(&path).expect("read the file");
    serde_json::from_str(&text).expect("the file is JSON")
}

fn fixture(name: &str) -> Value {
    repo_file(&format!("tests/fixtures/{name}"))
}

fn cases(spec: &Value) -> &Vec<Value> {
    spec["cases"].as_array().expect("a cases list")
}

#[test]
fn every_canonical_list_url_case_gives_the_specified_key() {
    let spec = fixture("canonical_list_url.json");
    let wrong: Vec<String> = cases(&spec)
        .iter()
        .filter_map(|case| {
            let raw = case["raw"].as_str().expect("raw is text");
            let want = case["canonical"].as_str();
            let got = canonical_list_url(raw);
            (got.as_deref() != want)
                .then(|| format!("{}: got {got:?}, want {want:?}", case["name"]))
        })
        .collect();
    assert!(wrong.is_empty(), "{wrong:#?}");
}

#[test]
fn every_decided_at_case_is_accepted_exactly_when_python_accepts_it() {
    let spec = fixture("decided_at.json");
    let wrong: Vec<String> = cases(&spec)
        .iter()
        .filter_map(|case| {
            let text = case["text"].as_str().expect("text is text");
            let want = case["valid"].as_bool().expect("valid is a bool");
            (is_isoformat(text) != want).then(|| format!("{text:?}: want valid={want}"))
        })
        .collect();
    assert!(wrong.is_empty(), "{wrong:#?}");
}

#[test]
fn this_binary_runs_exactly_the_steps_the_sweep_plans_for_it() {
    let declared = repo_file("plexdb/rust_steps.json");
    let mut planned: Vec<&str> = declared["steps"]
        .as_array()
        .expect("a steps list")
        .iter()
        .map(|step| step["name"].as_str().expect("a name"))
        .chain(
            declared["commands"]
                .as_array()
                .expect("a commands list")
                .iter()
                .map(|name| name.as_str().expect("a name")),
        )
        .collect();
    let mut here: Vec<&str> = STEPS.iter().map(|(name, _)| *name).collect();
    planned.sort_unstable();
    here.sort_unstable();
    assert_eq!(planned, here);
}
