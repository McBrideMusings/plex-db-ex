//! simplemma 2.0.0's English lemmatizer, `simplemma.lemmatize(word, lang="en")`.
//!
//! The dictionary is simplemma's own `en.plzma`, embedded unchanged. Only the
//! strategies that can answer for English are here, in the order
//! `DefaultStrategy._search_pipeline` tries them: dictionary lookup, English
//! clitics (`'s`, `n't`, ...), hyphen removal, the English suffix rules. The
//! rest (prefix, affix and morpheme decomposition, the Turkish apostrophe
//! boundary, French and Italian proclitics) are keyed to other languages, and
//! the default strategy runs without its greedy round. The golden file
//! `tests/fixtures/keyword_normalization.json` pins the result to Python's.

use std::collections::HashMap;
use std::sync::OnceLock;

use unicode_normalization::UnicodeNormalization;
use unicode_normalization::char::canonical_combining_class;

static EN_PLZMA: &[u8] = include_bytes!("../dictionary/en.plzma");

const MAGIC: &[u8] = b"SMFC1";
const REVERSE_FLAG: u8 = 0x01;
const SAME_AS_PREV: u8 = 254;
const LITERAL_VALUE: u8 = 255;

/// English clitics, longest first (`CLITIC_LANGS["en"]` sorted by length).
const CLITICS: [&str; 7] = ["n't", "'re", "'ve", "'ll", "'m", "'s", "'d"];
const IRREGULAR_CONTRACTIONS: [&str; 3] = ["can't", "won't", "shan't"];
const MAX_CLITICS: usize = 2;
const MIN_STEM_LEN: usize = 1;

const IES_ENDING: [&str; 1] = ["cies"];
const S_ENDING: [&str; 8] = [
    "doms", "isms", "ists", "ments", "nces", "ships", "tions", "ums",
];

fn read_varint(data: &[u8], pos: &mut usize) -> usize {
    let mut result = 0usize;
    let mut shift = 0;
    loop {
        let byte = data[*pos];
        *pos += 1;
        result |= usize::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return result;
        }
        shift += 7;
    }
}

/// `frontcode._decode_stream`: sorted, front-coded keys; values suffix-edited
/// against their own key.
fn decode(data: &[u8]) -> HashMap<String, String> {
    assert!(
        data.starts_with(MAGIC),
        "en.plzma is not a front-coded stream"
    );
    let mut pos = MAGIC.len();
    let reverse = data[pos] & REVERSE_FLAG != 0;
    pos += 1;
    let count = read_varint(data, &mut pos);
    let mut map = HashMap::with_capacity(count);
    let mut prev_key: Vec<u8> = Vec::new();
    let mut prev_value: Vec<u8> = Vec::new();
    while pos < data.len() {
        let shared = read_varint(data, &mut pos);
        let suffix = read_varint(data, &mut pos);
        let mut key = prev_key[..shared].to_vec();
        key.extend_from_slice(&data[pos..pos + suffix]);
        pos += suffix;
        let trim = data[pos];
        pos += 1;
        let value = if trim == SAME_AS_PREV {
            prev_value.clone()
        } else {
            let length = read_varint(data, &mut pos);
            let tail = &data[pos..pos + length];
            pos += length;
            if trim == LITERAL_VALUE {
                tail.to_vec()
            } else {
                let mut value = key[..key.len() - usize::from(trim)].to_vec();
                value.extend_from_slice(tail);
                value
            }
        };
        let (k, v) = if reverse {
            (
                key.iter().rev().copied().collect::<Vec<_>>(),
                value.iter().rev().copied().collect::<Vec<_>>(),
            )
        } else {
            (key.clone(), value.clone())
        };
        map.insert(
            String::from_utf8(k).expect("en.plzma keys are UTF-8"),
            String::from_utf8(v).expect("en.plzma values are UTF-8"),
        );
        prev_key = key;
        prev_value = value;
    }
    assert_eq!(map.len(), count, "en.plzma is truncated or corrupt");
    map
}

fn dictionary() -> &'static HashMap<String, String> {
    static DICTIONARY: OnceLock<HashMap<String, String>> = OnceLock::new();
    DICTIONARY.get_or_init(|| {
        let mut raw = Vec::new();
        lzma_rs::xz_decompress(&mut std::io::Cursor::new(EN_PLZMA), &mut raw)
            .expect("en.plzma is xz");
        decode(&raw)
    })
}

/// Python's `str.capitalize()`: the first character titlecased, the rest
/// lowercased.
fn capitalize(text: &str) -> String {
    let mut chars = text.chars();
    let Some(first) = chars.next() else {
        return String::new();
    };
    let mut out = String::new();
    match first {
        // Titlecase digraphs: DŽ/Dž/dž, LJ/Lj/lj, NJ/Nj/nj, DZ/Dz/dz.
        '\u{1c4}'..='\u{1c6}' => out.push('\u{1c5}'),
        '\u{1c7}'..='\u{1c9}' => out.push('\u{1c8}'),
        '\u{1ca}'..='\u{1cc}' => out.push('\u{1cb}'),
        '\u{1f1}'..='\u{1f3}' => out.push('\u{1f2}'),
        // Titlecase is the letter itself; uppercase would be Mtavruli.
        '\u{10d0}'..='\u{10fa}' | '\u{10fd}'..='\u{10ff}' => out.push(first),
        'ß' => out.push_str("Ss"),
        '\u{fb00}' => out.push_str("Ff"),
        '\u{fb01}' => out.push_str("Fi"),
        '\u{fb02}' => out.push_str("Fl"),
        '\u{fb03}' => out.push_str("Ffi"),
        '\u{fb04}' => out.push_str("Ffl"),
        '\u{fb05}' | '\u{fb06}' => out.push_str("St"),
        other => out.extend(other.to_uppercase()),
    }
    out.push_str(&chars.as_str().to_lowercase());
    out
}

fn first_is_upper(text: &str) -> bool {
    text.chars().next().is_some_and(char::is_uppercase)
}

/// `utils.normalize_apostrophes`.
fn normalize_apostrophes(text: &str) -> String {
    text.replace(['\u{2019}', '\u{2bc}'], "'")
}

fn has_apostrophe(text: &str) -> bool {
    text.contains(['\'', '\u{2019}', '\u{2bc}'])
}

/// `utils.apostrophe_variants`: the token, then its straight, curly and
/// modifier-letter spellings, without repeats.
fn apostrophe_variants(token: &str) -> Vec<String> {
    let straight = normalize_apostrophes(token);
    if !straight.contains('\'') {
        return vec![token.to_string()];
    }
    let mut variants = vec![token.to_string()];
    for candidate in [
        straight.clone(),
        straight.replace('\'', "\u{2019}"),
        straight.replace('\'', "\u{2bc}"),
    ] {
        if !variants.contains(&candidate) {
            variants.push(candidate);
        }
    }
    variants
}

fn get(token: &str) -> Option<&'static str> {
    dictionary().get(token).map(String::as_str)
}

fn cased(token: &str) -> String {
    if first_is_upper(token) {
        token.to_lowercase()
    } else {
        capitalize(token)
    }
}

/// `DictionaryLookupStrategy.get_lemma`.
fn dictionary_lookup(token: &str) -> Option<&'static str> {
    if let Some(found) = get(token) {
        return Some(found);
    }
    if let Some(found) = get(&cased(token)) {
        return Some(found);
    }
    if !has_apostrophe(token) {
        return None;
    }
    for variant in apostrophe_variants(token).iter().skip(1) {
        if let Some(found) = get(variant) {
            return Some(found);
        }
        if let Some(found) = get(&cased(variant)) {
            return Some(found);
        }
    }
    None
}

/// `utils.strip_diacritics`: drop combining marks, then recompose.
fn strip_diacritics(word: &str) -> String {
    word.nfd()
        .filter(|c| canonical_combining_class(*c) == 0)
        .nfc()
        .collect()
}

fn stem_lookup(stem: &str) -> Option<&'static str> {
    if let Some(found) = dictionary_lookup(stem) {
        return Some(found);
    }
    let folded = strip_diacritics(stem);
    if folded == stem {
        return None;
    }
    dictionary_lookup(&folded)
}

/// `CliticDecompositionStrategy._enclitic_lemma` for English.
fn clitic_lemma(token: &str) -> Option<&'static str> {
    let token = normalize_apostrophes(token);
    if IRREGULAR_CONTRACTIONS.contains(&token.to_lowercase().as_str()) {
        return None;
    }
    let mut stem = token.as_str();
    for _ in 0..MAX_CLITICS {
        let stripped = CLITICS.iter().find_map(|suffix| {
            let rest = stem.strip_suffix(suffix)?;
            (rest.chars().count() >= MIN_STEM_LEN).then_some(rest)
        })?;
        stem = stripped;
        if let Some(lemma) = stem_lookup(stem) {
            return Some(lemma);
        }
    }
    None
}

/// `HyphenRemovalStrategy.get_lemma`.
fn hyphen_lemma(token: &str) -> Option<String> {
    let is_hyphen = |c: char| c == '-' || c == '_';
    if !token.contains(is_hyphen) {
        return None;
    }
    // `HYPHEN_REGEX.split` keeps each hyphen as its own part.
    let mut parts: Vec<&str> = Vec::new();
    let mut start = 0;
    for (index, c) in token.char_indices() {
        if is_hyphen(c) {
            parts.push(&token[start..index]);
            parts.push(&token[index..index + 1]);
            start = index + 1;
        }
    }
    parts.push(&token[start..]);
    let last = *parts.last()?;
    if last.is_empty() {
        return None;
    }
    let joined: String = parts
        .iter()
        .filter(|part| **part != "-" && **part != "_")
        .copied()
        .collect::<String>()
        .to_lowercase();
    let candidate = if first_is_upper(token) {
        capitalize(&joined)
    } else {
        joined
    };
    if let Some(lemma) = dictionary_lookup(&candidate) {
        return Some(lemma.to_string());
    }
    let tail = dictionary_lookup(last)?;
    Some(format!("{}{tail}", parts[..parts.len() - 1].concat()))
}

/// `defaultrules.en.apply_en`.
fn rules_lemma(token: &str) -> Option<String> {
    let chars = token.chars().count();
    let drop_last =
        |count: usize| -> String { token.chars().take(chars - count).collect::<String>() };
    if chars > 7 && IES_ENDING.iter().any(|end| token.ends_with(end)) {
        return Some(format!("{}y", drop_last(3)));
    }
    if S_ENDING.iter().any(|end| token.ends_with(end)) {
        return Some(drop_last(1));
    }
    if token.ends_with("ized") {
        return Some(format!("{}ize", drop_last(4)));
    }
    if token.ends_with("erves") {
        return Some(drop_last(1));
    }
    None
}

/// `simplemma.lemmatize(token, lang="en")`.
pub fn lemmatize(token: &str) -> String {
    let token: String = token.nfc().collect();
    // `or` in the search pipeline: an empty answer falls through.
    let non_empty = |found: Option<String>| found.filter(|lemma| !lemma.is_empty());
    non_empty(dictionary_lookup(&token).map(str::to_string))
        .or_else(|| non_empty(clitic_lemma(&token).map(str::to_string)))
        .or_else(|| non_empty(hyphen_lemma(&token)))
        .or_else(|| non_empty(rules_lemma(&token)))
        .unwrap_or(token)
}
