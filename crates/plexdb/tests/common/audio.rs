//! The world the `harvest_audio*` tests share: a store built by the real migrations, a
//! media dir of small files in a temp dir, three Plex sections and a settable clock.

use std::cell::Cell;
use std::path::PathBuf;
use std::time::Duration;

use plexdb::harvest_audio::{Analyzer, Report, Settings, harvest};
use plexdb::plex::Section;
use plexdb::steps::Failure;
use rusqlite::Connection;

pub const T0: i64 = 1_800_000_000;
pub const HOUR: i64 = 3600;
pub const MIN_20: Option<i64> = Some(20 * 60_000);

pub struct World {
    pub _dir: tempfile::TempDir,
    pub store: PathBuf,
    pub media: PathBuf,
    pub conn: Connection,
    pub settings: Settings,
    pub sections: Vec<Section>,
    pub clock: Cell<i64>,
}

impl World {
    pub fn new() -> World {
        let dir = tempfile::tempdir().expect("tempdir");
        let conn = super::store(dir.path());
        let media = dir.path().join("media");
        std::fs::create_dir(&media).expect("media dir");
        let section = |key: &str, title: &str| Section {
            key: key.into(),
            kind: "movie".into(),
            title: title.into(),
        };
        let settings = Settings {
            libraries: vec!["Movies".into()],
            analyzer: "unused".into(),
            ffprobe: "unused".into(),
            timeout: Duration::from_secs(900),
            media_dir: media.clone(),
            source_roots: vec!["/plexroot".into()],
        };
        World {
            store: dir.path().join("plexdb.db"),
            _dir: dir,
            media,
            conn,
            settings,
            sections: vec![
                section("1", "Movies"),
                section("2", "TV Shows"),
                section("7", "Photos"),
            ],
            clock: Cell::new(T0),
        }
    }

    /// A media part in `section`: its file under the media dir holds `content`.
    pub fn part(
        &self,
        id: i64,
        section: &str,
        name: &str,
        content: &[u8],
        duration_ms: Option<i64>,
    ) {
        if !content.is_empty() {
            let path = self.media.join(name);
            std::fs::write(&path, content).expect("write media");
            let file = std::fs::File::options()
                .write(true)
                .open(&path)
                .expect("open");
            file.set_modified(
                std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000),
            )
            .expect("same mtime for every file");
        }
        self.row(id, section, name, content.len() as i64, duration_ms);
    }

    pub fn row(&self, id: i64, section: &str, name: &str, size: i64, duration_ms: Option<i64>) {
        let item = format!("plex:{id}");
        let key = format!("rk{id}");
        self.conn
            .execute(
                "INSERT INTO items (item_id, type, title) VALUES (?1, 'movie', ?1)",
                [&item],
            )
            .expect("item");
        self.conn
            .execute(
                "INSERT INTO plex_items (rating_key, item_id, section_id, last_seen) \
                 VALUES (?1, ?2, ?3, 'x')",
                [&key, &item, section],
            )
            .expect("plex item");
        self.conn
            .execute(
                "INSERT INTO media_part (part_id, item_id, rating_key, file, size, duration_ms, \
                 fetched_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'x')",
                rusqlite::params![
                    id,
                    item,
                    key,
                    format!("/plexroot/{name}"),
                    size,
                    duration_ms
                ],
            )
            .expect("part");
    }

    /// An extra in `library` whose primary file `name` under the media dir holds `content`.
    /// Its `extra_file.path` is `/plexroot/<name>`, as Plex-style roots map it.
    pub fn extra(
        &self,
        library: &str,
        item: &str,
        first_seen: &str,
        name: &str,
        content: &[u8],
        duration_ms: Option<i64>,
    ) {
        let path = self.media.join(name);
        std::fs::write(&path, content).expect("write media");
        let file = std::fs::File::options()
            .write(true)
            .open(&path)
            .expect("open");
        file.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000))
            .expect("same mtime for every file");
        self.conn
            .execute(
                "INSERT OR IGNORE INTO extra_library (name, root, type, declared_at) \
                 VALUES (?1, '/plexroot', 'video', 't')",
                [library],
            )
            .expect("extra library");
        self.conn
            .execute(
                "INSERT INTO items (item_id, type, title) VALUES (?1, 'extra', ?1)",
                [item],
            )
            .expect("extra item row");
        self.conn
            .execute(
                "INSERT INTO extra_item (item_id, library, organized, first_seen) \
                 VALUES (?1, ?2, 1, ?3)",
                [item, library, first_seen],
            )
            .expect("extra_item");
        self.conn
            .execute(
                "INSERT INTO extra_file (path, item_id, primary_file, size, duration_ms, \
                 first_seen, last_seen) VALUES (?1, ?2, 1, ?3, ?4, 't', 't')",
                rusqlite::params![
                    format!("/plexroot/{name}"),
                    item,
                    content.len() as i64,
                    duration_ms
                ],
            )
            .expect("extra_file");
    }

    pub fn extra_state(&self, path: &str) -> (String, Option<String>) {
        self.conn
            .query_row(
                "SELECT outcome, error FROM audio_extra_state WHERE path = ?1",
                [path],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("an extra state row")
    }

    pub fn run(&self, analyzer: &dyn Analyzer) -> Result<Report, Failure> {
        harvest(
            &self.store,
            &self.settings,
            &self.sections,
            analyzer,
            &|| self.clock.get(),
            &mut |_| {},
        )
    }

    pub fn state(&self, id: i64) -> (String, i64, Option<String>, Option<String>) {
        self.conn
            .query_row(
                "SELECT outcome, attempts, next_try_at, error FROM audio_harvest_state \
                 WHERE part_id = ?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .expect("a state row")
    }

    pub fn count(&self, table: &str) -> i64 {
        self.conn
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .expect("count")
    }

    pub fn has_state(&self, id: i64) -> bool {
        self.conn
            .query_row(
                "SELECT count(*) FROM audio_harvest_state WHERE part_id = ?1",
                [id],
                |row| row.get::<_, i64>(0),
            )
            .expect("count")
            > 0
    }
}
