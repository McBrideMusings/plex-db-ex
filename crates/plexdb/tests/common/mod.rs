//! A recorded Plex and Tautulli, and a store built by the real `plexdb migrate`, so a
//! step's test fails when a migration changes a table under it.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use plexdb::migrate::migrate;
use plexdb::plex::{PlexSource, Section, TYPE_EPISODE, TYPE_MOVIE, TYPE_SHOW};
use plexdb::tautulli::TautulliSource;
use plexdb_store::StoreError;
use rusqlite::Connection;
use serde_json::{Value, json};

pub fn fixture(name: &str) -> Value {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("run the tests through cargo");
    let path: PathBuf = Path::new(&manifest).join("../../tests/fixtures").join(name);
    let text = std::fs::read_to_string(&path).expect("read the fixture");
    serde_json::from_str(&text).expect("the fixture is JSON")
}

fn container(name: &str, key: &str) -> Vec<Value> {
    fixture(name)["MediaContainer"][key]
        .as_array()
        .expect("a list")
        .clone()
}

/// A migrated store at `dir/plexdb.db`.
pub fn store(dir: &Path) -> Connection {
    let path = dir.join("plexdb.db");
    migrate(&path, &dir.join("backups")).expect("plexdb migrate builds the store");
    let conn = Connection::open(&path).expect("open the store");
    conn.pragma_update(None, "foreign_keys", "ON")
        .expect("foreign keys on");
    conn
}

/// A `PlexSource` over in-memory records, counting what it was asked.
#[derive(Default)]
pub struct FakePlex {
    pub sections: Vec<Section>,
    pub records: HashMap<(String, i64), Vec<Value>>,
    pub full: HashMap<String, Value>,
    pub events: Vec<Value>,
    pub devices: Vec<Value>,
    pub accounts: Vec<Value>,
    /// Ignore the `viewedAt>` cutoff, as a source re-reporting a recorded event does.
    pub ignore_cutoff: bool,
    pub metadata_requests: RefCell<Vec<String>>,
    pub history_cutoffs: RefCell<Vec<Option<i64>>>,
}

impl FakePlex {
    pub fn set(&mut self, section: &str, plex_type: i64, records: Vec<Value>) {
        self.records.insert((section.into(), plex_type), records);
    }

    pub fn records_mut(&mut self, section: &str, plex_type: i64) -> &mut Vec<Value> {
        self.records
            .get_mut(&(section.into(), plex_type))
            .expect("records")
    }
}

impl PlexSource for FakePlex {
    fn sections(&self) -> Result<Vec<Section>, StoreError> {
        Ok(self.sections.clone())
    }

    fn items(&self, section_key: &str, plex_type: i64) -> Result<Vec<Value>, StoreError> {
        Ok(self
            .records
            .get(&(section_key.into(), plex_type))
            .cloned()
            .unwrap_or_default())
    }

    fn metadata(&self, rating_key: &str) -> Result<Option<Value>, StoreError> {
        self.metadata_requests.borrow_mut().push(rating_key.into());
        Ok(self.full.get(rating_key).cloned())
    }

    /// Honours the cutoff the way Plex's `viewedAt>` does: inclusive, despite its name.
    fn history(&self, since_viewed_at: Option<i64>) -> Result<Vec<Value>, StoreError> {
        self.history_cutoffs.borrow_mut().push(since_viewed_at);
        let since = if self.ignore_cutoff {
            None
        } else {
            since_viewed_at
        };
        Ok(self
            .events
            .iter()
            .filter(|event| since.is_none_or(|at| event["viewedAt"].as_i64().unwrap_or(0) >= at))
            .cloned()
            .collect())
    }

    fn devices(&self) -> Result<Vec<Value>, StoreError> {
        Ok(self.devices.clone())
    }

    fn accounts(&self) -> Result<Vec<Value>, StoreError> {
        Ok(self.accounts.clone())
    }
}

/// The four real sections, with the recorded movie, show and episode fixtures wired to
/// the sections they came from (Movies = 1, TV Shows = 2), plus the recorded history
/// and devices. Concerts and Power Hours are real sections with no recording.
pub fn recorded() -> FakePlex {
    let sections = fixture("plex/sections.json")["MediaContainer"]["Directory"]
        .as_array()
        .expect("directories")
        .iter()
        .map(|entry| Section {
            key: entry["key"].to_string().trim_matches('"').to_string(),
            kind: entry["type"].as_str().expect("a type").into(),
            title: entry["title"].as_str().unwrap_or("").into(),
        })
        .collect();
    let mut plex = FakePlex {
        sections,
        events: container("plex/history.json", "Metadata"),
        devices: container("plex/devices.json", "Device"),
        ..FakePlex::default()
    };
    plex.set(
        "1",
        TYPE_MOVIE,
        container("plex/section_1_type_1.json", "Metadata"),
    );
    plex.set(
        "2",
        TYPE_SHOW,
        container("plex/section_2_type_2.json", "Metadata"),
    );
    plex.set(
        "2",
        TYPE_EPISODE,
        container("plex/section_2_type_4.json", "Metadata"),
    );
    plex
}

/// A one-section movie library over `records`.
pub fn movies(records: Vec<Value>) -> FakePlex {
    let mut plex = FakePlex {
        sections: vec![Section {
            key: "1".into(),
            kind: "movie".into(),
            title: "Movies".into(),
        }],
        ..FakePlex::default()
    };
    plex.set("1", TYPE_MOVIE, records);
    plex
}

/// A movie record with one GUID and one file.
pub fn movie(rating_key: &str, title: &str, guid: &str, file: &str) -> Value {
    json!({
        "ratingKey": rating_key,
        "type": "movie",
        "title": title,
        "year": 2000,
        "Guid": [{"id": guid}],
        "Media": [{"Part": [{"file": file}]}],
    })
}

pub struct FakeTautulli {
    pub rows: Vec<Value>,
    pub users: Vec<Value>,
}

impl TautulliSource for FakeTautulli {
    fn history(&self) -> Result<Vec<Value>, StoreError> {
        Ok(self.rows.clone())
    }

    fn users(&self) -> Result<Vec<Value>, StoreError> {
        Ok(self.users.clone())
    }
}

pub fn one<T: rusqlite::types::FromSql>(conn: &Connection, sql: &str) -> T {
    conn.query_row(sql, [], |row| row.get(0))
        .expect("one value")
}

pub fn texts(conn: &Connection, sql: &str) -> Vec<String> {
    conn.prepare(sql)
        .expect("prepare")
        .query_map([], |row| row.get::<_, String>(0))
        .expect("query")
        .collect::<Result<_, _>>()
        .expect("rows")
}
