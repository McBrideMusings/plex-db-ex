//! `tests/fixtures/keyword_normalization.json` is the rule's specification: Python's
//! `normalize_keyword` for every raw spelling the store held and every English
//! dictionary word ending in "s". The Rust normalizer returns exactly those values.

use plexdb_keywords::normalize_keyword;

#[test]
fn matches_pythons_normalize_keyword_on_every_golden_case() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/keyword_normalization.json"
    );
    let text = std::fs::read_to_string(path).expect("golden file");
    let golden: serde_json::Value = serde_json::from_str(&text).expect("golden file is JSON");
    let cases = golden["cases"].as_array().expect("cases");
    assert!(
        cases.len() > 50_000,
        "golden file holds {} cases",
        cases.len()
    );
    let mut mismatches = Vec::new();
    for case in cases {
        let surface = case[0].as_str().expect("surface");
        let expected = case[1].as_str().expect("stored form");
        let got = normalize_keyword(surface);
        if got != expected {
            mismatches.push(format!("{surface:?}: python {expected:?}, rust {got:?}"));
        }
    }
    assert!(
        mismatches.is_empty(),
        "{} mismatch(es) of {}:\n{}",
        mismatches.len(),
        cases.len(),
        mismatches
            .iter()
            .take(40)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
