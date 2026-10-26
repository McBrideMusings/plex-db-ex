//! The specifications Python and Rust both follow while the writer moves
//! (ADR-0001). Python writes a decisions file and keys lists; the Rust folds read
//! that file and key the same lists. Each fixture under `tests/fixtures/` is run
//! by both suites, so a case the two read differently fails here or there. The
//! sweep plans a Rust step from `plexdb/rust_steps.json`, so this binary must run
//! exactly the steps that file declares.

use std::path::{Path, PathBuf};

use plexdb::identity::{canonical_path, derive_item_id, fnv1a_64};
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

fn roots(case: &Value) -> Vec<String> {
    case["source_roots"]
        .as_array()
        .expect("source_roots is a list")
        .iter()
        .map(|root| root.as_str().expect("a root").to_string())
        .collect()
}

fn pairs(case: &Value) -> Vec<(String, String)> {
    case["external_ids"]
        .as_array()
        .expect("external_ids is a list")
        .iter()
        .map(|pair| {
            (
                pair[0].as_str().expect("a namespace").to_string(),
                pair[1].as_str().expect("a value").to_string(),
            )
        })
        .collect()
}

#[test]
fn every_item_id_case_derives_the_published_id() {
    let spec = fixture("item_id.json");
    let section = |name: &str| {
        spec[name]
            .as_array()
            .unwrap_or_else(|| panic!("{name} is a list"))
            .clone()
    };
    let mut wrong = Vec::new();
    for case in section("item_id_cases") {
        let got = derive_item_id(
            &pairs(&case),
            case["canonical_path"].as_str().expect("a path"),
        );
        if got != case["expect"].as_str().expect("expect") {
            wrong.push(format!("{}: got {got}", case["name"]));
        }
    }
    for case in section("canonical_path_cases") {
        let got = canonical_path(case["raw"].as_str().expect("raw"), &roots(&case));
        if got != case["expect"].as_str().expect("expect") {
            wrong.push(format!("{}: got {got:?}", case["name"]));
        }
    }
    for case in section("end_to_end_cases") {
        let canonical = canonical_path(case["raw"].as_str().expect("raw"), &roots(&case));
        let got = derive_item_id(&pairs(&case), &canonical);
        if got != case["expect"].as_str().expect("expect") {
            wrong.push(format!("{}: got {got}", case["name"]));
        }
    }
    assert!(wrong.is_empty(), "{wrong:#?}");
}

#[test]
fn the_path_hash_is_fnv1a_as_published() {
    assert_eq!(fnv1a_64(""), 0xCBF2_9CE4_8422_2325);
    assert_eq!(fnv1a_64("a"), 0xAF63_DC4C_8601_EC8C);
    assert_eq!(fnv1a_64("foobar"), 0x8594_4171_F739_67E8);
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
