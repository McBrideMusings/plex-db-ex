//! `plexdb frames migrate | status | queue <item_id> [--by WHO]` — the Frame index's
//! operator surface (ADR-0024) (`plexdb/commands/frames.py`).
//!
//! Each creates `frames.db` and applies its migrations when needed, and none writes
//! `plexdb.db`. `probe-encoder` embeds one image and one phrase through the configured
//! encoder (ADR-0025) and touches neither file. `quant-agreement <phrase>…` compares int8
//! against fp16 rankings over `frames-float.db` (`frame_quant`).

use plexdb_store::{Config, StoreError};
use rusqlite::{Connection, OpenFlags};

use crate::frame_encoder::{
    Embedded, Encoder, EncoderClient, EncoderError, PROBE_IMAGE, PROBE_PHRASE, configured_reachable,
};
use crate::frame_quant;
use crate::frames::{
    self, FrameConfig, StatusInput, decoder_report, migrate_configured, open_frames, queue,
    section_titles,
};
use crate::localtime::Local;
use crate::pyjson::{Json, dumps_indent2};
use crate::steps::Failure;

pub const NAME: &str = "frames";

fn failure(err: StoreError) -> Failure {
    match err {
        StoreError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}

fn open_readonly(path: &std::path::Path) -> Result<Connection, StoreError> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|err| StoreError::Store(format!("cannot open {}: {err}", path.display())))?;
    conn.busy_timeout(std::time::Duration::from_millis(
        plexdb_store::BUSY_TIMEOUT_MS,
    ))?;
    Ok(conn)
}

pub(crate) fn which(program: &str) -> bool {
    if program.contains('/') {
        return std::path::Path::new(program).is_file();
    }
    std::env::var_os("PATH")
        .is_some_and(|paths| std::env::split_paths(&paths).any(|dir| dir.join(program).is_file()))
}

pub fn run(args: &[String]) -> Result<(), Failure> {
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    match words.as_slice() {
        ["migrate"] => migrate().map_err(failure),
        ["status"] => status().map_err(failure),
        ["queue", item_id] => queue_title(item_id, "cli").map_err(failure),
        ["queue", item_id, "--by", by] | ["queue", "--by", by, item_id] => {
            queue_title(item_id, by).map_err(failure)
        }
        ["probe-encoder"] => probe_encoder(),
        ["quant-agreement", rest @ ..] if !rest.is_empty() => quant_agreement(rest),
        _ => Err(Failure::Usage(
            "usage: frames migrate | status | queue <item_id> [--by WHO] | probe-encoder \
             | quant-agreement <phrase>... [--top K]..."
                .into(),
        )),
    }
}

fn encoder_failure(err: EncoderError) -> Failure {
    match err {
        EncoderError::Config(message) => Failure::Config(message),
        other => Failure::Failed(other.to_string()),
    }
}

fn call(answer: &Embedded) -> Vec<(&'static str, Json)> {
    // Python's `round()` rounds halves to even.
    let ms = |seconds: f64| Json::Int((seconds * 1000.0).round_ties_even() as i64);
    vec![
        (
            "dims",
            Json::Int(answer.vectors.first().map_or(0, Vec::len) as i64),
        ),
        ("first_byte_ms", ms(answer.first_byte_s)),
        ("latency_ms", ms(answer.total_s)),
    ]
}

fn probe_encoder() -> Result<(), Failure> {
    let config = FrameConfig::from_env().map_err(failure)?;
    let encoder = EncoderClient::from_config(&config).map_err(encoder_failure)?;
    // The phrase goes first and again last: the first call pays any model load, the
    // last is warm, and the difference is the cold-start wait.
    let phrase = [PROBE_PHRASE.to_string()];
    let cold = encoder.timed_texts(&phrase).map_err(encoder_failure)?;
    let image = encoder
        .timed_images(&[PROBE_IMAGE.to_vec()])
        .map_err(encoder_failure)?;
    let text = encoder.timed_texts(&phrase).map_err(encoder_failure)?;
    let mut text_call = call(&text);
    text_call.push(("phrase", Json::str(PROBE_PHRASE)));
    let pairs = |calls: Vec<(&'static str, Json)>| {
        Json::Object(calls.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    };
    let report = Json::object([
        ("encoder_url", Json::str(&encoder.base_url)),
        ("model", Json::str(&encoder.model)),
        ("served_by", Json::str(&image.model)),
        ("image", pairs(call(&image))),
        ("text", pairs(text_call)),
        (
            "cold_start_wait_ms",
            Json::Int(
                ((cold.first_byte_s - text.first_byte_s) * 1000.0)
                    .round_ties_even()
                    .max(0.0) as i64,
            ),
        ),
        ("frame_dims", Json::Int(frames::FRAME_DIMS as i64)),
    ]);
    println!("{}", dumps_indent2(&report));
    let width = |answer: &Embedded| answer.vectors.first().map_or(0, Vec::len);
    let widths = std::collections::BTreeSet::from([width(&image), width(&text)]);
    if widths.len() != 1 {
        return Err(Failure::Failed(format!(
            "image and text vectors differ in width: {:?}",
            widths.into_iter().collect::<Vec<_>>()
        )));
    }
    let only = widths.into_iter().next().unwrap_or(0);
    if only != frames::FRAME_DIMS {
        return Err(Failure::Failed(format!(
            "the encoder returns {only}-wide vectors; frame_vec holds {}",
            frames::FRAME_DIMS
        )));
    }
    // An answer naming no model is not a mismatch; one naming another model is.
    let served: std::collections::BTreeSet<&str> = [image.model.as_str(), text.model.as_str()]
        .into_iter()
        .filter(|name| *name != encoder.model && !name.is_empty())
        .collect();
    if !served.is_empty() {
        return Err(Failure::Failed(format!(
            "asked for {}, the encoder answered as {}",
            encoder.model,
            served.into_iter().collect::<Vec<_>>().join(" and ")
        )));
    }
    Ok(())
}

/// `--top K` (repeatable) and the phrases around it; no `--top` means 10 and 50.
fn quant_arguments(args: &[&str]) -> Result<(Vec<String>, Vec<usize>), Failure> {
    let mut phrases = Vec::new();
    let mut ks = Vec::new();
    let mut tokens = args.iter();
    while let Some(&token) = tokens.next() {
        let value = if token == "--top" {
            Some(
                *tokens
                    .next()
                    .ok_or_else(|| Failure::Usage("--top needs a value".into()))?,
            )
        } else {
            token.strip_prefix("--top=")
        };
        if let Some(raw) = value {
            match raw.parse::<usize>() {
                Ok(k) if k >= 1 => {
                    if !ks.contains(&k) {
                        ks.push(k);
                    }
                }
                _ => return Err(Failure::Usage(format!("{raw} is not a positive count"))),
            }
        } else {
            phrases.push(token.to_string());
        }
    }
    if phrases.is_empty() {
        return Err(Failure::Usage("name at least one phrase".into()));
    }
    if ks.is_empty() {
        ks = vec![10, 50];
    }
    Ok((phrases, ks))
}

fn quant_agreement(args: &[&str]) -> Result<(), Failure> {
    let (phrases, ks) = quant_arguments(args)?;
    let config = FrameConfig::from_env().map_err(failure)?;
    let path = config.require_frames_path().map_err(failure)?;
    let floats = frame_quant::float_path(&path);
    if !floats.is_file() {
        return Err(Failure::Failed(format!(
            "{} does not exist; run harvest-frames --keep-float first",
            floats.display()
        )));
    }
    let encoder = EncoderClient::from_config(&config).map_err(encoder_failure)?;
    let vectors = encoder.embed_texts(&phrases).map_err(encoder_failure)?;
    let frames_conn = open_frames(&path).map_err(failure)?;
    let float_conn = frame_quant::open_float(&floats).map_err(failure)?;
    let paired = frame_quant::load(&frames_conn, &float_conn)
        .map_err(|err| Failure::Failed(err.to_string()))?;
    let report = Json::object([
        ("titles", Json::Int(paired.titles as i64)),
        (
            "titles_without_floats",
            Json::Array(paired.titles_without_floats.iter().map(Json::str).collect()),
        ),
        (
            "phrases",
            Json::Array(
                phrases
                    .iter()
                    .zip(&vectors)
                    .map(|(phrase, vector)| {
                        let Json::Object(mut pairs) = frame_quant::agreement(&paired, vector, &ks)
                        else {
                            unreachable!("agreement builds an object")
                        };
                        pairs.insert(0, ("phrase".to_string(), Json::str(phrase)));
                        Json::Object(pairs)
                    })
                    .collect(),
            ),
        ),
    ]);
    println!("{}", dumps_indent2(&report));
    Ok(())
}

fn migrate() -> Result<(), StoreError> {
    let config = FrameConfig::from_env()?;
    config.require_frames_path()?;
    println!("{}", migrate_configured(&config)?);
    Ok(())
}

fn status() -> Result<(), StoreError> {
    let config = FrameConfig::from_env()?;
    let path = config.require_frames_path()?;
    let store = Config::from_env()?;
    let reachable = configured_reachable(&config);
    let frames_conn = open_frames(&path)?;
    let store_conn = open_readonly(&store.store_path)?;
    let sections = section_titles(&store_conn)?;
    let report = frames::status(
        &frames_conn,
        &sections,
        &StatusInput {
            path: &path,
            hours: &config.frame_hours,
            now: Local::now(),
            encoder_reachable: reachable,
            ffmpeg_found: which(&config.ffmpeg) && which(&config.ffprobe),
            decoder: decoder_report(&path, &config.decode),
        },
    )?;
    println!("{}", dumps_indent2(&report));
    Ok(())
}

fn queue_title(item_id: &str, by: &str) -> Result<(), StoreError> {
    let config = FrameConfig::from_env()?;
    let path = config.require_frames_path()?;
    let store = Config::from_env()?;
    let known = open_readonly(&store.store_path)?
        .query_row("SELECT 1 FROM items WHERE item_id = ?1", [item_id], |_| {
            Ok(())
        })
        .is_ok();
    let frames_conn = open_frames(&path)?;
    let added = queue(&frames_conn, item_id, known, by, &Local::now())?;
    let waiting: i64 =
        frames_conn.query_row("SELECT count(*) FROM frame_queue", [], |row| row.get(0))?;
    let verb = if added { "queued" } else { "already queued" };
    println!(
        "{verb} {item_id}; {} title(s) in the queue",
        group_thousands(waiting)
    );
    Ok(())
}

/// Python's `f"{n:,}"`.
pub(crate) fn group_thousands(n: i64) -> String {
    let digits = n.abs().to_string();
    let mut out = String::new();
    for (ix, c) in digits.chars().enumerate() {
        if ix > 0 && (digits.len() - ix).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}
