//! `plexdb-rs` — the writer's sweep steps that have moved to Rust.
//!
//! `plexdb-rs <step>` runs one. A person runs it through `plexdb <step>`, and the
//! sweep runs it in the place `plexdb/rust_steps.json` gives it; `plexdb steps`
//! lists both implementations' steps.
//!
//! Exit codes: 0 when the step ran, 1 when it failed, 2 for a usage error, and
//! 78 (`EX_CONFIG`) when a setting it needs is absent, which the sweep reports as
//! skipped. A failure prints one `error:` line on stderr.

use std::process::ExitCode;

use plexdb::steps::{self, Failure, STEPS};

/// Agreed with `plexdb/rust.py` `NOT_CONFIGURED`.
const NOT_CONFIGURED: u8 = 78;

fn usage() -> String {
    let names: Vec<&str> = STEPS.iter().map(|(name, _)| *name).collect();
    format!("usage: plexdb-rs <step>\n\nsteps: {}", names.join(", "))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    match words.as_slice() {
        [] => {
            eprintln!("{}", usage());
            ExitCode::from(2)
        }
        ["-h" | "--help", ..] => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        // The explorer is a server, not a sweep step: the sweep never plans it, and the
        // Python `plexdb explore` stays the one a person runs until its routes have moved.
        ["explore", ..] => match plexdb::explore::run(&args[1..]) {
            Ok(()) => ExitCode::SUCCESS,
            Err(failure) => report(failure),
        },
        [name, ..] => {
            let rest = &args[1..];
            let Some(run) = steps::find(name) else {
                eprintln!("error: no command {name:?}\n{}", usage());
                return ExitCode::from(2);
            };
            match run(rest) {
                Ok(()) => ExitCode::SUCCESS,
                Err(failure) => report(failure),
            }
        }
    }
}

/// Print why a command stopped and give its exit code.
fn report(failure: Failure) -> ExitCode {
    match failure {
        Failure::Usage(message) => {
            eprintln!("error: {message}");
            ExitCode::from(2)
        }
        Failure::Config(message) => {
            eprintln!("error: {message}");
            ExitCode::from(NOT_CONFIGURED)
        }
        Failure::Reported => ExitCode::FAILURE,
        Failure::Failed(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}
