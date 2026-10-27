//! `extras-organize`: act on a person's `organize` decisions (ADR-0030).
//!
//! An organize decision moves an extra's primary file to
//! `root/<library template over the item's field values>.<extension>`, or leaves a file
//! with no template where it is. It refuses while a required field is empty or a field
//! the template names has no value. A target holding the same SHA-256 deletes the
//! duplicate; any other file there leaves the extra unorganized and reports "target taken";
//! otherwise the file is renamed, an `extra_moved` row is written, `extra_file.path`
//! changes, the id stays and `organized` becomes 1. Emptied folders are left in place.
//!
//! A decision is acted on while the item's `organized_at` is empty or older than the
//! decision's `decided_at`, so a tick that finds it again does nothing; `undo` stamps
//! `organized_at` with the undo time for the same reason.

use std::path::Path;

use plexdb_store::StoreError;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};

use crate::extras_decisions::{self as decisions, Decision, NAMESPACE, PERSON};
use crate::extras_walk::{self, sha256_file};
use crate::pyiso::isoformat_micros;

pub const NAME: &str = "extras-organize";

/// What organizing one item would do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    /// Rename the file to `to`.
    Move { to: String },
    /// The file is already where the template puts it.
    InPlace,
    /// `to` holds a file with the same SHA-256: the duplicate at `from` is deleted.
    DeleteDuplicate { to: String },
    /// Nothing happens, and why.
    Refused(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Plan {
    pub item_id: String,
    pub from: String,
    pub action: Action,
}

impl Plan {
    pub fn line(&self) -> String {
        match &self.action {
            Action::Move { to } => format!("{}: move {} -> {to}", self.item_id, self.from),
            Action::InPlace => format!("{}: {} is already in place", self.item_id, self.from),
            Action::DeleteDuplicate { to } => format!(
                "{}: delete {}, a duplicate of {to}",
                self.item_id, self.from
            ),
            Action::Refused(why) => format!("{}: refused: {why}", self.item_id),
        }
    }
}

fn store_err(err: rusqlite::Error) -> StoreError {
    err.into()
}

/// Field values a person set that no tick has folded yet, so a plan made before the next
/// tick (`--dry-run`, or recording an organize) sees them.
#[derive(Debug, Default, Clone)]
pub struct Overlay(std::collections::HashMap<(String, String), Vec<String>>);

impl Overlay {
    /// The last `set` for each item and field in `entries` that the store's declaration accepts.
    pub fn of(conn: &Connection, entries: &[decisions::Entry]) -> Result<Self, StoreError> {
        let mut overlay = Overlay::default();
        for entry in entries {
            let Decision::Set {
                item_id,
                field,
                value,
            } = &entry.decision
            else {
                continue;
            };
            let Some(library) = decisions::library_of(conn, item_id).map_err(store_err)? else {
                continue;
            };
            let kind: Option<String> = conn
                .query_row(
                    "SELECT kind FROM extra_field WHERE library = ? AND name = ?",
                    params![library, field],
                    |row| row.get(0),
                )
                .optional()
                .map_err(store_err)?;
            if let Some(kind) = kind
                && let Ok(values) = decisions::field_values(&kind, field, value)
            {
                overlay.0.insert((item_id.clone(), field.clone()), values);
            }
        }
        Ok(overlay)
    }
}

/// A field's value for a path: the person's values when there are any, else the first
/// value any other source gave; a list joins with ", ".
fn field_text(
    conn: &Connection,
    overlay: &Overlay,
    item_id: &str,
    field: &str,
) -> Result<String, StoreError> {
    if let Some(values) = overlay.0.get(&(item_id.to_string(), field.to_string())) {
        return Ok(values.join(", "));
    }
    let mut stmt = conn
        .prepare(
            "SELECT value, source FROM enrichment WHERE item_id = ? AND namespace = ? \
             AND key = ? ORDER BY source <> ?, rank, value",
        )
        .map_err(store_err)?;
    let rows: Vec<(String, String)> = stmt
        .query_map(params![item_id, NAMESPACE, field, PERSON], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .map_err(store_err)?
        .collect::<Result<_, _>>()
        .map_err(store_err)?;
    let values: Vec<&str> = match rows.first() {
        Some((_, source)) if source == PERSON => rows
            .iter()
            .filter(|(_, s)| s == PERSON)
            .map(|(v, _)| v.as_str())
            .collect(),
        Some((value, _)) => vec![value.as_str()],
        None => Vec::new(),
    };
    Ok(values.join(", "))
}

/// One path segment from a field value: no separators, no control characters, no leading dot.
fn clean(value: &str) -> String {
    let swapped: String = value
        .chars()
        .map(|c| {
            if c == '/' || c == '\\' || c.is_control() {
                '-'
            } else {
                c
            }
        })
        .collect();
    let trimmed = swapped.trim().trim_end_matches('.').trim();
    match trimmed.strip_prefix('.') {
        Some(rest) => format!("_{rest}"),
        None => trimmed.to_string(),
    }
}

/// The template with each `{field}` replaced by `value_of(field)`; `Err` names the field
/// with no value.
fn render(
    template: &str,
    mut value_of: impl FnMut(&str) -> Result<String, StoreError>,
) -> Result<Result<String, String>, StoreError> {
    let mut out = String::new();
    let mut chars = template.chars();
    while let Some(c) = chars.next() {
        if c != '{' {
            out.push(c);
            continue;
        }
        let name: String = chars.by_ref().take_while(|&c| c != '}').collect();
        let value = clean(&value_of(&name)?);
        if value.is_empty() {
            return Ok(Err(name));
        }
        out.push_str(&value);
    }
    Ok(Ok(out))
}

/// What organizing `item_id` would do now.
pub fn plan(conn: &Connection, item_id: &str, overlay: &Overlay) -> Result<Plan, StoreError> {
    let refused = |from: &str, why: String| Plan {
        item_id: item_id.to_string(),
        from: from.to_string(),
        action: Action::Refused(why),
    };
    let library: Option<(String, Option<String>, String)> = conn
        .query_row(
            "SELECT l.name, l.template, l.root FROM extra_item i \
             JOIN extra_library l ON l.name = i.library WHERE i.item_id = ?",
            [item_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(store_err)?;
    let Some((library, template, root)) = library else {
        return Ok(refused("", "no such extra".into()));
    };
    let file: Option<(String, Option<String>)> = conn
        .query_row(
            "SELECT path, missing_since FROM extra_file WHERE item_id = ? AND primary_file = 1",
            [item_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(store_err)?;
    let Some((from, missing)) = file else {
        return Ok(refused("", "the extra has no file".into()));
    };
    if missing.is_some() || !Path::new(&from).is_file() {
        return Ok(refused(&from, "the primary file is not on disk".into()));
    }
    let required: Vec<String> = {
        let mut stmt = conn
            .prepare(
                "SELECT name FROM extra_field WHERE library = ? AND required = 1 ORDER BY name",
            )
            .map_err(store_err)?;
        stmt.query_map([&library], |row| row.get(0))
            .map_err(store_err)?
            .collect::<Result<_, _>>()
            .map_err(store_err)?
    };
    for field in &required {
        if clean(&field_text(conn, overlay, item_id, field)?).is_empty() {
            return Ok(refused(&from, format!("required field {field} is empty")));
        }
    }
    let target = match template {
        None => {
            let name = Path::new(&from)
                .file_name()
                .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
            format!("{root}/{name}")
        }
        Some(template) => {
            let rendered = render(&template, |field| field_text(conn, overlay, item_id, field))?;
            match rendered {
                Err(field) => {
                    return Ok(refused(&from, format!("field {field} is empty")));
                }
                Ok(relative) => {
                    let ext = Path::new(&from)
                        .extension()
                        .map(|e| format!(".{}", e.to_string_lossy()))
                        .unwrap_or_default();
                    format!("{root}/{relative}{ext}")
                }
            }
        }
    };
    let action = if target == from {
        Action::InPlace
    } else if !Path::new(&target).exists() || same_file(&from, &target) {
        Action::Move { to: target }
    } else if file_facts(Path::new(&from)).0 != file_facts(Path::new(&target)).0 {
        Action::Refused(format!("target taken: {target}"))
    } else {
        match (
            sha256_file(Path::new(&from)),
            sha256_file(Path::new(&target)),
        ) {
            (Ok(a), Ok(b)) if a == b => {
                let tracked: bool = conn
                    .query_row(
                        "SELECT EXISTS (SELECT 1 FROM extra_file WHERE path = ?1 AND item_id <> ?2)",
                        params![target, item_id],
                        |row| row.get(0),
                    )
                    .map_err(store_err)?;
                if tracked {
                    Action::Refused(format!("target taken: {target} belongs to another extra"))
                } else {
                    Action::DeleteDuplicate { to: target }
                }
            }
            (Ok(_), Ok(_)) => Action::Refused(format!("target taken: {target}")),
            (Err(err), _) | (_, Err(err)) => {
                Action::Refused(format!("cannot read {target} or {from}: {err}"))
            }
        }
    };
    Ok(Plan {
        item_id: item_id.to_string(),
        from,
        action,
    })
}

/// Carry `plan` out. Returns whether the item is now organized.
pub fn apply(conn: &mut Connection, plan: &Plan, now: &str) -> Result<bool, StoreError> {
    let mark = |tx: &Connection| -> Result<(), StoreError> {
        tx.execute(
            "UPDATE extra_item SET organized = 1, organized_at = ? WHERE item_id = ?",
            params![now, plan.item_id],
        )
        .map_err(store_err)?;
        Ok(())
    };
    match &plan.action {
        Action::Refused(_) => Ok(false),
        Action::InPlace => {
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(store_err)?;
            mark(&tx)?;
            tx.commit().map_err(store_err)?;
            Ok(true)
        }
        Action::DeleteDuplicate { to } => {
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(store_err)?;
            tx.execute("DELETE FROM extra_file WHERE path = ?", [&plan.from])
                .map_err(store_err)?;
            let tracked: bool = tx
                .query_row(
                    "SELECT EXISTS (SELECT 1 FROM extra_file WHERE path = ?)",
                    [to],
                    |row| row.get(0),
                )
                .map_err(store_err)?;
            if !tracked {
                let (size, mtime) = file_facts(Path::new(to));
                let sha = sha256_file(Path::new(to)).ok();
                tx.execute(
                    "INSERT INTO extra_file (path, item_id, primary_file, size, mtime, sha256, \
                     first_seen, last_seen) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    params![to, plan.item_id, size, mtime, sha, now, now],
                )
                .map_err(store_err)?;
            }
            mark(&tx)?;
            let library = decisions::library_of(&tx, &plan.item_id)
                .map_err(store_err)?
                .unwrap_or_default();
            extras_walk::choose_primaries(&tx, &library)?;
            tx.commit().map_err(store_err)?;
            // The row is gone first: a file left behind by a failed delete is found by the
            // next walk as a copy of the item, never a file the store has lost.
            std::fs::remove_file(&plan.from)
                .map_err(|err| StoreError::Store(format!("cannot delete {}: {err}", plan.from)))?;
            Ok(true)
        }
        Action::Move { to } => {
            if let Some(parent) = Path::new(to).parent() {
                std::fs::create_dir_all(parent).map_err(|err| {
                    StoreError::Store(format!("cannot create {}: {err}", parent.display()))
                })?;
            }
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(store_err)?;
            std::fs::rename(&plan.from, to).map_err(|err| {
                StoreError::Store(format!("cannot move {} to {to}: {err}", plan.from))
            })?;
            let written = (|| -> Result<(), StoreError> {
                tx.execute(
                    "INSERT INTO extra_moved (from_path, to_path, item_id, moved_at) \
                     VALUES (?, ?, ?, ?)",
                    params![plan.from, to, plan.item_id, now],
                )
                .map_err(store_err)?;
                tx.execute(
                    "UPDATE extra_file SET path = ? WHERE path = ?",
                    params![to, plan.from],
                )
                .map_err(store_err)?;
                mark(&tx)
            })();
            match written {
                Ok(()) => match tx.commit() {
                    Ok(()) => Ok(true),
                    Err(err) => {
                        let _ = std::fs::rename(to, &plan.from);
                        Err(store_err(err))
                    }
                },
                Err(err) => {
                    let _ = std::fs::rename(to, &plan.from);
                    Err(err)
                }
            }
        }
    }
}

/// Whether two paths name one file (a case-insensitive share, a symlinked root).
fn same_file(a: &str, b: &str) -> bool {
    use std::os::unix::fs::MetadataExt;
    match (std::fs::metadata(a), std::fs::metadata(b)) {
        (Ok(a), Ok(b)) => a.dev() == b.dev() && a.ino() == b.ino(),
        _ => false,
    }
}

fn file_facts(path: &Path) -> (Option<i64>, Option<i64>) {
    let Ok(meta) = std::fs::metadata(path) else {
        return (None, None);
    };
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .and_then(|d| i64::try_from(d.as_secs()).ok());
    (i64::try_from(meta.len()).ok(), mtime)
}

/// The items with an `organize` decision not yet acted on, in file order.
pub fn pending(conn: &Connection, entries: &[decisions::Entry]) -> Result<Vec<String>, StoreError> {
    let mut items = Vec::new();
    for entry in entries {
        let Decision::Organize { item_id } = &entry.decision else {
            continue;
        };
        let row: Option<Option<String>> = conn
            .query_row(
                "SELECT organized_at FROM extra_item WHERE item_id = ?",
                [item_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(store_err)?;
        let Some(organized_at) = row else { continue };
        let acted = match organized_at.as_deref().and_then(isoformat_micros) {
            None => false,
            Some(at) => isoformat_micros(&entry.decided_at).is_some_and(|decided| at >= decided),
        };
        if !acted {
            items.push(item_id.clone());
        }
    }
    Ok(items)
}

/// Act on every pending organize decision in `path`, returning one line per item.
pub fn run_pending(
    conn: &mut Connection,
    path: &Path,
    now: &str,
) -> Result<Vec<String>, crate::decisions::FoldError> {
    let Some(entries) = decisions::load(path)? else {
        return Ok(vec![format!(
            "extras organize: no file at {}",
            path.display()
        )]);
    };
    let items = pending(conn, &entries)?;
    let mut lines = vec![format!(
        "extras organize: {} decision(s) waiting",
        items.len()
    )];
    for item_id in items {
        let plan = plan(conn, &item_id, &Overlay::default())?;
        match apply(conn, &plan, now) {
            Ok(done) => lines.push(format!(
                "{}{}",
                plan.line(),
                if done { "" } else { " (left in the queue)" }
            )),
            Err(err) => lines.push(format!(
                "{}: failed: {err} (left in the queue)",
                plan.item_id
            )),
        }
    }
    Ok(lines)
}

/// Reverse the move `move_id` made: the file goes back to where it was and the extra goes
/// back to the queue. Returns the line to print.
pub fn undo(conn: &mut Connection, move_id: i64, now: &str) -> Result<String, StoreError> {
    let row: Option<(String, String, String)> = conn
        .query_row(
            "SELECT from_path, to_path, item_id FROM extra_moved WHERE move_id = ?",
            [move_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(store_err)?;
    let Some((from, to, item_id)) = row else {
        return Err(StoreError::Store(format!("no move {move_id}")));
    };
    if !Path::new(&to).is_file() {
        return Err(StoreError::Store(format!(
            "move {move_id}: {to} is not a file any more"
        )));
    }
    if Path::new(&from).exists() {
        return Err(StoreError::Store(format!(
            "move {move_id}: {from} is taken"
        )));
    }
    if let Some(parent) = Path::new(&from).parent() {
        std::fs::create_dir_all(parent).map_err(|err| {
            StoreError::Store(format!("cannot create {}: {err}", parent.display()))
        })?;
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(store_err)?;
    std::fs::rename(&to, &from)
        .map_err(|err| StoreError::Store(format!("cannot move {to} back to {from}: {err}")))?;
    let written = (|| -> rusqlite::Result<()> {
        let moved = tx.execute(
            "UPDATE extra_file SET path = ? WHERE path = ?",
            params![from, to],
        )?;
        if moved != 1 {
            return Err(rusqlite::Error::StatementChangedRows(moved));
        }
        tx.execute(
            "UPDATE extra_item SET organized = 0, organized_at = ? WHERE item_id = ?",
            params![now, item_id],
        )?;
        tx.execute("DELETE FROM extra_moved WHERE move_id = ?", [move_id])?;
        Ok(())
    })();
    match written {
        Ok(()) => tx.commit().map_err(store_err)?,
        Err(err) => {
            let _ = std::fs::rename(&from, &to);
            return Err(store_err(err));
        }
    }
    Ok(format!("undid move {move_id}: {to} -> {from}"))
}
