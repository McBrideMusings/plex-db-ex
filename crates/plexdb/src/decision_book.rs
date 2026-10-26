//! A Plex DB-EX decisions file as the explorer writes it (`plexdb/decisions.py`
//! `DecisionsFile`): read for the last entry per stored key, and appended to one
//! decision at a time under a lock, replaced by rename so a reader never sees half of it.
//!
//! The sweep's fold reads the same files with [`decisions::load`]; this module is the
//! writing half. Only the last entry per key counts, so a write keeps one entry per key
//! and the file's size is bounded by the number of rows the store holds.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, PoisonError};

use serde_json::Value;

use crate::decisions::{self, DecisionsFileError, Entry, Spec, py_tuple_repr};
use crate::pyjson::{Json, dumps_indent2};
use crate::staleness::Now;

/// A stored key: one text per field of the spec's key.
pub type Key = Vec<String>;

/// Every raw spelling of a stored keyword (`keyword_forms.surface`), shortest first then
/// alphabetical; a keyword with none is absent.
pub type Spellings = HashMap<String, Vec<String>>;

const MAX_ENTRIES: usize = 50_000;
const MAX_TOTAL_BYTES: usize = 8 << 20;

/// Why a decision could not be recorded: the request is wrong, or the file is.
#[derive(Debug)]
pub enum RecordError {
    Invalid(String),
    File(DecisionsFileError),
}

impl From<DecisionsFileError> for RecordError {
    fn from(err: DecisionsFileError) -> Self {
        RecordError::File(err)
    }
}

/// A file's last entry for each stored key, in the order the keys first appear.
#[derive(Default)]
pub struct Latest {
    pub entries: Vec<(Key, Entry)>,
    index: HashMap<Key, usize>,
}

impl Latest {
    pub fn get(&self, key: &[String]) -> Option<&Entry> {
        self.index.get(key).map(|&at| &self.entries[at].1)
    }

    /// A file of one entry, as the response to a write lays it over the table.
    pub fn single(key: Key, entry: Entry) -> Self {
        let mut out = Latest::default();
        out.put(key, entry);
        out
    }

    fn put(&mut self, key: Key, entry: Entry) {
        match self.index.get(&key) {
            Some(&at) => self.entries[at].1 = entry,
            None => {
                self.index.insert(key.clone(), self.entries.len());
                self.entries.push((key, entry));
            }
        }
    }
}

/// One decisions file: its shape, how an entry's spelling becomes a stored key, and where
/// it sits.
pub struct Book {
    spec: &'static Spec,
    /// What the file holds, for messages ("merge decisions").
    noun: &'static str,
    /// The stored key an entry's key names under the current normalization, or `None`
    /// when it names nothing a row could be keyed on.
    resolve: fn(&[String]) -> Option<Key>,
    /// Each entry key that could name stored `key`, in `spellings` order.
    spell: fn(&[String], &Spellings) -> Vec<Key>,
    path: PathBuf,
    lock: Mutex<()>,
}

impl Book {
    pub fn new(
        spec: &'static Spec,
        noun: &'static str,
        resolve: fn(&[String]) -> Option<Key>,
        spell: fn(&[String], &Spellings) -> Vec<Key>,
        path: PathBuf,
    ) -> Self {
        Book {
            spec,
            noun,
            resolve,
            spell,
            path,
            lock: Mutex::new(()),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The file's entries beside the stored key each names, and the raw JSON objects they
    /// were read from, or empty when there is no file.
    fn read(&self) -> Result<Vec<(Option<Key>, Entry, Value)>, DecisionsFileError> {
        let text = match std::fs::read(&self.path) {
            Ok(bytes) => String::from_utf8(bytes).map_err(|err| {
                DecisionsFileError(format!("{} is not UTF-8 text: {err}", self.path.display()))
            })?,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(err) => {
                return Err(DecisionsFileError(format!(
                    "cannot read {}: {err}",
                    self.path.display()
                )));
            }
        };
        let entries = decisions::parse(self.spec, &self.path, &text)?;
        let raw: Vec<Value> = serde_json::from_str(&text).unwrap_or_default();
        Ok(entries
            .into_iter()
            .zip(raw)
            .map(|(entry, raw)| ((self.resolve)(&entry.key), entry, raw))
            .collect())
    }

    /// The last entry for each stored key; an entry naming none is left out.
    pub fn latest(&self) -> Result<Latest, DecisionsFileError> {
        let entries = {
            let _held = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
            self.read()?
        };
        let mut out = Latest::default();
        for (key, entry, _) in entries {
            if let Some(key) = key {
                out.put(key, entry);
            }
        }
        Ok(out)
    }

    /// Append one decision on stored `key`, spelled by the first of `spellings` that
    /// resolves back to it, carrying `values`, dropping every earlier entry naming the
    /// same key; return the entry as it was written.
    ///
    /// The caller has checked the fields and that the store has a row for `key`.
    pub fn record(
        &self,
        key: &[String],
        decision: &str,
        spellings: &Spellings,
        values: &[String],
    ) -> Result<(Json, Entry), RecordError> {
        let spelled = (self.spell)(key, spellings)
            .into_iter()
            .find(|candidate| (self.resolve)(candidate).as_deref() == Some(key))
            .ok_or_else(|| {
                let items: Vec<&str> = key.iter().map(String::as_str).collect();
                RecordError::Invalid(format!(
                    "no spelling of {} normalizes back to it",
                    py_tuple_repr(&items)
                ))
            })?;
        let mut fields: Vec<(String, Json)> = self
            .spec
            .key
            .iter()
            .zip(&spelled)
            .map(|(name, text)| ((*name).to_string(), Json::str(text.clone())))
            .collect();
        fields.push(("decision".into(), Json::str(decision)));
        if decision != "cleared" {
            for (name, text) in self.spec.values.iter().zip(values) {
                fields.push(((*name).to_string(), Json::str(text.clone())));
            }
        }
        let decided_at = Now::read().iso();
        fields.push(("decided_at".into(), Json::str(decided_at.clone())));
        let written = Entry {
            key: spelled.clone(),
            decision: decision.to_string(),
            values: if decision == "cleared" {
                Vec::new()
            } else {
                values.to_vec()
            },
            decided_at,
        };
        let entry = Json::Object(fields);

        let _held = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        let mut kept: Vec<Json> = self
            .read()?
            .into_iter()
            .filter(|(named, _, _)| named.as_deref() != Some(key))
            .map(|(_, _, raw)| Json::from(&raw))
            .collect();
        kept.push(entry.clone());
        if kept.len() > MAX_ENTRIES {
            return Err(RecordError::Invalid(format!(
                "too many {} (max {MAX_ENTRIES})",
                self.noun
            )));
        }
        let mut body = dumps_indent2(&Json::Array(kept));
        body.push('\n');
        if body.len() > MAX_TOTAL_BYTES {
            return Err(RecordError::Invalid(format!(
                "{} would exceed {MAX_TOTAL_BYTES} bytes",
                self.noun
            )));
        }
        self.write(&body)?;
        Ok((entry, written))
    }

    fn write(&self, body: &str) -> Result<(), DecisionsFileError> {
        let mut name = self.path.file_name().unwrap_or_default().to_os_string();
        name.push(".tmp");
        let tmp = self.path.with_file_name(name);
        let result = std::fs::write(&tmp, body).and_then(|()| std::fs::rename(&tmp, &self.path));
        if result.is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
        result.map_err(|err| {
            DecisionsFileError(format!("cannot write {}: {err}", self.path.display()))
        })
    }
}
