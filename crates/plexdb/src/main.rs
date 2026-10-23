//! `plexdb-rs` — the writer's sweep steps that have moved to Rust.
//!
//! `plexdb-rs steps --json` lists them; `plexdb-rs <step>` runs one. A person
//! runs neither directly: `plexdb <step>` and the sweep hand a moved step here.
//!
//! Exit codes: 0 when the step ran, 1 when it failed, 2 for a usage error, and
//! 78 (`EX_CONFIG`) when a setting it needs is absent, which the sweep reports as
//! skipped. A failure prints one `error:` line on stderr.

use std::process::ExitCode;

use plexdb::steps::{self, Failure, STEPS};

/// Agreed with `plexdb/rust.py` `NOT_CONFIGURED`.
const NOT_CONFIGURED: u8 = 78;

fn usage() -> String {
    let names: Vec<&str> = STEPS.iter().map(|step| step.name).collect();
    format!(
        "usage: plexdb-rs steps [--json]\n       plexdb-rs <step>\n\nsteps: {}",
        names.join(", ")
    )
}

fn list_steps(json: bool) -> ExitCode {
    if json {
        match serde_json::to_string(STEPS) {
            Ok(text) => println!("{text}"),
            Err(err) => {
                eprintln!("error: {err}");
                return ExitCode::FAILURE;
            }
        }
    } else {
        for step in STEPS {
            println!("{:>3}  {:<24} {:?}", step.order, step.name, step.policy);
        }
    }
    ExitCode::SUCCESS
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
        ["steps"] => list_steps(false),
        ["steps", "--json"] => list_steps(true),
        ["steps", ..] => {
            eprintln!("error: steps takes only --json\n{}", usage());
            ExitCode::from(2)
        }
        [name, rest @ ..] => {
            let Some(step) = steps::find(name) else {
                eprintln!("error: no command {name:?}\n{}", usage());
                return ExitCode::from(2);
            };
            if !rest.is_empty() {
                eprintln!("error: {name} takes no arguments, got {}", rest.join(" "));
                return ExitCode::from(2);
            }
            match (step.run)() {
                Ok(()) => ExitCode::SUCCESS,
                Err(Failure::Config(message)) => {
                    eprintln!("error: {message}");
                    ExitCode::from(NOT_CONFIGURED)
                }
                Err(Failure::Failed(message)) => {
                    eprintln!("error: {message}");
                    ExitCode::FAILURE
                }
            }
        }
    }
}
