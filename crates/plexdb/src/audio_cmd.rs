//! `plexdb audio status` — what the audio harvester has done, per configured library, and
//! whether a harvest is running now (plex-db-ex-9ak.1).
//!
//! Read-only: it opens the store read-only and probes the run lock without holding it. With
//! `PLEXDB_AUDIO_SECTIONS` unset it says the harvester is off.

use plexdb_store::Config;

use crate::harvest_audio::{self, Counts, sections_from_env};
use crate::pyjson::{Json, dumps_indent2};
use crate::staleness::Now;
use crate::steps::{Failure, failure};

pub const NAME: &str = "audio";

fn counts_json(section: &str, counts: &Counts) -> Json {
    let count = |n: usize| Json::Int(n as i64);
    let mut fields = vec![
        ("section_id", Json::str(section)),
        ("parts", count(counts.parts)),
        ("done", count(counts.done)),
        ("pending", count(counts.pending)),
        ("failed", count(counts.failed)),
        ("unreachable", count(counts.unreachable)),
        ("excluded", count(counts.excluded)),
    ];
    if counts.parts == 0 {
        fields.push(("note", Json::str(harvest_audio::EMPTY_SECTION_NOTE)));
    }
    Json::object_from(fields)
}

/// The report `plexdb audio status` prints.
pub fn report(
    store: &std::path::Path,
    sections: &[String],
    now_secs: i64,
) -> Result<Json, Failure> {
    let running = Json::Bool(harvest_audio::harvest_running(store));
    if sections.is_empty() {
        return Ok(Json::object([
            ("enabled", Json::Bool(false)),
            (
                "status",
                Json::str(format!("off: {} is not set", harvest_audio::SECTIONS_VAR)),
            ),
            ("running", running),
            ("sections", Json::Array(Vec::new())),
        ]));
    }
    let per_section = harvest_audio::status(store, sections, now_secs).map_err(failure)?;
    Ok(Json::object([
        ("enabled", Json::Bool(true)),
        ("status", Json::str("on")),
        ("running", running),
        (
            "sections",
            Json::Array(
                per_section
                    .iter()
                    .map(|(section, counts)| counts_json(section, counts))
                    .collect(),
            ),
        ),
    ]))
}

/// `plexdb audio status`.
pub fn run(args: &[String]) -> Result<(), Failure> {
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["status"] => {
            let config = Config::from_env().map_err(failure)?;
            let json = report(&config.store_path, &sections_from_env(), Now::read().secs())?;
            println!("{}", dumps_indent2(&json));
            Ok(())
        }
        _ => Err(Failure::Usage(format!(
            "usage: plexdb audio status, got {}",
            args.join(" ")
        ))),
    }
}
