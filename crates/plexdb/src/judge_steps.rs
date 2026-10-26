//! `judge-keyword-pairs` and `judge-keyword-roles`: the two steps that ask Jev about the
//! stored keywords (`plexdb/commands/judge_keyword_pairs.py`,
//! `plexdb/commands/judge_keyword_roles.py`).
//!
//! The embedding server and Jev are optional services behind adapters (ADR-0004). A pair
//! or a keyword is judged once and kept, so a skipped or failed run costs nothing but the
//! rows it would have added.

use std::io::Write;

use plexdb_store::Config;

use crate::args::{self, Spec};
use crate::decisions::{py_repr, thousands};
use crate::embed::LlamaSwapEmbedder;
use crate::gated::{config, credential, open};
use crate::jev::LiveJev;
use crate::roles::{self, RoleStats};
use crate::steps::Failure;
use crate::synonyms::{self, EMBEDDING_CACHE_FILE, JudgeStats};

pub const PAIRS: &str = "judge-keyword-pairs";
pub const ROLES: &str = "judge-keyword-roles";

const EMBED_URL_VAR: &str = "LLAMA_BROKER_BASE_URL";
const JEV_KEY_VAR: &str = "TYPESAFE_API_KEY";

const SPEC: Spec = Spec {
    options: &["--limit"],
    flags: &[],
    positionals: 0,
};

/// `--limit N`, a whole number of at least 1, as the Python `positive_int` read it.
fn limit(step: &str, args: &[String]) -> Result<Option<usize>, Failure> {
    let usage = |message: String| Failure::Usage(format!("{step}: {message}"));
    let parsed = args::parse(args, &SPEC).map_err(&usage)?;
    let Some(raw) = parsed.option("--limit") else {
        return Ok(None);
    };
    let Some(value) = crate::pyval::py_int_text(raw) else {
        return Err(usage(format!(
            "argument --limit: invalid positive_int value: {}",
            py_repr(raw)
        )));
    };
    if value < 1 {
        return Err(usage(format!(
            "argument --limit: must be at least 1, got {value}"
        )));
    }
    Ok(Some(usize::try_from(value).unwrap_or(usize::MAX)))
}

fn printer() -> impl FnMut(&str) {
    |line| {
        println!("{line}");
        let _ = std::io::stdout().flush();
    }
}

pub fn pairs_report(stats: &JudgeStats) -> String {
    format!(
        "keyword pairs: {} keyword(s) stored, {} examined, {} pair(s) proposed, \
         {} already judged, {} judged now, {} unjudgeable",
        thousands(stats.keywords_stored),
        thousands(stats.keywords_examined),
        thousands(stats.pairs_proposed),
        thousands(stats.pairs_already_judged),
        thousands(stats.pairs_judged),
        thousands(stats.pairs_unjudgeable)
    )
}

pub fn roles_report(stats: &RoleStats) -> String {
    format!(
        "keyword roles: {} keyword(s) stored, {} already judged, {} judged now, \
         {} unjudgeable, {} row(s) written",
        thousands(stats.keywords_stored),
        thousands(stats.keywords_already_judged),
        thousands(stats.keywords_judged),
        thousands(stats.keywords_unjudgeable),
        thousands(stats.rows_written)
    )
}

pub fn run_pairs(args: &[String]) -> Result<(), Failure> {
    let limit = limit(PAIRS, args)?;
    let config: Config = config()?;
    let base_url = credential(EMBED_URL_VAR, "embed keywords")?;
    let api_key = credential(JEV_KEY_VAR, "judge keyword pairs")?;
    let mut store = open(&config)?;
    let cache = config.store_path.with_file_name(EMBEDDING_CACHE_FILE);
    let stats = synonyms::find_synonym_pairs(
        &mut store.conn,
        &LlamaSwapEmbedder::new(&base_url),
        &LiveJev::new(&api_key),
        Some(&cache),
        limit,
        &mut printer(),
    )
    .map_err(|err| Failure::Failed(err.to_string()))?;
    println!("{}", pairs_report(&stats));
    Ok(())
}

pub fn run_roles(args: &[String]) -> Result<(), Failure> {
    let limit = limit(ROLES, args)?;
    let config: Config = config()?;
    let api_key = credential(JEV_KEY_VAR, "judge keyword roles")?;
    let mut store = open(&config)?;
    let stats = roles::judge_keyword_roles(
        &mut store.conn,
        &LiveJev::new(&api_key),
        limit,
        &mut printer(),
    )
    .map_err(|err| Failure::Failed(err.to_string()))?;
    println!("{}", roles_report(&stats));
    Ok(())
}
