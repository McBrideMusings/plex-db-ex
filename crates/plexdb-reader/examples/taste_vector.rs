//! Print one account's taste vector, so the rollup can be driven and read
//! back without a consumer.
//!
//! ```text
//! cargo run --example taste_vector -- data/plexdb.db 568385169 20
//! ```

use std::process::ExitCode;

use plexdb_reader::Reader;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let (path, account) = match (args.get(1), args.get(2)) {
        (Some(path), Some(account)) => (path, account),
        _ => {
            eprintln!("usage: taste_vector <store.db> <plex_account_id> [top_n]");
            return ExitCode::FAILURE;
        }
    };
    let account: i64 = match account.parse() {
        Ok(value) => value,
        Err(_) => {
            eprintln!("plex_account_id must be an integer, got {account:?}");
            return ExitCode::FAILURE;
        }
    };
    let top_n: usize = args.get(3).and_then(|n| n.parse().ok()).unwrap_or(20);

    let reader = match Reader::open(path) {
        Ok(reader) => reader,
        Err(error) => {
            eprintln!("error: {error}");
            return ExitCode::FAILURE;
        }
    };
    let vector = match reader.taste_vector_for(account) {
        Ok(vector) => vector,
        Err(error) => {
            eprintln!("error: {error}");
            return ExitCode::FAILURE;
        }
    };

    let mut ranked = vector.attributes.clone();
    ranked.sort_by(|a, b| {
        b.weight
            .partial_cmp(&a.weight)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.value.cmp(&b.value))
    });

    println!(
        "account {account}: {} attributes, {} show(s) with no season numbers",
        vector.attributes.len(),
        vector.shows_without_seasons.len()
    );
    let total: f64 = ranked.iter().take(top_n).map(|a| a.weight).sum();
    for attribute in ranked.iter().take(top_n) {
        let share = if total > 0.0 {
            100.0 * attribute.weight / total
        } else {
            0.0
        };
        println!(
            "  {:<24} {:>7.3}  {:>4.1}%  [{}/{}]",
            attribute.value, attribute.weight, share, attribute.namespace, attribute.key
        );
    }
    for show in &vector.shows_without_seasons {
        println!("  no season numbers: {show}");
    }
    ExitCode::SUCCESS
}
