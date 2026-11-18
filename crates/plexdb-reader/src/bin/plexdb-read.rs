//! `plexdb-read` — the reader crate's accessors as a command line: one
//! subcommand per accessor, JSON on stdout, a message on stderr and a non-zero
//! exit on any error.
//!
//! Each subcommand is a [`Call`], the same value Plex DB-EX's `/api/store/*` routes
//! answer, so the command line and the HTTP API cannot disagree (ADR-0031).
//!
//! Where the files are comes from a flag, else the environment:
//!
//! | flag | environment |
//! |---|---|
//! | `--snapshot` | `PLEXDB_SNAPSHOT_PATH` |
//! | `--frames` | `PLEXDB_FRAMES_DIR` (the file is `frames.db` inside it) |
//! | `--phrase-cache` | `PLEXDB_PHRASE_CACHE` |
//! | `--encoder-url` | `PLEXDB_FRAME_ENCODER_URL` |
//! | `--encoder-model` | `PLEXDB_FRAME_MODEL` |

use std::collections::BTreeMap;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

use plexdb_reader::{Call, PhraseCache, Reader, Url};

const USAGE: &str = "usage: plexdb-read [--snapshot P] [--frames P] [--phrase-cache P] \
[--encoder-url U] [--encoder-model M] <subcommand> [args]

subcommands:
  enrichment-for <item_id> <namespace>
  enrichment-for-many <namespace> <item_id>...
  audio-facts-for <item_id>
  audio-facts-for-many <item_id>...
  keyword-for-surface <surface>
  surfaces-for-keyword <keyword>
  display-name <keyword>
  edges-from <item_id> <edge_type>
  edges-to <item_id> <edge_type>
  tag-neighbours <kind> <tag>
  collections-for <item_id>
  taste-vector-for <account_id>
  watched-units-for <account_id>
  watched-units
  pooled-taste-vector
  cuts <item_id>
  chapters <item_id>
  capabilities
  search <phrase> (--pool <item_id> [--pool ...] | --pool-file <path|->)
         [--not <phrase> [--not ...]] [--weight w] [--temperature T] [--draw n --seed s]

--pool and --not take one value each and repeat: --pool a --pool b --not x --not y.
Flags may come before or after the phrase.
--help prints this and exits 0.";

enum Failure {
    Help,
    Usage(String),
    Run(String),
}

impl<E: std::fmt::Display> From<E> for Failure {
    fn from(err: E) -> Self {
        Failure::Run(err.to_string())
    }
}

#[derive(Default)]
struct Args {
    positional: Vec<String>,
    flags: BTreeMap<String, Vec<String>>,
}

impl Args {
    fn parse(raw: Vec<String>) -> Result<Self, Failure> {
        let mut args = Args::default();
        let mut tokens = raw.into_iter().peekable();
        while let Some(token) = tokens.next() {
            if token == "--help" {
                return Err(Failure::Help);
            }
            if !token.starts_with("--") {
                args.positional.push(token);
                continue;
            }
            // Every flag takes exactly one value, whatever it starts with, so a
            // repeated flag builds a list and a negative may begin with `--`.
            let value = tokens
                .next()
                .ok_or_else(|| Failure::Usage(format!("{token} needs a value")))?;
            args.flags.entry(token).or_default().push(value);
        }
        Ok(args)
    }

    fn flag(&self, name: &str) -> Option<&str> {
        self.flags
            .get(name)
            .and_then(|v| v.last())
            .map(String::as_str)
    }

    fn list(&self, name: &str) -> Vec<&str> {
        self.flags
            .get(name)
            .map(|v| v.iter().map(String::as_str).collect())
            .unwrap_or_default()
    }

    fn number<T: std::str::FromStr>(&self, name: &str) -> Result<Option<T>, Failure> {
        self.flag(name)
            .map(|v| {
                v.parse::<T>()
                    .map_err(|_| Failure::Usage(format!("{name} is not a number: {v}")))
            })
            .transpose()
    }

    /// The `n`th positional, named for the usage error.
    fn arg(&self, n: usize, name: &str) -> Result<&str, Failure> {
        self.positional
            .get(n)
            .map(String::as_str)
            .ok_or_else(|| Failure::Usage(format!("missing <{name}>")))
    }
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty())
}

fn open(args: &Args) -> Result<Reader, Failure> {
    let snapshot = args
        .flag("--snapshot")
        .map(str::to_string)
        .or_else(|| env("PLEXDB_SNAPSHOT_PATH"))
        .ok_or_else(|| {
            Failure::Usage("no snapshot: pass --snapshot or set PLEXDB_SNAPSHOT_PATH".into())
        })?;
    let frames = args
        .flag("--frames")
        .map(PathBuf::from)
        .or_else(|| env("PLEXDB_FRAMES_DIR").map(|d| PathBuf::from(d).join("frames.db")));
    let cache = args
        .flag("--phrase-cache")
        .map(PathBuf::from)
        .or_else(|| env("PLEXDB_PHRASE_CACHE").map(PathBuf::from));
    let encoder = args
        .flag("--encoder-url")
        .map(str::to_string)
        .or_else(|| env("PLEXDB_FRAME_ENCODER_URL"));
    let model = args
        .flag("--encoder-model")
        .map(str::to_string)
        .or_else(|| env("PLEXDB_FRAME_MODEL"));
    let phrases = match (cache, encoder) {
        (Some(path), Some(url)) => Some(PhraseCache {
            path,
            encoder_url: Url::parse(&url)
                .map_err(|e| Failure::Usage(format!("encoder URL {url}: {e}")))?,
            request_model: model.unwrap_or_default(),
        }),
        _ => None,
    };
    Ok(Reader::open(snapshot, frames.as_deref(), phrases)?)
}

fn pool_ids(args: &Args) -> Result<Vec<String>, Failure> {
    let mut ids: Vec<String> = args
        .list("--pool")
        .into_iter()
        .map(str::to_string)
        .collect();
    if let Some(path) = args.flag("--pool-file") {
        let mut text = String::new();
        if path == "-" {
            std::io::stdin().read_to_string(&mut text)?;
        } else {
            text = std::fs::read_to_string(path)?;
        }
        ids.extend(
            text.lines()
                .map(str::trim)
                .filter(|l| !l.is_empty())
                .map(str::to_string),
        );
    }
    Ok(ids)
}

fn run(raw: Vec<String>) -> Result<String, Failure> {
    let args = Args::parse(raw)?;
    let call = call(&args)?;
    let reader = open(&args)?;
    Ok(reader.answer(&call)?)
}

/// The subcommand and its arguments as the [`Call`] it names.
fn call(args: &Args) -> Result<Call, Failure> {
    let text = |n: usize, name: &str| args.arg(n, name).map(str::to_string);
    let rest = |from: usize| -> Vec<String> {
        args.positional[from.min(args.positional.len())..].to_vec()
    };
    Ok(match args.arg(0, "subcommand")? {
        "enrichment-for" => Call::EnrichmentFor {
            item_id: text(1, "item_id")?,
            namespace: text(2, "namespace")?,
        },
        "enrichment-for-many" => Call::EnrichmentForMany {
            namespace: text(1, "namespace")?,
            item_ids: rest(2),
        },
        "audio-facts-for" => Call::AudioFactsFor {
            item_id: text(1, "item_id")?,
        },
        "audio-facts-for-many" => Call::AudioFactsForMany { item_ids: rest(1) },
        "keyword-for-surface" => Call::KeywordForSurface {
            surface: text(1, "surface")?,
        },
        "surfaces-for-keyword" => Call::SurfacesForKeyword {
            keyword: text(1, "keyword")?,
        },
        "display-name" => Call::DisplayName {
            keyword: text(1, "keyword")?,
        },
        "edges-from" => Call::EdgesFrom {
            item_id: text(1, "item_id")?,
            edge_type: text(2, "edge_type")?,
        },
        "edges-to" => Call::EdgesTo {
            item_id: text(1, "item_id")?,
            edge_type: text(2, "edge_type")?,
        },
        "tag-neighbours" => Call::TagNeighbours {
            kind: text(1, "kind")?,
            tag: text(2, "tag")?,
        },
        "collections-for" => Call::CollectionsFor {
            item_id: text(1, "item_id")?,
        },
        "taste-vector-for" => Call::TasteVectorFor {
            account: account(args)?,
        },
        "watched-units-for" => Call::WatchedUnitsFor {
            account: account(args)?,
        },
        "watched-units" => Call::WatchedUnits,
        "pooled-taste-vector" => Call::PooledTasteVector,
        "cuts" => Call::Cuts {
            item_id: text(1, "item_id")?,
        },
        "chapters" => Call::Chapters {
            item_id: text(1, "item_id")?,
        },
        "capabilities" => Call::Capabilities,
        "search" => search(args)?,
        other => return Err(Failure::Usage(format!("unknown subcommand {other}"))),
    })
}

fn account(args: &Args) -> Result<i64, Failure> {
    let raw = args.arg(1, "account_id")?;
    raw.parse()
        .map_err(|_| Failure::Usage(format!("<account_id> is not a number: {raw}")))
}

fn search(args: &Args) -> Result<Call, Failure> {
    if let Some(extra) = args.positional.get(2) {
        return Err(Failure::Usage(format!(
            "unexpected argument {extra}: --pool and --not take one value each, repeat the flag"
        )));
    }
    let draw = args.number::<usize>("--draw")?;
    let seed = args.number::<u64>("--seed")?;
    if draw.is_none() && seed.is_some() {
        return Err(Failure::Usage("--seed needs --draw".into()));
    }
    Ok(Call::Search {
        pool: pool_ids(args)?,
        phrase: args.arg(1, "phrase")?.to_string(),
        negatives: args.list("--not").into_iter().map(str::to_string).collect(),
        weight: args.number("--weight")?,
        temperature: args.number("--temperature")?,
        draw,
        seed: seed.unwrap_or(0),
    })
}

fn main() -> ExitCode {
    match run(std::env::args().skip(1).collect()) {
        Ok(value) => {
            println!("{value}");
            ExitCode::SUCCESS
        }
        Err(Failure::Help) => {
            println!("{USAGE}");
            ExitCode::SUCCESS
        }
        Err(Failure::Usage(message)) => {
            eprintln!("error: {message}\n{USAGE}");
            ExitCode::from(2)
        }
        Err(Failure::Run(message)) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}
