//! `plexdb audio status` — what the audio harvester has done, per configured library in list
//! order, and whether a harvest is running now (plex-db-ex-9ak.1, libraries 9ak.5).
//!
//! Read-only: it opens the store read-only and probes the run lock without holding it. With
//! `PLEXDB_AUDIO_LIBRARIES` unset it says the harvester is off. Otherwise it makes the one
//! Plex sections call that turns section titles into ids; if Plex cannot answer, the report
//! carries `plex_error` and every Plex name is listed as unknown.

use plexdb_store::{Config, open_readonly};

use crate::harvest_audio::{self, Counts, EMPTY_EXTRAS_NOTE, LIBRARIES_VAR};
use crate::libraries::{Library, LibraryKind, names_from_env};
use crate::plex::{PlexSource, Section};
use crate::pyjson::{Json, dumps_indent2};
use crate::sources::Sources;
use crate::staleness::Now;
use crate::steps::{Failure, failure};

pub const NAME: &str = "audio";

fn counts_json(library: &Library, counts: &Counts) -> Json {
    let count = |n: usize| Json::Int(n as i64);
    let mut fields = vec![("library", Json::str(&library.name))];
    match &library.kind {
        LibraryKind::Section(id) => {
            fields.push(("kind", Json::str("section")));
            fields.push(("section_id", Json::str(id)));
        }
        LibraryKind::Extras => fields.push(("kind", Json::str("extras"))),
    }
    fields.extend([
        ("parts", count(counts.parts)),
        ("done", count(counts.done)),
        ("pending", count(counts.pending)),
        ("failed", count(counts.failed)),
        ("unreachable", count(counts.unreachable)),
        ("excluded", count(counts.excluded)),
    ]);
    if counts.parts == 0 {
        fields.push((
            "note",
            Json::str(match library.kind {
                LibraryKind::Section(_) => harvest_audio::EMPTY_SECTION_NOTE,
                LibraryKind::Extras => EMPTY_EXTRAS_NOTE,
            }),
        ));
    }
    Json::object_from(fields)
}

/// The report `plexdb audio status` prints, for the listed `names`, Plex's `sections` and
/// the error that made them unavailable, if any.
pub fn report(
    store: &std::path::Path,
    names: &[String],
    sections: &[Section],
    plex_error: Option<&str>,
    now_secs: i64,
) -> Result<Json, Failure> {
    let running = Json::Bool(harvest_audio::harvest_running(store));
    if names.is_empty() {
        return Ok(Json::object([
            ("enabled", Json::Bool(false)),
            (
                "status",
                Json::str(format!("off: {LIBRARIES_VAR} is not set")),
            ),
            ("running", running),
            ("libraries", Json::Array(Vec::new())),
            ("unknown", Json::Array(Vec::new())),
        ]));
    }
    let resolved = {
        let conn = open_readonly(store).map_err(failure)?;
        harvest_audio::resolve_libraries(&conn, names, sections)
            .map_err(|err| Failure::Failed(format!("store: {err}")))?
    };
    let per_library = harvest_audio::status(store, &resolved, now_secs).map_err(failure)?;
    let mut fields = vec![
        ("enabled", Json::Bool(true)),
        ("status", Json::str("on")),
        ("running", running),
        (
            "libraries",
            Json::Array(
                per_library
                    .iter()
                    .map(|(library, counts)| counts_json(library, counts))
                    .collect(),
            ),
        ),
        (
            "unknown",
            Json::Array(resolved.unknown.iter().map(Json::str).collect()),
        ),
    ];
    if let Some(error) = plex_error {
        fields.push(("plex_error", Json::str(error)));
    }
    Ok(Json::object_from(fields))
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
            let names = names_from_env(LIBRARIES_VAR);
            let (sections, plex_error) = if names.is_empty() {
                (Vec::new(), None)
            } else {
                match Sources::from_env()
                    .plex("resolve the names in PLEXDB_AUDIO_LIBRARIES")
                    .and_then(|plex| plex.sections())
                {
                    Ok(sections) => (sections, None),
                    Err(err) => (Vec::new(), Some(err.to_string())),
                }
            };
            let json = report(
                &config.store_path,
                &names,
                &sections,
                plex_error.as_deref(),
                Now::read().secs(),
            )?;
            println!("{}", dumps_indent2(&json));
            Ok(())
        }
        _ => Err(Failure::Usage(format!(
            "usage: plexdb audio status, got {}",
            args.join(" ")
        ))),
    }
}
