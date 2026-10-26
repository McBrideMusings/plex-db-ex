//! The Frame encoder client: frames and phrases to CLIP vectors (ADR-0025)
//! (`plexdb/frame_encoder.py`).
//!
//! Every call is a `POST {PLEXDB_FRAME_ENCODER_URL}/v1/embeddings` naming
//! `PLEXDB_FRAME_MODEL`. This client loads no model and decides nothing about when work
//! runs; whatever serves the URL does. A slow answer is waited out — a broker holds a
//! request through a cold start — and one that is down or busy (a refused connection, a
//! 5xx, a 429) is [`EncoderError::Unavailable`].
//!
//! Each image is a `data:image/jpeg;base64,` URI in `input`, with `"modality": "image"`.

use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Value, json};

use crate::frames::FrameConfig;

/// Long enough to wait out a broker loading the model onto a GPU.
const TIMEOUT: Duration = Duration::from_secs(300);
/// `reachable` asks only whether the server answers; listing models loads none.
const REACHABLE_TIMEOUT: Duration = Duration::from_secs(10);

/// The phrase `plexdb frames probe-encoder` embeds.
pub const PROBE_PHRASE: &str = "a red square";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EncoderError {
    /// The server is down or busy; the run stops and the title in hand is left untouched.
    #[error("{0}")]
    Unavailable(String),
    /// The server answered and refused, or answered nonsense; the title fails.
    #[error("{0}")]
    Rejected(String),
    /// A setting is absent.
    #[error("{0}")]
    Config(String),
}

/// One request's answer: one unit-length vector per input, in input order, the model the
/// server says produced them, and how long the request took to its first byte and its
/// last.
#[derive(Debug, Clone)]
pub struct Embedded {
    pub vectors: Vec<Vec<f64>>,
    pub model: String,
    pub first_byte_s: f64,
    pub total_s: f64,
}

#[derive(Clone, Copy)]
enum Modality {
    Image,
    Text,
}

impl Modality {
    fn name(self) -> &'static str {
        match self {
            Modality::Image => "image",
            Modality::Text => "text",
        }
    }
}

/// What the harvester asks of an encoder. [`EncoderClient`] is the real one; a test
/// substitutes a fake that answers without a socket.
pub trait Encoder {
    fn model(&self) -> &str;
    fn timed_images(&self, images: &[Vec<u8>]) -> Result<Embedded, EncoderError>;
}

pub struct EncoderClient {
    pub base_url: String,
    pub model: String,
    agent: ureq::Agent,
}

fn log(line: &str) {
    eprintln!("{line}");
}

impl EncoderClient {
    pub fn new(base_url: &str, model: &str) -> Self {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(TIMEOUT))
            .http_status_as_error(false)
            .build()
            .into();
        EncoderClient {
            base_url: base_url.trim_end_matches('/').to_string(),
            model: model.to_string(),
            agent,
        }
    }

    /// The configured encoder, or a `Config` error naming what is unset.
    pub fn from_config(config: &FrameConfig) -> Result<Self, EncoderError> {
        if config.encoder_url.is_empty() {
            return Err(EncoderError::Config(
                "PLEXDB_FRAME_ENCODER_URL is not set, so there is no Frame encoder".into(),
            ));
        }
        if config.model.is_empty() {
            return Err(EncoderError::Config(
                "PLEXDB_FRAME_MODEL is not set, so the Frame encoder has no model".into(),
            ));
        }
        Ok(Self::new(&config.encoder_url, &config.model))
    }

    pub fn timed_texts(&self, texts: &[String]) -> Result<Embedded, EncoderError> {
        self.post(texts.to_vec(), Modality::Text)
    }

    fn post(&self, payload: Vec<String>, modality: Modality) -> Result<Embedded, EncoderError> {
        if payload.is_empty() {
            return Ok(Embedded {
                vectors: Vec::new(),
                model: self.model.clone(),
                first_byte_s: 0.0,
                total_s: 0.0,
            });
        }
        let url = format!("{}/v1/embeddings", self.base_url);
        let count = payload.len();
        let body = json!({
            "model": self.model,
            "input": payload,
            "modality": modality.name(),
            "encoding_format": "float",
        });
        let started = Instant::now();
        let unavailable = |kind: &str| {
            log(&format!(
                "encoder: POST {url} {} x{count} -> {kind}",
                modality.name()
            ));
            EncoderError::Unavailable(format!("encoder unavailable at {url}: {kind}"))
        };
        let mut response = self
            .agent
            .post(&url)
            .send_json(&body)
            .map_err(|err| unavailable(error_kind(&err)))?;
        let first_byte_s = started.elapsed().as_secs_f64();
        let status = response.status().as_u16();
        let raw = response
            .body_mut()
            .with_config()
            .limit(1 << 30)
            .read_to_vec()
            .map_err(|_| unavailable("ReadError"))?;
        let total_s = started.elapsed().as_secs_f64();
        log(&format!(
            "encoder: POST {url} {} x{count} -> {status}, {} bytes, first byte {:.0} ms, total {:.0} ms",
            modality.name(),
            raw.len(),
            first_byte_s * 1000.0,
            total_s * 1000.0
        ));
        // 429 is a busy encoder: every later request would meet the same answer, so it
        // stops the run like a refused connection does.
        if status >= 500 || status == 429 {
            return Err(EncoderError::Unavailable(format!(
                "encoder unavailable at {url}: {status}: {}",
                py_bytes_repr(&raw[..raw.len().min(200)])
            )));
        }
        if status != 200 {
            return Err(EncoderError::Rejected(format!(
                "the encoder at {url} refused the request: {status}: {}",
                py_bytes_repr(&raw[..raw.len().min(200)])
            )));
        }
        let (vectors, model) = parse(&raw, &url, count)?;
        Ok(Embedded {
            vectors,
            model,
            first_byte_s,
            total_s,
        })
    }

    /// Whether the server answers `GET /v1/models` with a 2xx inside ten seconds. It
    /// never embeds, so it never waits out a cold start.
    pub fn reachable(&self) -> bool {
        let url = format!("{}/v1/models", self.base_url);
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(REACHABLE_TIMEOUT))
            .http_status_as_error(false)
            .build()
            .into();
        match agent.get(&url).call() {
            Ok(response) => {
                let status = response.status();
                log(&format!("encoder: GET {url} -> {}", status.as_u16()));
                status.is_success()
            }
            Err(err) => {
                log(&format!("encoder: GET {url} -> {}", error_kind(&err)));
                false
            }
        }
    }
}

impl Encoder for EncoderClient {
    fn model(&self) -> &str {
        &self.model
    }

    fn timed_images(&self, images: &[Vec<u8>]) -> Result<Embedded, EncoderError> {
        let uris = images
            .iter()
            .map(|jpeg| format!("data:image/jpeg;base64,{}", STANDARD.encode(jpeg)))
            .collect();
        self.post(uris, Modality::Image)
    }
}

/// What `plexdb frames status` reports as `encoder_reachable`: `None` when no encoder URL
/// is set, else whether it answers. Reachability needs no model.
pub fn configured_reachable(config: &FrameConfig) -> Option<bool> {
    if config.encoder_url.is_empty() {
        return None;
    }
    Some(EncoderClient::new(&config.encoder_url, &config.model).reachable())
}

pub(crate) fn error_kind(err: &ureq::Error) -> &'static str {
    match err {
        ureq::Error::Timeout(_) => "TimeoutException",
        ureq::Error::HostNotFound | ureq::Error::ConnectionFailed => "ConnectError",
        ureq::Error::Io(_) => "ReadError",
        _ => "TransportError",
    }
}

/// Python's `repr(bytes)`.
pub fn py_bytes_repr(bytes: &[u8]) -> String {
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = String::from("b");
    out.push(quote as char);
    for &b in bytes {
        match b {
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            b if b == quote => {
                out.push('\\');
                out.push(b as char);
            }
            0x20..=0x7e => out.push(b as char),
            b => out.push_str(&format!("\\x{b:02x}")),
        }
    }
    out.push(quote as char);
    out
}

/// numpy's pairwise summation of a contiguous float64 run (`@TYPE@_pairwise_sum`), the
/// order `np.add.reduce` adds in; a plain left-to-right sum rounds differently.
fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// `np.linalg.norm(row)` along a row: `sqrt(add.reduce(row * row))`.
fn norm(row: &[f64]) -> f64 {
    let squares: Vec<f64> = row.iter().map(|x| x * x).collect();
    pairwise_sum(&squares).sqrt()
}

fn parse(raw: &[u8], url: &str, expected: usize) -> Result<(Vec<Vec<f64>>, String), EncoderError> {
    let unusable =
        || EncoderError::Rejected(format!("the encoder at {url} returned an unusable body"));
    let body: Value = serde_json::from_slice(raw).map_err(|_| unusable())?;
    let data = body
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(unusable)?;
    let mut rows: Vec<(i64, &Value)> = Vec::with_capacity(data.len());
    for row in data {
        let index = row
            .get("index")
            .and_then(Value::as_i64)
            .ok_or_else(unusable)?;
        let embedding = row.get("embedding").ok_or_else(unusable)?;
        rows.push((index, embedding));
    }
    rows.sort_by_key(|(index, _)| *index);
    let mut matrix: Vec<Vec<f64>> = Vec::with_capacity(rows.len());
    for (_, embedding) in &rows {
        let values = embedding.as_array().ok_or_else(unusable)?;
        let mut vector = Vec::with_capacity(values.len());
        for value in values {
            vector.push(value.as_f64().ok_or_else(unusable)?);
        }
        matrix.push(vector);
    }
    let model = match body.get("model") {
        None => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    };
    let width = matrix.first().map_or(0, Vec::len);
    if matrix.len() != expected || matrix.iter().any(|row| row.len() != width) {
        return Err(EncoderError::Rejected(format!(
            "asked {url} for {expected} vector(s), it returned {}",
            rows.len()
        )));
    }
    let mut vectors = Vec::with_capacity(matrix.len());
    for row in matrix {
        let length = norm(&row);
        if !row.iter().all(|x| x.is_finite())
            || length.partial_cmp(&0.0) != Some(std::cmp::Ordering::Greater)
        {
            return Err(EncoderError::Rejected(format!(
                "the encoder at {url} returned a zero or non-finite vector"
            )));
        }
        vectors.push(row.iter().map(|x| x / length).collect());
    }
    Ok((vectors, model))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pairwise_sum_groups_the_way_numpy_does() {
        // 768 values: split 384 | 384, then 192 | 192, then 96 | 96 (each under 128).
        let values: Vec<f64> = (0..768).map(|i| 1.0 / f64::from(i + 3)).collect();
        let straight: f64 = values.iter().sum();
        let paired = pairwise_sum(&values);
        // Same total to within rounding; the grouping is what the golden test pins.
        assert!((straight - paired).abs() < 1e-12);
    }

    #[test]
    fn a_body_is_sorted_by_index_and_normalised() {
        let raw = br#"{"model":"m","data":[{"index":1,"embedding":[0,2]},{"index":0,"embedding":[3,4]}]}"#;
        let (vectors, model) = parse(raw, "http://x/v1/embeddings", 2).unwrap();
        assert_eq!(model, "m");
        assert_eq!(vectors[0], vec![0.6, 0.8]);
        assert_eq!(vectors[1], vec![0.0, 1.0]);
    }

    #[test]
    fn a_wrong_count_and_a_zero_vector_are_refused() {
        let raw = br#"{"data":[{"index":0,"embedding":[0,0]}]}"#;
        let err = parse(raw, "u", 1).unwrap_err();
        assert!(err.to_string().contains("zero or non-finite"));
        let err = parse(raw, "u", 2).unwrap_err();
        assert!(
            err.to_string()
                .contains("asked u for 2 vector(s), it returned 1")
        );
    }
}
