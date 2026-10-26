//! `plexdb search <phrase> (--pool ID | --pool-file PATH) [--not PHRASE] [--weight W]
//! [--temperature T] [--draw N [--seed S]]` — rank Shots against a Phrase (ADR-0024,
//! ADR-0026) (`plexdb/commands/search.py`).
//!
//! The search is `plexdb_reader::Reader::search_drawn`, the reader crate's one
//! implementation, so this command, `plexdb-read search` and a Rust consumer cannot
//! disagree. This module resolves where the files are from the environment and prints
//! the JSON the reader produces.

use std::io::Read;

use plexdb_reader::{PhraseCache, Reader, Search, Url};
use plexdb_store::Config;

use crate::frames::FrameConfig;
use crate::steps::{Failure, failure};

pub const NAME: &str = "search";

const USAGE: &str = "usage: search <phrase> [--pool ITEM_ID]... [--pool-file PATH] \
[--not PHRASE]... [--weight W] [--temperature T] [--draw N] [--seed S]";

#[derive(Default)]
struct Options {
    phrase: Option<String>,
    pool: Vec<String>,
    pool_file: Option<String>,
    negatives: Vec<String>,
    weight: Option<f32>,
    temperature: Option<f32>,
    draw: Option<usize>,
    seed: Option<u64>,
}

fn number<T: std::str::FromStr>(flag: &str, raw: &str) -> Result<T, Failure> {
    raw.parse()
        .map_err(|_| Failure::Usage(format!("{flag}: invalid value: {raw:?}")))
}

fn parse(args: &[String]) -> Result<Options, Failure> {
    let mut options = Options::default();
    let mut tokens = args.iter();
    while let Some(token) = tokens.next() {
        if !token.starts_with("--") {
            if options.phrase.replace(token.clone()).is_some() {
                return Err(Failure::Usage(format!("unexpected argument {token}")));
            }
            continue;
        }
        // Every flag takes one value, whatever it starts with, so a negative may begin
        // with `--` and a repeated flag builds a list.
        let value = tokens
            .next()
            .ok_or_else(|| Failure::Usage(format!("{token} needs a value")))?;
        match token.as_str() {
            "--pool" => options.pool.push(value.clone()),
            "--pool-file" => options.pool_file = Some(value.clone()),
            "--not" => options.negatives.push(value.clone()),
            "--weight" => options.weight = Some(number(token, value)?),
            "--temperature" => options.temperature = Some(number(token, value)?),
            "--draw" => options.draw = Some(number(token, value)?),
            "--seed" => options.seed = Some(number(token, value)?),
            other => return Err(Failure::Usage(format!("unknown option {other}"))),
        }
    }
    Ok(options)
}

fn pool(options: &Options) -> Result<Vec<String>, Failure> {
    let mut ids = options.pool.clone();
    if let Some(path) = &options.pool_file {
        let mut text = String::new();
        if path == "-" {
            std::io::stdin()
                .read_to_string(&mut text)
                .map_err(|err| Failure::Failed(err.to_string()))?;
        } else {
            text = std::fs::read_to_string(path)
                .map_err(|err| Failure::Failed(format!("{path}: {err}")))?;
        }
        ids.extend(
            text.lines()
                .map(str::trim)
                .filter(|line| !line.is_empty())
                .map(str::to_string),
        );
    }
    if ids.is_empty() {
        return Err(Failure::Config(
            "name the titles to search with --pool or --pool-file".into(),
        ));
    }
    Ok(ids)
}

pub fn run(args: &[String]) -> Result<(), Failure> {
    let options = parse(args)?;
    let phrase = options
        .phrase
        .as_deref()
        .ok_or_else(|| Failure::Usage(USAGE.into()))?;
    if options.seed.is_some() && options.draw.is_none() {
        return Err(Failure::Usage("--seed needs --draw".into()));
    }
    for text in std::iter::once(phrase).chain(options.negatives.iter().map(String::as_str)) {
        if text.starts_with("--") {
            return Err(Failure::Usage(
                "a phrase or negative cannot start with --".into(),
            ));
        }
    }
    let store = Config::from_env().map_err(failure)?;
    let frames = FrameConfig::from_env().map_err(failure)?;
    let frames_path = frames.require_frames_path().map_err(failure)?;
    if frames.encoder_url.is_empty() {
        return Err(Failure::Config(
            "PLEXDB_FRAME_ENCODER_URL is not set, so a new phrase cannot be embedded".into(),
        ));
    }
    if frames.model.is_empty() {
        return Err(Failure::Config(
            "PLEXDB_FRAME_MODEL is not set, so a new phrase cannot be embedded".into(),
        ));
    }
    let ids = pool(&options)?;
    let cache = PhraseCache {
        path: frames
            .phrase_cache_path()
            .unwrap_or_else(|| frames_path.with_file_name("phrases.db")),
        encoder_url: Url::parse(&frames.encoder_url)
            .map_err(|err| Failure::Config(format!("encoder URL {}: {err}", frames.encoder_url)))?,
        request_model: frames.model.clone(),
    };
    let snapshot = store.snapshot_path.unwrap_or(store.store_path);
    let reader = Reader::open(snapshot, Some(frames_path.as_path()), Some(cache))
        .map_err(|err| Failure::Failed(err.to_string()))?;
    let pool: Vec<&str> = ids.iter().map(String::as_str).collect();
    let negatives: Vec<&str> = options.negatives.iter().map(String::as_str).collect();
    let found = reader
        .search_drawn(
            &Search {
                pool: &pool,
                phrase,
                negatives: &negatives,
                weight: options.weight,
                temperature: options.temperature,
            },
            options.draw,
            options.seed.unwrap_or(0),
        )
        .map_err(|err| Failure::Failed(err.to_string()))?;
    let text = serde_json::to_string(&found).map_err(|err| Failure::Failed(err.to_string()))?;
    println!("{text}");
    Ok(())
}
