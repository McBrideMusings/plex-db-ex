//! Recording the files and tracks Plex lists for each movie and episode.
//!
//! `media_part` and `media_stream` are facts about a title only Plex carries: which
//! file plays it, in what container and codecs, and which audio and subtitle tracks
//! the file holds. The section listing already names every title's `updatedAt` and
//! its parts, but not the tracks, which come from `/library/metadata/<ratingKey>`,
//! one request per title.
//!
//! **Fetch once per change.** A title is fetched only when the `updatedAt` its
//! listing reports differs from the one stored with its parts. A fetch replaces the
//! whole set of parts and streams recorded for that Plex record, and each record
//! commits on its own, so a run that stops partway keeps what it fetched.

use std::collections::{BTreeSet, HashMap};

use plexdb_store::StoreError;
use rusqlite::types::Value as Sql;
use rusqlite::{Connection, params_from_iter};
use serde_json::Value;

use crate::plex::{PlexSource, TYPE_EPISODE, TYPE_MOVIE};
use crate::pyval::{get, number, or, py_int, text_or_none, truthy};

pub const NAME_WALK: &str = "media-walk";

/// A Plex section type -> the `(items.type, Plex item type)` that carries files.
fn file_pass(kind: &str) -> Option<(&'static str, i64)> {
    match kind {
        "movie" => Some(("movie", TYPE_MOVIE)),
        "show" => Some(("episode", TYPE_EPISODE)),
        _ => None,
    }
}

/// What one media walk did.
#[derive(Debug, Default)]
pub struct MediaWalkStats {
    pub titles_listed: usize,
    /// Titles whose stored `updatedAt` matched, so nothing was requested.
    pub unchanged: usize,
    /// Metadata requests made.
    pub fetched: usize,
    pub parts_written: usize,
    pub streams_written: usize,
    /// Titles that dropped their parts because Plex lists no file for them now.
    pub emptied: usize,
    /// Rating keys Plex returned no record for when fetched.
    pub missing: usize,
}

fn nullable<T: Into<Sql>>(value: Option<T>) -> Sql {
    value.map_or(Sql::Null, Into::into)
}

fn listed_parts(record: &Value) -> bool {
    get(record, "Media")
        .as_array()
        .into_iter()
        .flatten()
        .any(|media| {
            get(media, "Part")
                .as_array()
                .into_iter()
                .flatten()
                .any(|part| truthy(get(part, "file")))
        })
}

fn drop_parts(conn: &Connection, rating_key: &str) -> Result<(), StoreError> {
    conn.execute(
        "DELETE FROM media_stream WHERE part_id IN \
         (SELECT part_id FROM media_part WHERE rating_key = ?)",
        [rating_key],
    )?;
    conn.execute("DELETE FROM media_part WHERE rating_key = ?", [rating_key])?;
    Ok(())
}

fn stream_kind(stream_type: i64) -> Option<&'static str> {
    match stream_type {
        1 => Some("video"),
        2 => Some("audio"),
        3 => Some("subtitle"),
        _ => None,
    }
}

fn forced(value: &Value) -> bool {
    match value {
        Value::Bool(flag) => *flag,
        Value::String(text) => text == "1",
        other => number(other) == Some(1.0),
    }
}

/// Replace the parts and streams recorded for `rating_key` with `record`'s.
fn write_parts(
    conn: &Connection,
    item_id: &str,
    rating_key: &str,
    record: &Value,
    updated_at: Option<i64>,
    now: &str,
) -> Result<(usize, usize), StoreError> {
    drop_parts(conn, rating_key)?;
    let (mut parts, mut streams) = (0, 0);
    for media in get(record, "Media").as_array().into_iter().flatten() {
        for part in get(media, "Part").as_array().into_iter().flatten() {
            let part_id = py_int(get(part, "id"));
            let file = get(part, "file");
            let (Some(part_id), true) = (part_id, truthy(file)) else {
                continue;
            };
            conn.execute(
                "INSERT OR REPLACE INTO media_part (part_id, item_id, rating_key, file, size, \
                 container, video_codec, audio_codec, height, bitrate, duration_ms, \
                 plex_updated_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                params_from_iter([
                    Sql::Integer(part_id),
                    Sql::Text(item_id.into()),
                    Sql::Text(rating_key.into()),
                    Sql::Text(crate::pyval::py_str(file)),
                    nullable(py_int(get(part, "size"))),
                    nullable(text_or_none(or(
                        get(part, "container"),
                        get(media, "container"),
                    ))),
                    nullable(text_or_none(get(media, "videoCodec"))),
                    nullable(text_or_none(get(media, "audioCodec"))),
                    nullable(py_int(get(media, "height"))),
                    nullable(py_int(get(media, "bitrate"))),
                    nullable(py_int(or(get(part, "duration"), get(media, "duration")))),
                    nullable(updated_at),
                    Sql::Text(now.into()),
                ]),
            )?;
            parts += 1;
            conn.execute("DELETE FROM media_stream WHERE part_id = ?", [part_id])?;
            for (position, stream) in get(part, "Stream")
                .as_array()
                .into_iter()
                .flatten()
                .enumerate()
            {
                let Some(kind) = stream_kind(py_int(get(stream, "streamType")).unwrap_or(0)) else {
                    continue;
                };
                let index = py_int(get(stream, "index")).unwrap_or(position as i64);
                conn.execute(
                    "INSERT OR REPLACE INTO media_stream (part_id, idx, kind, codec, lang, \
                     forced, title) VALUES (?,?,?,?,?,?,?)",
                    params_from_iter([
                        Sql::Integer(part_id),
                        Sql::Integer(index),
                        Sql::Text(kind.into()),
                        nullable(text_or_none(get(stream, "codec"))),
                        nullable(text_or_none(or(
                            get(stream, "languageCode"),
                            get(stream, "language"),
                        ))),
                        Sql::Integer(i64::from(forced(get(stream, "forced")))),
                        nullable(text_or_none(or(
                            get(stream, "displayTitle"),
                            get(stream, "title"),
                        ))),
                    ]),
                )?;
                streams += 1;
            }
        }
    }
    Ok((parts, streams))
}

/// Record the parts and streams of every movie and episode whose Plex `updatedAt`
/// differs from the one stored.
pub fn media_walk(
    conn: &mut Connection,
    source: &dyn PlexSource,
    section_key: Option<&str>,
) -> Result<MediaWalkStats, StoreError> {
    let mut stats = MediaWalkStats::default();
    let now = crate::staleness::Now::read().iso();

    let mut item_for_key: HashMap<String, (String, String)> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT p.rating_key, p.item_id, i.type FROM plex_items p \
             JOIN items i ON i.item_id = p.item_id",
        )?;
        for row in stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })? {
            let (rating_key, item_id, kind) = row?;
            item_for_key.insert(rating_key, (item_id, kind));
        }
    }
    let mut stored: HashMap<String, BTreeSet<Option<i64>>> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT rating_key, plex_updated_at FROM media_part")?;
        for row in stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?))
        })? {
            let (rating_key, updated_at) = row?;
            stored.entry(rating_key).or_default().insert(updated_at);
        }
    }

    for section in source.sections()? {
        if section_key.is_some_and(|key| section.key != key) {
            continue;
        }
        let Some((kind, plex_type)) = file_pass(&section.kind) else {
            continue;
        };
        for listed in source.items(&section.key, plex_type)? {
            let Some(rating_key) = text_or_none(get(&listed, "ratingKey")) else {
                continue;
            };
            let Some((item_id, known_kind)) = item_for_key.get(&rating_key) else {
                continue;
            };
            if known_kind != kind {
                continue;
            }
            stats.titles_listed += 1;
            let updated_at = py_int(get(&listed, "updatedAt"));
            if !listed_parts(&listed) {
                if stored.contains_key(&rating_key) {
                    let tx = conn.transaction()?;
                    drop_parts(&tx, &rating_key)?;
                    tx.commit()?;
                    stats.emptied += 1;
                }
                continue;
            }
            if let Some(updated) = updated_at
                && stored.get(&rating_key) == Some(&BTreeSet::from([Some(updated)]))
            {
                stats.unchanged += 1;
                continue;
            }
            stats.fetched += 1;
            let Some(record) = source.metadata(&rating_key)? else {
                stats.missing += 1;
                let tx = conn.transaction()?;
                drop_parts(&tx, &rating_key)?;
                tx.commit()?;
                continue;
            };
            let tx = conn.transaction()?;
            let (parts, streams) =
                write_parts(&tx, item_id, &rating_key, &record, updated_at, &now)?;
            tx.commit()?;
            stats.parts_written += parts;
            stats.streams_written += streams;
        }
    }
    Ok(stats)
}

/// The line `plexdb media-walk` prints.
pub fn describe(stats: &MediaWalkStats) -> String {
    format!(
        "media: {} title(s) listed, {} unchanged, {} fetched, {} part(s) and {} stream(s) \
         written, {} emptied, {} missing from Plex",
        stats.titles_listed,
        stats.unchanged,
        stats.fetched,
        stats.parts_written,
        stats.streams_written,
        stats.emptied,
        stats.missing
    )
}
