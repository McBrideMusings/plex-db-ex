//! Embedding short texts through llama-swap's OpenAI-compatible endpoint
//! (`plexdb/embed_client.py`), and the file that keeps the vectors between sweeps.
//!
//! `judge-keyword-pairs` depends on [`Embedder`], not on [`LlamaSwapEmbedder`], so a
//! test can substitute a fake that returns vectors without a socket.

use std::collections::HashMap;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::Path;
use std::time::Duration;

use serde_json::{Value, json};

use crate::frame_encoder::error_kind;

/// The one model the keyword vectors come from. A different model gives different
/// cosines, so the cut in `synonyms.rs` belongs to this one.
pub const MODEL: &str = "embeddinggemma-300m";
const TIMEOUT: Duration = Duration::from_secs(300);

/// The embedding server could not give the vectors asked for.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct EmbeddingError(pub String);

pub trait Embedder {
    /// One vector per text, in the order given.
    fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, EmbeddingError>;
}

/// The one [`Embedder`] that reaches a real server.
pub struct LlamaSwapEmbedder {
    url: String,
    agent: ureq::Agent,
}

impl LlamaSwapEmbedder {
    pub fn new(base_url: &str) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .build()
            .into();
        LlamaSwapEmbedder {
            url: format!("{}/v1/embeddings", base_url.trim_end_matches('/')),
            agent,
        }
    }
}

impl Embedder for LlamaSwapEmbedder {
    fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, EmbeddingError> {
        let url = &self.url;
        let mut response = self
            .agent
            .post(url)
            .send_json(json!({"model": MODEL, "input": texts}))
            .map_err(|err| {
                EmbeddingError(format!(
                    "cannot reach the embedding server at {url}: {}",
                    error_kind(&err)
                ))
            })?;
        let status = response.status().as_u16();
        let unusable = || {
            EmbeddingError(format!(
                "the embedding server returned an unusable body for {url}"
            ))
        };
        let raw = response
            .body_mut()
            .with_config()
            .limit(1 << 30)
            .read_to_string()
            .map_err(|_| unusable())?;
        if status != 200 {
            let head: String = raw.chars().take(200).collect();
            return Err(EmbeddingError(format!(
                "the embedding server returned {status} for {url}: {head}"
            )));
        }
        let vectors = parse(&raw).ok_or_else(unusable)?;
        if vectors.len() != texts.len() {
            return Err(EmbeddingError(format!(
                "asked for {} embedding(s), the server returned {}",
                texts.len(),
                vectors.len()
            )));
        }
        Ok(vectors)
    }
}

/// The vectors of an embeddings body, ordered by each row's `index`.
fn parse(raw: &str) -> Option<Vec<Vec<f32>>> {
    let body: Value = serde_json::from_str(raw).ok()?;
    let mut rows: Vec<(i64, Vec<f32>)> = body
        .get("data")?
        .as_array()?
        .iter()
        .map(|row| {
            let index = row.get("index")?.as_i64()?;
            let vector = row
                .get("embedding")?
                .as_array()?
                .iter()
                .map(|x| x.as_f64().map(|x| x as f32))
                .collect::<Option<Vec<f32>>>()?;
            Some((index, vector))
        })
        .collect::<Option<_>>()?;
    rows.sort_by_key(|(index, _)| *index);
    Some(rows.into_iter().map(|(_, vector)| vector).collect())
}

const CACHE_MAGIC: &[u8; 8] = b"PLXKEMB1";

/// The cached unit vector per text, or nothing when the file is absent, unreadable or
/// was made by another model.
pub fn load_cache(path: &Path) -> HashMap<String, Vec<f32>> {
    read_cache(path).unwrap_or_default()
}

fn read_cache(path: &Path) -> Option<HashMap<String, Vec<f32>>> {
    let mut reader = BufReader::new(std::fs::File::open(path).ok()?);
    let mut magic = [0u8; 8];
    reader.read_exact(&mut magic).ok()?;
    if &magic != CACHE_MAGIC {
        return None;
    }
    let model = read_text(&mut reader)?;
    if model != MODEL {
        return None;
    }
    let count = read_u32(&mut reader)? as usize;
    let dim = read_u32(&mut reader)? as usize;
    let mut known = HashMap::with_capacity(count);
    for _ in 0..count {
        let text = read_text(&mut reader)?;
        let mut bytes = vec![0u8; dim * 4];
        reader.read_exact(&mut bytes).ok()?;
        let vector = bytes
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .collect();
        known.insert(text, vector);
    }
    Some(known)
}

fn read_u32(reader: &mut impl Read) -> Option<u32> {
    let mut bytes = [0u8; 4];
    reader.read_exact(&mut bytes).ok()?;
    Some(u32::from_le_bytes(bytes))
}

fn read_text(reader: &mut impl Read) -> Option<String> {
    let length = read_u32(reader)? as usize;
    let mut bytes = vec![0u8; length];
    reader.read_exact(&mut bytes).ok()?;
    String::from_utf8(bytes).ok()
}

/// Write `vectors` to `path` through a temporary file beside it, so a run killed
/// mid-save leaves the old cache whole.
pub fn save_cache(path: &Path, vectors: &[(&String, &Vec<f32>)]) -> std::io::Result<()> {
    let mut tmp_name = path.file_name().unwrap_or_default().to_os_string();
    tmp_name.push(".tmp");
    let tmp = path.with_file_name(tmp_name);
    let result = (|| {
        let mut writer = BufWriter::new(std::fs::File::create(&tmp)?);
        writer.write_all(CACHE_MAGIC)?;
        write_text(&mut writer, MODEL)?;
        let dim = vectors.first().map_or(0, |(_, vector)| vector.len());
        writer.write_all(&(vectors.len() as u32).to_le_bytes())?;
        writer.write_all(&(dim as u32).to_le_bytes())?;
        for (text, vector) in vectors {
            write_text(&mut writer, text)?;
            for x in vector.iter() {
                writer.write_all(&x.to_le_bytes())?;
            }
        }
        writer.flush()?;
        std::fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

fn write_text(writer: &mut impl Write, text: &str) -> std::io::Result<()> {
    writer.write_all(&(text.len() as u32).to_le_bytes())?;
    writer.write_all(text.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_are_put_back_in_index_order() {
        let body = r#"{"data": [
            {"index": 1, "embedding": [3.0, 4.0]},
            {"index": 0, "embedding": [1.0, 2.0]}]}"#;
        assert_eq!(
            parse(body),
            Some(vec![vec![1.0, 2.0], vec![3.0, 4.0]]),
            "sorted by index"
        );
    }

    #[test]
    fn a_body_without_data_is_unusable() {
        assert_eq!(parse("{}"), None);
        assert_eq!(parse("not json"), None);
    }

    #[test]
    fn the_cache_round_trips_and_a_foreign_file_is_empty() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("keyword-embeddings.bin");
        assert!(load_cache(&path).is_empty(), "absent file");
        let (a, b) = ("heist".to_string(), "caper".to_string());
        let (va, vb) = (vec![0.5f32, -0.25], vec![1.0f32, 0.0]);
        save_cache(&path, &[(&a, &va), (&b, &vb)]).expect("save");
        let known = load_cache(&path);
        assert_eq!(known.get("heist"), Some(&va));
        assert_eq!(known.get("caper"), Some(&vb));
        std::fs::write(&path, b"not a cache").expect("overwrite");
        assert!(load_cache(&path).is_empty(), "foreign file");
    }
}
