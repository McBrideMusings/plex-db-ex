//! Print each named keyword's display name, or that it has none, so
//! `display_name` can be driven and read back without a consumer.
//!
//! ```text
//! cargo run --example display_name -- data/plexdb.db "action film" heist
//! ```

use std::process::ExitCode;

use plexdb_reader::Reader;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let Some(path) = args.get(1) else {
        eprintln!("usage: display_name <store.db> <keyword>...");
        return ExitCode::FAILURE;
    };
    let reader = match Reader::open(path, None, None) {
        Ok(reader) => reader,
        Err(err) => {
            eprintln!("error: {err}");
            return ExitCode::FAILURE;
        }
    };
    for keyword in &args[2..] {
        match reader.display_name(keyword) {
            Ok(Some(name)) => println!("{keyword:?} -> {name:?}"),
            Ok(None) => println!("{keyword:?} -> no display name"),
            Err(err) => {
                eprintln!("error: {err}");
                return ExitCode::FAILURE;
            }
        }
    }
    ExitCode::SUCCESS
}
