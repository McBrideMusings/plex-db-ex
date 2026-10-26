//! Reading a part's video file for the frame harvester: its Cuts, its last frame, one
//! JPEG per sample time (`plexdb/frame_harvest.py`'s `FfmpegMedia`).
//!
//! ffmpeg and ffprobe stay external processes, run read-only against the media mount.
//! Reading the file rather than `/library/parts/<id>/file` matters: Plex makes each such
//! read a playback session and ends the account's previous one, so two readers of
//! different titles cut each other off.

use std::cell::RefCell;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use sha2::{Digest, Sha256};

/// One JPEG per sample time, `None` where the file has no frame there.
pub type Jpegs = Vec<Option<Vec<u8>>>;

/// A decode's Cuts, the offset of its last video frame, and its warning count.
type ScanParts = (Vec<Cut>, i64, usize);

/// A failure to read a file: `message` is the full text and may name paths and carry
/// ffmpeg's stderr; `reason` is the short path-free phrase a page may show.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct MediaError {
    pub message: String,
    pub reason: String,
}

impl MediaError {
    pub fn new(message: impl Into<String>, reason: impl Into<String>) -> Self {
        MediaError {
            message: message.into(),
            reason: reason.into(),
        }
    }
}

/// Frames cut out per ffmpeg process.
const EXTRACT_CHUNK: usize = 24;
/// How much of the file the hash reads.
const HASH_BYTES: usize = 1 << 20;
/// The longest one ffmpeg or ffprobe process may run: a full decode for scdet, minutes
/// for anything else.
const DECODE_TIMEOUT: Duration = Duration::from_secs(6 * 3600);
const PROBE_TIMEOUT: Duration = Duration::from_secs(900);
const ERROR_LINES_KEPT: usize = 5;

#[derive(Debug, Clone, PartialEq)]
pub struct Cut {
    pub t_ms: i64,
    pub score: Option<f64>,
    pub source: &'static str,
}

/// What one decode of a title finds: its Cuts and the offset of its last video frame.
#[derive(Debug, Clone)]
pub struct Scan {
    pub cuts: Vec<Cut>,
    pub last_frame_ms: i64,
    pub warnings: usize,
}

/// The three things the harvester asks of a video file.
pub trait MediaReader {
    fn file_hash(&self, path: &Path, size: i64) -> Result<String, MediaError>;
    fn scan(&self, path: &Path) -> Result<Scan, MediaError>;
    /// One JPEG per time, `None` where the file has no frame there.
    fn frames(&self, path: &Path, times_ms: &[i64]) -> Result<Jpegs, MediaError>;
}

// --- paths ------------------------------------------------------------------------

/// `identity.canonical_path`: strip whichever configured source root the path sits under
/// and normalise separators. Longest root first, and a root matches only at a path
/// boundary.
pub fn canonical_path(raw: &str, source_roots: &[String]) -> String {
    let normalized = raw.replace('\\', "/");
    let mut roots: Vec<String> = source_roots
        .iter()
        .map(|root| root.replace('\\', "/").trim_end_matches('/').to_string())
        .collect();
    // Python sorts by `len` (characters), longest first, keeping input order on ties.
    roots.sort_by_key(|root| std::cmp::Reverse(root.chars().count()));
    for root in &roots {
        if !normalized.starts_with(root.as_str()) {
            continue;
        }
        let rest = &normalized[root.len()..];
        if rest.is_empty() {
            return String::new();
        }
        if let Some(inside) = rest.strip_prefix('/') {
            return inside.to_string();
        }
    }
    normalized
}

/// `os.path.normpath` for POSIX paths.
pub fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".into();
    }
    let initial = if path.starts_with("//") && !path.starts_with("///") {
        2
    } else if path.starts_with('/') {
        1
    } else {
        0
    };
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part != ".."
            || (initial == 0 && parts.is_empty())
            || parts.last().is_some_and(|last| *last == "..")
        {
            parts.push(part);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let joined = format!("{}{}", "/".repeat(initial), parts.join("/"));
    if joined.is_empty() {
        ".".into()
    } else {
        joined
    }
}

/// Where this process reads the file Plex reports as `plex_file`.
///
/// Plex's path loses whichever of `source_roots` it sits under and is read below
/// `media_dir`. A path under no root, or one whose `..` would leave `media_dir`, is a
/// [`MediaError`] rather than a read somewhere else. The check is on the path's text, so
/// a symlink inside the library is followed wherever it points.
pub fn local_path(
    media_dir: &Path,
    plex_file: &str,
    source_roots: &[String],
) -> Result<PathBuf, MediaError> {
    let rest = canonical_path(plex_file, source_roots);
    if rest == plex_file.replace('\\', "/") {
        return Err(MediaError::new(
            format!(
                "{plex_file} is under none of PLEX_SOURCE_ROOTS {}",
                py_list_repr(source_roots)
            ),
            "file not under PLEX_SOURCE_ROOTS",
        ));
    }
    let absolute = std::path::absolute(media_dir).unwrap_or_else(|_| media_dir.to_path_buf());
    let root = normpath(&absolute.to_string_lossy());
    let joined = if rest.starts_with('/') {
        rest
    } else if root.ends_with('/') {
        format!("{root}{rest}")
    } else {
        format!("{root}/{rest}")
    };
    let path = normpath(&joined);
    let prefix = if root == "/" {
        "/".to_string()
    } else {
        format!("{root}/")
    };
    if path == root || !path.starts_with(&prefix) {
        return Err(MediaError::new(
            format!("{plex_file} leaves PLEXDB_MEDIA_DIR"),
            "file leaves PLEXDB_MEDIA_DIR",
        ));
    }
    Ok(PathBuf::from(path))
}

fn py_list_repr(items: &[String]) -> String {
    let quoted: Vec<String> = items
        .iter()
        .map(|item| crate::frames::py_repr(item))
        .collect();
    format!("[{}]", quoted.join(", "))
}

// --- parsing ffmpeg's output ----------------------------------------------------

const DECODER_PREFIXES: [&str; 20] = [
    "hevc",
    "h264",
    "av1",
    "vp8",
    "vp9",
    "mpeg1video",
    "mpeg2video",
    "mpeg4video",
    "mpeg4",
    "vc1",
    "wmv1",
    "wmv2",
    "wmv3",
    "msmpeg4v1",
    "msmpeg4v2",
    "msmpeg4v3",
    "mjpeg",
    "prores",
    "dnxhd",
    "theora",
];
const DECODE_ERROR_WORDS: [&str; 5] = [
    "invalid",
    "error",
    "outside the valid range",
    "undecodable",
    "concealing",
];

/// `^\[(hevc|h264|…)\w*\s*@`: a line from a video decoder.
fn is_video_decoder_line(line: &str) -> bool {
    let Some(rest) = line.strip_prefix('[') else {
        return false;
    };
    DECODER_PREFIXES.iter().any(|prefix| {
        rest.strip_prefix(prefix).is_some_and(|after| {
            let after = after.trim_start_matches(|c: char| c.is_alphanumeric() || c == '_');
            after.trim_start().starts_with('@')
        })
    })
}

/// Split ffmpeg's warning-level stderr into the video decoder's error lines and a count
/// of every other line. A decoder line counts as an error when it carries one of the
/// error words; the rest are warnings the harvest ignores.
pub fn classify_decode_stderr(stderr: &str) -> (Vec<String>, usize) {
    let mut errors = Vec::new();
    let mut warnings = 0;
    for raw in stderr.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        let lowered = line.to_lowercase();
        if is_video_decoder_line(line)
            && !lowered.contains("failed setup for format")
            && DECODE_ERROR_WORDS.iter().any(|word| lowered.contains(word))
        {
            errors.push(line.to_string());
        } else {
            warnings += 1;
        }
    }
    (errors, warnings)
}

fn rounded_ms(seconds: f64) -> i64 {
    (seconds * 1000.0).round_ties_even() as i64
}

/// Cuts from `metadata=print` output: a `lavfi.scd.score` and a `lavfi.scd.time` line per
/// detected scene change.
pub fn parse_scdet(text: &str) -> Result<Vec<Cut>, MediaError> {
    let mut cuts = Vec::new();
    let mut score: Option<f64> = None;
    for line in text.lines() {
        if let Some(value) = line.strip_prefix("lavfi.scd.score=") {
            score = Some(py_float(value).ok_or_else(|| {
                MediaError::new(
                    format!(
                        "scdet printed an unreadable score: {}",
                        crate::frames::py_repr(line)
                    ),
                    "scene scan output unreadable",
                )
            })?);
        } else if let Some(value) = line.strip_prefix("lavfi.scd.time=") {
            let seconds = py_float(value).ok_or_else(|| {
                MediaError::new(
                    format!(
                        "scdet printed an unreadable time: {}",
                        crate::frames::py_repr(line)
                    ),
                    "scene scan output unreadable",
                )
            })?;
            let t_ms = rounded_ms(seconds);
            if t_ms > 0 {
                cuts.push(Cut {
                    t_ms,
                    score,
                    source: "scdet",
                });
            }
        }
    }
    Ok(cuts)
}

/// Python's `float(text)` for the numbers ffmpeg prints.
fn py_float(text: &str) -> Option<f64> {
    let text = text.trim();
    match text.to_ascii_lowercase().as_str() {
        "nan" | "+nan" | "-nan" => return Some(f64::NAN),
        "inf" | "+inf" | "infinity" | "+infinity" => return Some(f64::INFINITY),
        "-inf" | "-infinity" => return Some(f64::NEG_INFINITY),
        _ => {}
    }
    if text.is_empty()
        || text
            .chars()
            .any(|c| c.is_ascii_alphabetic() && c != 'e' && c != 'E')
    {
        return None;
    }
    text.parse().ok()
}

/// The largest frame pts in framecrc output, in whole ms rounded down; `None` with no
/// frame. framecrc writes `#tb 0: <num>/<den>` and then one `stream, dts, pts, duration,
/// size, crc` line per frame.
pub fn parse_last_frame<'a>(
    lines: impl Iterator<Item = &'a str>,
) -> Result<Option<i64>, MediaError> {
    let unreadable = |line: &str| {
        MediaError::new(
            format!(
                "framecrc printed an unreadable line: {}",
                crate::frames::py_repr(line)
            ),
            "frame list unreadable",
        )
    };
    let (mut num, mut den) = (0_i128, 0_i128);
    let mut last: Option<i128> = None;
    for line in lines {
        if let Some(rest) = line.strip_prefix("#tb 0:") {
            let (n, d) = rest
                .trim()
                .split_once('/')
                .ok_or_else(|| unreadable(line))?;
            num = n.trim().parse().map_err(|_| unreadable(line))?;
            den = d.trim().parse().map_err(|_| unreadable(line))?;
        } else if !line.is_empty() && !line.starts_with('#') {
            let pts: i128 = line
                .split(',')
                .nth(2)
                .and_then(|field| field.trim().parse().ok())
                .ok_or_else(|| unreadable(line))?;
            last = Some(last.map_or(pts, |held| held.max(pts)));
        }
    }
    let Some(last) = last else {
        return Ok(None);
    };
    if den <= 0 {
        return Err(MediaError::new(
            "framecrc printed no time base",
            "frame list unreadable",
        ));
    }
    Ok(Some((last * num * 1000).div_euclid(den) as i64))
}

pub fn parse_chapters(text: &str) -> Result<Vec<Cut>, MediaError> {
    let unreadable = || {
        MediaError::new(
            "ffprobe returned chapters that are not JSON",
            "chapter list unreadable",
        )
    };
    let body: Value = serde_json::from_str(if text.is_empty() { "{}" } else { text })
        .map_err(|_| unreadable())?;
    let chapters = match body.get("chapters") {
        None => &Vec::new(),
        Some(Value::Array(items)) => items,
        Some(_) => return Err(unreadable()),
    };
    let mut cuts = Vec::new();
    for chapter in chapters {
        let start = chapter.get("start_time").ok_or_else(unreadable)?;
        let seconds = match start {
            Value::String(text) => py_float(text),
            Value::Number(n) => n.as_f64(),
            _ => None,
        }
        .ok_or_else(unreadable)?;
        let t_ms = rounded_ms(seconds);
        if t_ms > 0 {
            cuts.push(Cut {
                t_ms,
                score: None,
                source: "chapter",
            });
        }
    }
    Ok(cuts)
}

// --- running ffmpeg -------------------------------------------------------------

struct Ran {
    code: i32,
    stdout: Vec<u8>,
    stderr: String,
}

fn drain(mut stream: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stream.read_to_end(&mut bytes);
        bytes
    })
}

fn base_name(argv0: &str) -> &str {
    argv0.rsplit('/').next().unwrap_or(argv0)
}

fn run(argv: &[String], what: &str, timeout: Duration) -> Result<Ran, MediaError> {
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| {
            if err.kind() == std::io::ErrorKind::NotFound {
                MediaError::new(
                    format!("{what}: {} is not installed", argv[0]),
                    "ffmpeg or ffprobe not installed",
                )
            } else {
                MediaError::new(
                    format!("{what}: cannot run {}: {err}", argv[0]),
                    "ffmpeg or ffprobe not installed",
                )
            }
        })?;
    let stdout = child.stdout.take().map(drain);
    let stderr = child.stderr.take().map(drain);
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() > timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(MediaError::new(
                    format!(
                        "{what}: {} ran past {:.0} s",
                        argv[0],
                        timeout.as_secs_f64()
                    ),
                    format!("{} timed out", base_name(&argv[0])),
                ));
            }
            Ok(None) => thread::sleep(Duration::from_millis(20)),
            Err(err) => {
                return Err(MediaError::new(
                    format!("{what}: {err}"),
                    "ffmpeg or ffprobe not installed",
                ));
            }
        }
    };
    let stdout = stdout
        .map(|handle| handle.join().unwrap_or_default())
        .unwrap_or_default();
    let stderr = stderr
        .map(|handle| handle.join().unwrap_or_default())
        .unwrap_or_default();
    Ok(Ran {
        code: status.code().unwrap_or(-1),
        stdout,
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

/// The last 200 characters of `text` after stripping, like `text.strip()[-200:]`.
fn tail200(text: &str) -> String {
    let stripped = text.trim();
    let count = stripped.chars().count();
    stripped.chars().skip(count.saturating_sub(200)).collect()
}

/// RFC 2822 date in GMT, `email.utils.formatdate(secs, usegmt=True)`.
pub fn http_date(secs: i64) -> String {
    const DAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // Howard Hinnant's civil-from-days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    // 1970-01-01 was a Thursday; Monday is index 0.
    let weekday = (days + 3).rem_euclid(7) as usize;
    format!(
        "{}, {day:02} {} {year:04} {:02}:{:02}:{:02} GMT",
        DAYS[weekday],
        MONTHS[(month - 1) as usize],
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

/// Told why a CUDA decode fell back to the CPU.
pub type FallbackHook = Box<dyn Fn(&str)>;

/// Reads a part's file with ffmpeg and ffprobe, read-only, from the media mount.
pub struct FfmpegMedia {
    ffmpeg: String,
    ffprobe: String,
    decode: RefCell<String>,
    on_fallback: Option<FallbackHook>,
}

impl FfmpegMedia {
    pub fn new(
        ffmpeg: &str,
        ffprobe: &str,
        decode: &str,
        on_fallback: Option<FallbackHook>,
    ) -> Self {
        FfmpegMedia {
            ffmpeg: ffmpeg.to_string(),
            ffprobe: ffprobe.to_string(),
            decode: RefCell::new(decode.to_string()),
            on_fallback,
        }
    }

    /// Flags before the scene scan's input; extraction never takes them, because 24
    /// parallel 4K CUDA decoders exceed NVDEC's session limits. No
    /// `-hwaccel_output_format`: ffmpeg copies each decoded frame back to system memory,
    /// so scdet and framecrc see the pixels the CPU decode gives.
    fn hwaccel(&self) -> Vec<String> {
        if *self.decode.borrow() == "cuda" {
            vec!["-hwaccel".into(), "cuda".into()]
        } else {
            Vec::new()
        }
    }

    /// CUDA failed where a CPU decode is about to be tried: decode on the CPU from here
    /// on and say why, with ffmpeg's own last line.
    fn fall_back(&self, stderr: &str) {
        let reason = stderr
            .trim()
            .lines()
            .rfind(|line| !line.trim().is_empty())
            .map(|line| line.trim().to_string())
            .unwrap_or_else(|| "ffmpeg exited without a message".into());
        *self.decode.borrow_mut() = "cpu".into();
        if let Some(record) = &self.on_fallback {
            record(&reason);
        }
    }

    fn scdet(&self, path: &Path) -> Result<ScanParts, MediaError> {
        let tmp = tempfile::Builder::new()
            .prefix("plexdb-scan-")
            .tempdir()
            .map_err(|err| {
                MediaError::new(format!("{}: {err}", path.display()), "scratch unavailable")
            })?;
        let crc = tmp.path().join("frames.crc");
        let argv = || -> Vec<String> {
            let mut argv: Vec<String> = [
                self.ffmpeg.as_str(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
            ]
            .iter()
            .map(|s| s.to_string())
            .collect();
            argv.extend(self.hwaccel());
            argv.extend(
                [
                    "-i",
                    &path.to_string_lossy(),
                    "-filter_complex",
                    "[0:v:0]scale=-2:224,scdet=t=10:s=0,metadata=print:file=-[all]",
                    "-map",
                    "[all]",
                    "-fps_mode",
                    "passthrough",
                    "-f",
                    "framecrc",
                    "-y",
                    &crc.to_string_lossy(),
                ]
                .iter()
                .map(|s| s.to_string()),
            );
            argv
        };
        let what = format!("{}: scdet", path.display());
        let mut done = run(&argv(), &what, DECODE_TIMEOUT)?;
        if done.code != 0 && *self.decode.borrow() == "cuda" {
            let cuda_stderr = done.stderr.clone();
            *self.decode.borrow_mut() = "cpu".into();
            done = run(&argv(), &what, DECODE_TIMEOUT)?;
            if done.code == 0 {
                self.fall_back(&cuda_stderr);
            } else {
                *self.decode.borrow_mut() = "cuda".into();
            }
        }
        if done.code != 0 {
            return Err(MediaError::new(
                format!(
                    "{}: ffmpeg scdet exited {}: {}",
                    path.display(),
                    done.code,
                    tail200(&done.stderr)
                ),
                format!("ffmpeg scene scan exited {}", done.code),
            ));
        }
        let (errors, warnings) = classify_decode_stderr(&done.stderr);
        if !errors.is_empty() {
            let kept: Vec<&str> = errors
                .iter()
                .take(ERROR_LINES_KEPT)
                .map(String::as_str)
                .collect();
            return Err(MediaError::new(
                format!(
                    "{}: source file damaged ({} decode errors, {} decode); first {ERROR_LINES_KEPT}: {}",
                    path.display(),
                    errors.len(),
                    self.decode.borrow(),
                    kept.join(" | ")
                ),
                format!("source file damaged ({} decode errors)", errors.len()),
            ));
        }
        let listing = std::fs::read_to_string(&crc).map_err(|err| {
            MediaError::new(format!("{}: {err}", crc.display()), "frame list unreadable")
        })?;
        let last_frame_ms = parse_last_frame(listing.lines())?.ok_or_else(|| {
            MediaError::new(
                format!("{}: ffmpeg decoded no video frame", path.display()),
                "no video frame decoded",
            )
        })?;
        let cuts = parse_scdet(&String::from_utf8_lossy(&done.stdout))?;
        Ok((cuts, last_frame_ms, warnings))
    }

    fn chapters(&self, path: &Path) -> Result<Vec<Cut>, MediaError> {
        let argv: Vec<String> = [
            self.ffprobe.as_str(),
            "-v",
            "error",
            "-show_chapters",
            "-print_format",
            "json",
            &path.to_string_lossy(),
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let done = run(
            &argv,
            &format!("{}: chapters", path.display()),
            PROBE_TIMEOUT,
        )?;
        if done.code != 0 {
            return Err(MediaError::new(
                format!(
                    "{}: ffprobe exited {}: {}",
                    path.display(),
                    done.code,
                    tail200(&done.stderr)
                ),
                format!("ffprobe exited {}", done.code),
            ));
        }
        parse_chapters(&String::from_utf8_lossy(&done.stdout))
    }

    fn cut_chunk(&self, path: &Path, chunk: &[i64]) -> Result<Jpegs, MediaError> {
        let tmp = tempfile::Builder::new()
            .prefix("plexdb-frames-")
            .tempdir()
            .map_err(|err| {
                MediaError::new(format!("{}: {err}", path.display()), "scratch unavailable")
            })?;
        let mut argv: Vec<String> = [
            self.ffmpeg.as_str(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        // One decoder thread per input: each of the chunk's inputs is its own decoder,
        // and at the default of one thread per core a 4K chunk peaked at 16 GB (4 GB at
        // one thread, and faster, since the inputs already run side by side).
        for t_ms in chunk {
            argv.extend([
                "-threads".into(),
                "1".into(),
                "-ss".into(),
                format!("{:.3}", *t_ms as f64 / 1000.0),
                "-i".into(),
                path.to_string_lossy().into_owned(),
            ]);
        }
        for ix in 0..chunk.len() {
            argv.extend([
                "-map".into(),
                format!("{ix}:v:0"),
                "-frames:v".into(),
                "1".into(),
                "-vf".into(),
                "scale=-2:224".into(),
                "-q:v".into(),
                "3".into(),
                "-y".into(),
                tmp.path()
                    .join(format!("{ix}.jpg"))
                    .to_string_lossy()
                    .into_owned(),
            ]);
        }
        let ran = run(&argv, &format!("{}: frames", path.display()), PROBE_TIMEOUT)?;
        let found: Vec<Option<Vec<u8>>> = (0..chunk.len())
            .map(|ix| std::fs::read(tmp.path().join(format!("{ix}.jpg"))).ok())
            .collect();
        // An input seeked past the file's last frame writes no JPEG and makes ffmpeg exit
        // non-zero; every other input still writes its own, so a None is that time's miss.
        if ran.code != 0
            && !found
                .iter()
                .any(|jpeg| jpeg.as_ref().is_some_and(|b| !b.is_empty()))
        {
            return Err(MediaError::new(
                format!(
                    "{}: ffmpeg exited {}: {}",
                    path.display(),
                    ran.code,
                    tail200(&ran.stderr)
                ),
                format!("ffmpeg frame cut exited {}", ran.code),
            ));
        }
        Ok(found)
    }
}

impl MediaReader for FfmpegMedia {
    /// sha256 over the part's size, its modification time and its first MiB.
    fn file_hash(&self, path: &Path, size: i64) -> Result<String, MediaError> {
        let unreadable = |err: std::io::Error| {
            MediaError::new(
                format!(
                    "{}: cannot read: {}",
                    path.display(),
                    crate::frames::strerror(&err)
                ),
                "file unreadable",
            )
        };
        let modified = std::fs::metadata(path)
            .map_err(unreadable)?
            .modified()
            .map_err(unreadable)?;
        let mut head = Vec::new();
        std::fs::File::open(path)
            .map_err(unreadable)?
            .take(HASH_BYTES as u64)
            .read_to_end(&mut head)
            .map_err(unreadable)?;
        let secs = match modified.duration_since(std::time::UNIX_EPOCH) {
            Ok(since) => since.as_secs() as i64,
            Err(before) => -(before.duration().as_secs_f64().ceil() as i64),
        };
        let mut digest = Sha256::new();
        digest.update(format!("{size}|{}|", http_date(secs)).as_bytes());
        digest.update(&head);
        Ok(format!("{:x}", digest.finalize()))
    }

    fn scan(&self, path: &Path) -> Result<Scan, MediaError> {
        let (mut cuts, last_frame_ms, warnings) = self.scdet(path)?;
        cuts.extend(self.chapters(path)?);
        // Python sorts by (t_ms, source) with a stable sort.
        cuts.sort_by(|a, b| (a.t_ms, a.source).cmp(&(b.t_ms, b.source)));
        Ok(Scan {
            cuts,
            last_frame_ms,
            warnings,
        })
    }

    fn frames(&self, path: &Path, times_ms: &[i64]) -> Result<Jpegs, MediaError> {
        let mut out = Vec::with_capacity(times_ms.len());
        for chunk in times_ms.chunks(EXTRACT_CHUNK) {
            out.extend(self.cut_chunk(path, chunk)?);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roots(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn a_root_matches_only_at_a_path_boundary() {
        let roots = roots(&["/Volumes", "/Volumes/media"]);
        assert_eq!(canonical_path("/Volumes/media/a/b.mkv", &roots), "a/b.mkv");
        assert_eq!(
            canonical_path("/Volumes/mediacache/x.mkv", &roots),
            "mediacache/x.mkv"
        );
        assert_eq!(
            canonical_path("/elsewhere/x.mkv", &roots),
            "/elsewhere/x.mkv"
        );
    }

    #[test]
    fn local_path_refuses_a_path_outside_the_roots_or_the_media_dir() {
        let media = Path::new("/media");
        let roots = roots(&["/data"]);
        assert_eq!(
            local_path(media, "/data/movies/a.mkv", &roots).unwrap(),
            PathBuf::from("/media/movies/a.mkv")
        );
        let err = local_path(media, "/other/a.mkv", &roots).unwrap_err();
        assert_eq!(err.reason, "file not under PLEX_SOURCE_ROOTS");
        assert_eq!(
            err.message,
            "/other/a.mkv is under none of PLEX_SOURCE_ROOTS ['/data']"
        );
        let err = local_path(media, "/data/../../etc/passwd", &roots).unwrap_err();
        assert_eq!(err.reason, "file leaves PLEXDB_MEDIA_DIR");
    }

    #[test]
    fn scdet_cuts_pair_each_time_with_the_score_before_it() {
        let text =
            "frame:0 pts:0\nlavfi.scd.score=12.5\nlavfi.scd.time=10.0025\nlavfi.scd.time=0\n";
        let cuts = parse_scdet(text).unwrap();
        assert_eq!(
            cuts,
            vec![Cut {
                t_ms: 10002,
                score: Some(12.5),
                source: "scdet"
            }]
        );
        assert!(parse_scdet("lavfi.scd.time=abc").is_err());
    }

    #[test]
    fn the_last_frame_is_the_largest_pts_in_whole_milliseconds() {
        let listing = "#tb 0: 1/90000\n0, 0, 0, 3600, 100, 0x1\n0, 90090, 90090, 3600, 100, 0x2\n";
        assert_eq!(parse_last_frame(listing.lines()).unwrap(), Some(1001));
        assert_eq!(parse_last_frame("#tb 0: 1/25\n".lines()).unwrap(), None);
    }

    #[test]
    fn chapters_at_zero_are_not_cuts() {
        let body = r#"{"chapters":[{"start_time":"0.000000"},{"start_time":"61.5"}]}"#;
        let cuts = parse_chapters(body).unwrap();
        assert_eq!(cuts.len(), 1);
        assert_eq!(cuts[0].t_ms, 61500);
    }

    #[test]
    fn decode_errors_are_video_decoder_lines_with_an_error_word() {
        let stderr = "[hevc @ 0x1] Invalid NAL unit size\n[aac @ 0x2] Error here\nplain warning\n";
        let (errors, warnings) = classify_decode_stderr(stderr);
        assert_eq!(errors, vec!["[hevc @ 0x1] Invalid NAL unit size"]);
        assert_eq!(warnings, 2);
    }

    #[test]
    fn the_http_date_matches_rfc_2822() {
        assert_eq!(http_date(0), "Thu, 01 Jan 1970 00:00:00 GMT");
        assert_eq!(http_date(1_761_447_845), "Sun, 26 Oct 2025 03:04:05 GMT");
    }
}
