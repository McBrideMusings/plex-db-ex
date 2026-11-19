//! `plexdb-read` — the store API as a command line: one route path in, its JSON
//! reply on stdout, a message on stderr and a non-zero exit on any error.
//!
//! The path is read by [`Call::parse`], the same table Plex DB-EX's store routes
//! use, so the command line and the HTTP API cannot disagree (ADR-0031).
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
[--encoder-url U] [--encoder-model M] <path>[?query] [--body JSON | --body-file P|-]

<path> is a store API route, the same path Plex DB-EX answers (docs/api.md):
  /items?type=movie&include=enrichment&limit=500
  /items/imdb:tt0113277/edges?type=tmdb_similar&direction=in
  /accounts/1/taste
  /keywords/heist/pairs
With --body or --body-file the request is a POST (`/searches`); without, a GET.
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

fn run(raw: Vec<String>) -> Result<String, Failure> {
    let args = Args::parse(raw)?;
    let call = call(&args)?;
    let reader = open(&args)?;
    Ok(reader.answer(&call)?)
}

/// The path, query and body as the [`Call`] they name.
fn call(args: &Args) -> Result<Call, Failure> {
    let target = args.arg(0, "path")?;
    if let Some(extra) = args.positional.get(1) {
        return Err(Failure::Usage(format!("unexpected argument {extra}")));
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let body = match (args.flag("--body"), args.flag("--body-file")) {
        (Some(_), Some(_)) => {
            return Err(Failure::Usage(
                "--body and --body-file are one or the other".into(),
            ));
        }
        (Some(text), None) => Some(text.to_string()),
        (None, Some("-")) => {
            let mut text = String::new();
            std::io::stdin().read_to_string(&mut text)?;
            Some(text)
        }
        (None, Some(path)) => Some(std::fs::read_to_string(path)?),
        (None, None) => None,
    };
    let body: Option<serde_json::Value> = body
        .map(|text| serde_json::from_str(&text))
        .transpose()
        .map_err(|e| Failure::Usage(format!("the body is not JSON: {e}")))?;
    let method = if body.is_some() { "POST" } else { "GET" };
    Ok(Call::parse(method, path, query, body.as_ref())?)
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
