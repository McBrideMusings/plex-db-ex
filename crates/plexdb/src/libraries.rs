//! Ordered library names, shared by `harvest-audio` (`PLEXDB_AUDIO_LIBRARIES`) and the
//! `harvest-frames` priority (`PLEXDB_FRAME_LIBRARIES`) (plex-db-ex-9ak.5).
//!
//! A library name is a Plex section **title** or an `extra_library.name`. The store keeps no
//! section titles, so a caller fetches Plex's sections once and hands them to [`resolve`],
//! which is a pure function: names, sections and extras names in, ordered libraries and the
//! names that matched nothing out. A name that matches a section title and an extras
//! library resolves to the section.

use rusqlite::Connection;

use crate::plex::Section;

/// What a resolved library is made of.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LibraryKind {
    /// A Plex section, by its id (`plex_items.section_id`).
    Section(String),
    /// An `extra_library`.
    Extras,
}

/// One library of the list, in the order it was named.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Library {
    pub name: String,
    pub kind: LibraryKind,
}

/// What a list of names resolved to.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Resolved {
    pub libraries: Vec<Library>,
    /// Names that are neither a section title nor an extras library, in list order.
    pub unknown: Vec<String>,
}

/// A comma-separated list of names: trimmed, blanks dropped, a repeat dropped at its later
/// position.
pub fn parse_names(raw: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for part in raw.split(',') {
        let name = part.trim();
        if !name.is_empty() && !names.iter().any(|seen| seen == name) {
            names.push(name.to_string());
        }
    }
    names
}

/// The list in variable `var`; empty when unset.
pub fn names_from_env(var: &str) -> Vec<String> {
    parse_names(&std::env::var(var).unwrap_or_default())
}

/// Match each name against the section titles, then the extras library names.
pub fn resolve(names: &[String], sections: &[Section], extras: &[String]) -> Resolved {
    let mut out = Resolved::default();
    for name in names {
        if out.libraries.iter().any(|lib| &lib.name == name) || out.unknown.contains(name) {
            continue;
        }
        if let Some(section) = sections.iter().find(|section| &section.title == name) {
            out.libraries.push(Library {
                name: name.clone(),
                kind: LibraryKind::Section(section.key.clone()),
            });
        } else if extras.iter().any(|extra| extra == name) {
            out.libraries.push(Library {
                name: name.clone(),
                kind: LibraryKind::Extras,
            });
        } else {
            out.unknown.push(name.clone());
        }
    }
    out
}

/// Every `extra_library.name` in the store.
pub fn extra_library_names(conn: &Connection) -> rusqlite::Result<Vec<String>> {
    conn.prepare("SELECT name FROM extra_library ORDER BY name")?
        .query_map([], |row| row.get(0))?
        .collect()
}

/// The sentence a report says of names that matched nothing.
pub fn unknown_note(name: &str) -> String {
    format!("unknown library {name}: not a Plex section title or an extras library; skipped")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn section(key: &str, title: &str) -> Section {
        Section {
            key: key.into(),
            kind: "movie".into(),
            title: title.into(),
        }
    }

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn names_are_trimmed_and_deduped_keeping_the_first_position() {
        assert_eq!(
            parse_names(" Music Videos, Movies ,,Music Videos,TV"),
            names(&["Music Videos", "Movies", "TV"])
        );
        assert!(parse_names("  ").is_empty());
    }

    #[test]
    fn titles_and_extras_resolve_in_list_order_and_the_rest_are_unknown() {
        let resolved = resolve(
            &names(&["Music Videos", "Movies", "Nope", "Movies", "Nope"]),
            &[section("1", "Movies"), section("2", "TV Shows")],
            &names(&["Music Videos"]),
        );
        assert_eq!(
            resolved.libraries,
            vec![
                Library {
                    name: "Music Videos".into(),
                    kind: LibraryKind::Extras
                },
                Library {
                    name: "Movies".into(),
                    kind: LibraryKind::Section("1".into())
                },
            ]
        );
        assert_eq!(resolved.unknown, names(&["Nope"]));
    }

    #[test]
    fn a_section_title_wins_over_an_extras_name() {
        let resolved = resolve(
            &names(&["Clips"]),
            &[section("5", "Clips")],
            &names(&["Clips"]),
        );
        assert_eq!(resolved.libraries[0].kind, LibraryKind::Section("5".into()));
    }
}
