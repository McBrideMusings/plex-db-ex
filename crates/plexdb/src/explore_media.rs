//! The Search tab's frames and clips: cut by ffmpeg from the part's file under
//! `PLEXDB_MEDIA_DIR` (found by asking Plex which file the part is), and kept in one
//! least-recently-used cache under `<explore data>/clips` (`plexdb/explore_search.py`'s
//! `MediaCache`, `_file`, `_cut`, `frame` and `clip`).
//!
//! Every ffmpeg run and every Plex call here blocks, so the server calls into this module
//! from tokio's blocking pool only.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, PoisonError};
use std::time::{Duration, Instant};

use plexdb_store::StoreError;

use crate::explore_views::ExploreError;
use crate::frame_media::{FRAME_HEIGHT, FfmpegMedia, MediaError, local_path};
use crate::frames::FrameConfig;
use crate::plex::PlexClient;

/// Shared least-recently-used ceiling of every cached frame and clip.
pub const CACHE_BYTES: u64 = 2 << 30;
/// ffmpeg cuts at once. Plex DB-EX has no login, so this bounds what an anonymous caller
/// can spend.
const CUT_SLOTS: usize = 3;
/// How long a frame or clip waits for a free ffmpeg slot.
const CUT_WAIT: Duration = Duration::from_secs(120);
pub const LIGHTBOX_HEIGHT: u32 = 540;
pub const CLIP_WIDTH: u32 = 640;
/// The longest window a clip may cut: a Shot is seconds, never minutes.
pub const MAX_CLIP_MS: i64 = 120_000;
/// The `part` in the URL of an extra's frame or clip: no Plex part exists, and Plex's start at 1.
pub const EXTRA_PART: i64 = 0;

// --- the media cache -------------------------------------------------------------

/// Cut frames and clips on disk, evicting the least recently used past `cap`.
pub struct MediaCache {
    pub root: PathBuf,
    cap: u64,
    keys: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    /// Bytes under `root`, counted once from the disk and kept up as cuts land, so a write
    /// walks the tree only when the total has crossed `cap`.
    total: Mutex<Option<u64>>,
}

/// `os.utime(path)`: a hit refreshes the file's time; absent or just evicted is an error.
fn touch(path: &Path) -> std::io::Result<()> {
    let c_path = std::ffi::CString::new(path.as_os_str().as_encoded_bytes())
        .map_err(|_| std::io::Error::other("path holds a NUL"))?;
    // SAFETY: `utimes` with a null times pointer sets both times to now; the path is a
    // valid NUL-terminated string for the call's duration.
    let code = unsafe { libc::utimes(c_path.as_ptr(), std::ptr::null()) };
    if code == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

impl MediaCache {
    pub fn new(root: PathBuf, cap: u64) -> Self {
        MediaCache {
            root,
            cap,
            keys: Mutex::new(HashMap::new()),
            total: Mutex::new(None),
        }
    }

    pub fn path(&self, name: &str) -> PathBuf {
        self.root.join(name)
    }

    /// The cached file for `name`, made by `make` once however many ask.
    pub fn get(
        &self,
        name: &str,
        make: impl FnOnce() -> Result<Vec<u8>, ExploreError>,
    ) -> Result<PathBuf, ExploreError> {
        let target = self.path(name);
        let gate = self
            .keys
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .entry(name.to_string())
            .or_default()
            .clone();
        let outcome = {
            let _held = gate.lock().unwrap_or_else(PoisonError::into_inner);
            if touch(&target).is_ok() {
                Ok(target.clone())
            } else {
                self.write(&target, make)
            }
        };
        self.keys
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(name);
        outcome
    }

    fn write(
        &self,
        target: &Path,
        make: impl FnOnce() -> Result<Vec<u8>, ExploreError>,
    ) -> Result<PathBuf, ExploreError> {
        let data = make()?;
        let io = |err: std::io::Error| {
            ExploreError::Internal(format!("cannot write {}: {err}", target.display()))
        };
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(io)?;
        }
        let mut partial = target.as_os_str().to_owned();
        partial.push(".part");
        let partial = PathBuf::from(partial);
        std::fs::write(&partial, &data).map_err(io)?;
        std::fs::rename(&partial, target).map_err(io)?;
        self.account(data.len() as u64);
        Ok(target.to_path_buf())
    }

    /// Add a landed cut to the running total; past the cap, evict the least recently used
    /// files until the total fits, and take the total from that walk.
    fn account(&self, added: u64) {
        let mut total = self.total.lock().unwrap_or_else(PoisonError::into_inner);
        let known = match *total {
            Some(known) => known + added,
            None => {
                let mut files = Vec::new();
                walk(&self.root, &mut files);
                files.iter().map(|(_, size, _)| *size).sum()
            }
        };
        if known <= self.cap {
            *total = Some(known);
            return;
        }
        let mut files: Vec<(PathBuf, u64, std::time::SystemTime)> = Vec::new();
        walk(&self.root, &mut files);
        let mut left: u64 = files.iter().map(|(_, size, _)| *size).sum();
        files.sort_by_key(|(_, _, modified)| *modified);
        for (victim, size, _) in files {
            if left <= self.cap {
                break;
            }
            if std::fs::remove_file(victim).is_ok() {
                left -= size;
            }
        }
        *total = Some(left);
    }
}

/// Every file under `dir` that is not a `.part`, with its size and modification time.
fn walk(dir: &Path, out: &mut Vec<(PathBuf, u64, std::time::SystemTime)>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_dir() {
            walk(&path, out);
        } else if meta.is_file() && path.extension().is_none_or(|ext| ext != "part") {
            let modified = meta.modified().unwrap_or(std::time::UNIX_EPOCH);
            out.push((path, meta.len(), modified));
        }
    }
}

// --- the cut slots -----------------------------------------------------------------

/// `threading.BoundedSemaphore(3)` with a timed acquire: a results page asks for thirty
/// thumbnails at once; they take turns rather than fail.
struct CutSlots {
    free: Mutex<usize>,
    freed: Condvar,
}

struct CutSlot<'a>(&'a CutSlots);

impl CutSlots {
    fn take(&self, wait: Duration) -> Option<CutSlot<'_>> {
        let deadline = Instant::now() + wait;
        let mut free = self.free.lock().unwrap_or_else(PoisonError::into_inner);
        while *free == 0 {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return None;
            }
            free = self
                .freed
                .wait_timeout(free, left)
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
        *free -= 1;
        Some(CutSlot(self))
    }
}

impl Drop for CutSlot<'_> {
    fn drop(&mut self) {
        *self.0.free.lock().unwrap_or_else(PoisonError::into_inner) += 1;
        self.0.freed.notify_one();
    }
}

// --- the files -------------------------------------------------------------------------

/// Why a cut did not happen: the file itself failed (logged, never shown), or something
/// before it (a missing part, Plex, the media settings) that is answered as it is.
pub enum CutError {
    Media(MediaError),
    Explore(ExploreError),
}

impl From<ExploreError> for CutError {
    fn from(err: ExploreError) -> Self {
        CutError::Explore(err)
    }
}

impl From<MediaError> for CutError {
    fn from(err: MediaError) -> Self {
        CutError::Media(err)
    }
}

impl From<rusqlite::Error> for CutError {
    fn from(err: rusqlite::Error) -> Self {
        CutError::Explore(err.into())
    }
}

impl From<StoreError> for CutError {
    fn from(err: StoreError) -> Self {
        CutError::Explore(err.into())
    }
}

/// Where a part's file is, how it is cut, and the cache the cuts land in.
pub struct Media {
    config: FrameConfig,
    store: PathBuf,
    pub cache: MediaCache,
    files: Mutex<HashMap<(String, i64), PathBuf>>,
    plex: Mutex<Option<Arc<PlexClient>>>,
    slots: CutSlots,
}

fn log(line: String) {
    eprintln!("explore: {line}");
}

impl Media {
    pub fn new(config: FrameConfig, store: PathBuf, data_dir: &Path) -> Self {
        Media {
            config,
            store,
            cache: MediaCache::new(data_dir.join("clips"), CACHE_BYTES),
            files: Mutex::new(HashMap::new()),
            plex: Mutex::new(None),
            slots: CutSlots {
                free: Mutex::new(CUT_SLOTS),
                freed: Condvar::new(),
            },
        }
    }

    /// Plex, reached once the first time it is needed.
    pub fn plex(&self) -> Result<Arc<PlexClient>, StoreError> {
        let mut held = self.plex.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(client) = held.as_ref() {
            return Ok(client.clone());
        }
        let client = Arc::new(PlexClient::new(
            &self.config.plex_url,
            &self.config.plex_token,
        )?);
        *held = Some(client.clone());
        Ok(client)
    }

    /// Why no frame can be cut, or `None` when everything is in place (`_media_problem`).
    pub fn problem(&self) -> Option<String> {
        let missing: Vec<&str> = [self.config.ffmpeg.as_str(), self.config.ffprobe.as_str()]
            .into_iter()
            .filter(|name| !crate::frames_cmd::which(name))
            .collect();
        if !missing.is_empty() {
            return Some(format!(
                "{} not found (PLEXDB_FFMPEG, PLEXDB_FFPROBE)",
                missing.join(" and ")
            ));
        }
        if self.config.plex_url.is_empty() || self.config.plex_token.is_empty() {
            return Some(
                "PLEX_URL and PLEX_TOKEN are not set, so a part's file cannot be found".into(),
            );
        }
        if let Err(err) = self.locator() {
            return Some(err);
        }
        if let Some(dir) = &self.config.media_dir
            && !dir.is_dir()
        {
            return Some(format!(
                "PLEXDB_MEDIA_DIR {} is not a readable directory",
                dir.display()
            ));
        }
        None
    }

    /// `frame_harvest.locator`: the media directory and roots, or the config error.
    fn locator(&self) -> Result<(&Path, &[String]), String> {
        let Some(media_dir) = &self.config.media_dir else {
            return Err(
                "PLEXDB_MEDIA_DIR is not set, so no video file can be read; mount the \
                        library read-only and name the directory Plex calls PLEX_SOURCE_ROOTS"
                    .into(),
            );
        };
        if self.config.source_roots.is_empty() {
            return Err(
                "PLEX_SOURCE_ROOTS is not set, so Plex's file paths cannot be mapped".into(),
            );
        }
        Ok((media_dir.as_path(), &self.config.source_roots))
    }

    fn ffmpeg(&self) -> Result<FfmpegMedia, ExploreError> {
        if let Some(problem) = self.problem() {
            return Err(ExploreError::Unavailable(problem));
        }
        Ok(FfmpegMedia::new(
            &self.config.ffmpeg,
            &self.config.ffprobe,
            &self.config.decode,
            None,
        ))
    }

    /// Where this process reads `part`'s file: Plex names it, the locator maps it (`_file`).
    fn file(&self, item_id: &str, part: i64) -> Result<PathBuf, CutError> {
        let key = (item_id.to_string(), part);
        if let Some(path) = self
            .files
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(&key)
        {
            return Ok(path.clone());
        }
        if let Some(problem) = self.problem() {
            return Err(ExploreError::Unavailable(problem).into());
        }
        let (media_dir, roots) = self.locator().map_err(ExploreError::Unavailable)?;
        let plex_file = if part == EXTRA_PART {
            self.extra_path(item_id)?
        } else {
            let conn = plexdb_store::open_readonly(&self.store)?;
            let keys: Vec<String> = conn
                .prepare("SELECT rating_key FROM plex_items WHERE item_id = ?1")?
                .query_map([item_id], |row| row.get::<_, rusqlite::types::Value>(0))?
                .collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .map(|value| match value {
                    rusqlite::types::Value::Integer(n) => n.to_string(),
                    rusqlite::types::Value::Text(text) => text,
                    other => format!("{other:?}"),
                })
                .collect();
            let plex = self.plex()?;
            let mut found = None;
            for key in &keys {
                match plex.part_file(key, part) {
                    Ok(Some(file)) => {
                        found = Some(file);
                        break;
                    }
                    Ok(None) => {}
                    Err(err) => {
                        log(format!("part {part} of {item_id}: {err}"));
                        return Err(ExploreError::Unavailable(
                            "Plex did not say where this title's file is".into(),
                        )
                        .into());
                    }
                }
            }
            found.ok_or_else(|| {
                ExploreError::NotFound(format!("Plex no longer lists part {part} for {item_id}"))
            })?
        };
        let path = local_path(media_dir, &plex_file, roots)?;
        self.files
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(key, path.clone());
        Ok(path)
    }

    /// Where the store says an extra's primary file is: Plex knows nothing of it.
    fn extra_path(&self, item_id: &str) -> Result<String, ExploreError> {
        let conn = plexdb_store::open_readonly(&self.store)?;
        conn.query_row(
            "SELECT path FROM extra_file WHERE item_id = ?1 AND primary_file = 1 \
             AND missing_since IS NULL",
            [item_id],
            |row| row.get::<_, String>(0),
        )
        .map(Some)
        .or_else(|err| match err {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })?
        .ok_or_else(|| ExploreError::NotFound(format!("{item_id} has no file on disk")))
    }

    /// Run `make` in a cut slot; a failure of the file itself is logged, forgotten and
    /// answered without the server's paths (`_cut`).
    fn cut(
        &self,
        item_id: &str,
        part: i64,
        make: impl FnOnce() -> Result<Vec<u8>, CutError>,
    ) -> Result<Vec<u8>, ExploreError> {
        let Some(_slot) = self.slots.take(CUT_WAIT) else {
            return Err(ExploreError::Unavailable(
                "the cuts ahead of this one took too long; try again".into(),
            ));
        };
        match make() {
            Ok(bytes) => Ok(bytes),
            Err(CutError::Media(err)) => {
                // The message names files on the server, and this page has no login: it
                // goes to the log, and the path is looked up again next time in case Plex
                // moved it.
                log(format!(
                    "cut failed for part {part} of {item_id}: {}",
                    err.message
                ));
                self.files
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner)
                    .remove(&(item_id.to_string(), part));
                Err(ExploreError::Unavailable(
                    "could not read this title's file; see the log".into(),
                ))
            }
            Err(CutError::Explore(other)) => Err(other),
        }
    }

    /// One JPEG at `t_ms`, `height` px tall, from the cache or cut now.
    pub fn frame(
        &self,
        item_id: &str,
        part: i64,
        t_ms: i64,
        large: bool,
    ) -> Result<PathBuf, ExploreError> {
        let height = if large { LIGHTBOX_HEIGHT } else { FRAME_HEIGHT };
        let name = format!("frames/{}-{part}-{t_ms}-{height}.jpg", slug(item_id));
        self.cache.get(&name, || {
            self.cut(item_id, part, || {
                let path = self.file(item_id, part)?;
                let jpeg = self.ffmpeg()?.frame_at(&path, t_ms, height)?;
                jpeg.ok_or_else(|| {
                    CutError::Media(MediaError::new(
                        format!("{}: no frame at {t_ms} ms", path.display()),
                        "no frame at that time",
                    ))
                })
            })
        })
    }

    /// The Shot's window as an MP4, from the cache or cut now.
    pub fn clip(
        &self,
        item_id: &str,
        part: i64,
        in_ms: i64,
        out_ms: i64,
    ) -> Result<PathBuf, ExploreError> {
        let name = format!("{}-{part}-{in_ms}-{out_ms}-{CLIP_WIDTH}.mp4", slug(item_id));
        self.cache.get(&name, || {
            self.cut(item_id, part, || {
                let path = self.file(item_id, part)?;
                Ok(self.ffmpeg()?.clip(&path, in_ms, out_ms, CLIP_WIDTH)?)
            })
        })
    }
}

/// `_slug`: the item_id with everything that is not alphanumeric as `_`.
pub fn slug(item_id: &str) -> String {
    item_id
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { '_' })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cache_evicts_the_least_recently_used_file_past_its_cap() {
        let dir = tempfile::tempdir().expect("tempdir");
        let cache = MediaCache::new(dir.path().join("clips"), 250);
        let make = |n: u8| move || Ok(vec![n; 100]);
        let first = cache.get("a.bin", make(1)).expect("a");
        std::thread::sleep(Duration::from_millis(20));
        cache.get("frames/b.jpg", make(2)).expect("b");
        std::thread::sleep(Duration::from_millis(20));
        // A hit on `a` makes `b` the oldest.
        cache.get("a.bin", || panic!("cached")).expect("hit");
        std::thread::sleep(Duration::from_millis(20));
        cache.get("c.bin", make(3)).expect("c");
        assert!(first.exists(), "a was touched, so it stays");
        assert!(
            !dir.path().join("clips/frames/b.jpg").exists(),
            "b was the oldest"
        );
        assert!(dir.path().join("clips/c.bin").exists());
    }

    #[test]
    fn the_slug_keeps_letters_and_digits() {
        assert_eq!(slug("imdb:tt1375666"), "imdb_tt1375666");
        assert_eq!(slug("fs:ab/é"), "fs_ab_é");
    }
}
