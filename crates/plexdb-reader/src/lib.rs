//! Read-only, typed access to a `plexdb.db` store ([ADR-0003][adr-0003]).
//!
//! One process — the `plexdb` Python package in this repository — writes
//! the store; everything else, including this crate, reads it.
//! [`Reader::open`] enforces that at two levels: the connection itself is
//! opened with SQLite's own read-only flag (not a convention this crate
//! could forget to honour), and the store's schema version is checked
//! against what this crate's accessors were written for, so a mismatch
//! fails loudly at open time rather than as a missing-column error deep
//! inside a query.
//!
//! Typed accessors, not a generic query function: a schema change that
//! drops a column a consumer reads fails that consumer's **build**, not a
//! runtime string-built query.
//!
//! **No ranking policy lives here.** [`Reader::taste_vector_for`] computes
//! the Layer 2 rollup — how many distinct watched titles carry each
//! enrichment attribute — and nothing more. Recency half-life, exploration
//! fraction, and negative-signal weighting are the consumer's decisions,
//! tracked separately and not yet answered (plex-db-ex#13).
//!
//! **Weighted collection membership is not implemented.** The issue that
//! commissioned this crate (plex-db-ex#11) asks for it, but the
//! `collection_membership` table it would read does not exist yet — the
//! store's own schema (`plexdb/schema.py`, currently at version
//! [`SUPPORTED_SCHEMA_VERSION`]) has no such table, `docs/schema.md` lists
//! it under "Not yet built", and the project roadmap places it under
//! "Later", after this milestone. Adding it here would mean inventing a
//! table the writer does not create, which no real store would ever
//! satisfy. This accessor is deferred until that table ships.
//!
//! [adr-0003]: https://github.com/McBrideMusings/plex-db-ex/blob/main/docs/adr/0003-rust-reader-crate-behind-a-plugin-capability-grant.md

mod error;
mod model;
mod schema;

use std::path::Path;

use rusqlite::{Connection, OpenFlags, Row};

pub use error::ReaderError;
pub use model::{Edge, EnrichmentFact, TasteAttribute};
pub use schema::SUPPORTED_SCHEMA_VERSION;

/// The exact flags every connection in this crate is opened with: read-only,
/// no implicit create, no read-write fallback. A write attempted through a
/// connection opened this way fails at SQLite's own gate, not by this
/// crate's convention — see the unit test below, which opens a connection
/// with this exact constant and proves a write against it fails.
const OPEN_FLAGS: OpenFlags = OpenFlags::SQLITE_OPEN_READ_ONLY;

/// A read-only handle on a `plexdb.db` store.
#[derive(Debug)]
pub struct Reader {
    conn: Connection,
}

impl Reader {
    /// Open the store at `path` read-only.
    ///
    /// Fails loudly — never lazily, never as a missing-column panic later —
    /// if the file is absent, is not a plexdb store, or is a schema version
    /// this build does not understand.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, ReaderError> {
        let path = path.as_ref();
        let conn =
            Connection::open_with_flags(path, OPEN_FLAGS).map_err(|source| ReaderError::Open {
                path: path.to_path_buf(),
                source,
            })?;
        schema::check(&conn, path)?;
        Ok(Self { conn })
    }

    /// Every enrichment fact recorded for `item_id` under `namespace`, in
    /// `(key, value)` order. Empty, not an error, when nothing is recorded.
    pub fn enrichment_for(
        &self,
        item_id: &str,
        namespace: &str,
    ) -> Result<Vec<EnrichmentFact>, ReaderError> {
        let mut stmt = self.conn.prepare(
            "SELECT namespace, key, value, fetched_at \
             FROM enrichment \
             WHERE item_id = ?1 AND namespace = ?2 \
             ORDER BY key, value",
        )?;
        let rows = stmt
            .query_map((item_id, namespace), Self::enrichment_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// Edges of `edge_type` pointing *out of* `item_id`, ranked ascending.
    pub fn edges_from(&self, item_id: &str, edge_type: &str) -> Result<Vec<Edge>, ReaderError> {
        self.query_edges(
            "SELECT from_id, to_id, edge_type, rank, fetched_at \
             FROM edges \
             WHERE from_id = ?1 AND edge_type = ?2 \
             ORDER BY rank",
            item_id,
            edge_type,
        )
    }

    /// Edges of `edge_type` pointing *into* `item_id`, ranked ascending.
    pub fn edges_to(&self, item_id: &str, edge_type: &str) -> Result<Vec<Edge>, ReaderError> {
        self.query_edges(
            "SELECT from_id, to_id, edge_type, rank, fetched_at \
             FROM edges \
             WHERE to_id = ?1 AND edge_type = ?2 \
             ORDER BY rank",
            item_id,
            edge_type,
        )
    }

    /// Shared by [`Self::edges_from`] and [`Self::edges_to`], which differ
    /// only in which column they filter — `sql` carries that difference,
    /// this carries the prepare/bind/collect boilerplate.
    fn query_edges(
        &self,
        sql: &str,
        item_id: &str,
        edge_type: &str,
    ) -> Result<Vec<Edge>, ReaderError> {
        let mut stmt = self.conn.prepare(sql)?;
        let rows = stmt
            .query_map((item_id, edge_type), Self::edge_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// The Layer 2 rollup for one Plex account: every enrichment attribute
    /// carried by a title that account has played, weighted by how many
    /// *distinct* such titles carry it — a rewatch of one title does not
    /// inflate its attributes' weight. See [`TasteAttribute`] for what is
    /// deliberately left out.
    ///
    /// Deterministic: calling this twice against unchanged data returns an
    /// identical vector, in the same order. Empty, not an error, for an
    /// account with no plays.
    pub fn taste_vector_for(
        &self,
        plex_account_id: i64,
    ) -> Result<Vec<TasteAttribute>, ReaderError> {
        let mut stmt = self.conn.prepare(
            "SELECT e.namespace, e.key, e.value, COUNT(DISTINCT p.item_id) AS weight \
             FROM plays p \
             JOIN enrichment e ON e.item_id = p.item_id \
             WHERE p.plex_account_id = ?1 \
             GROUP BY e.namespace, e.key, e.value \
             ORDER BY e.namespace, e.key, e.value",
        )?;
        let rows = stmt
            .query_map([plex_account_id], Self::taste_attribute_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    fn taste_attribute_row(row: &Row) -> rusqlite::Result<TasteAttribute> {
        Ok(TasteAttribute {
            namespace: row.get(0)?,
            key: row.get(1)?,
            value: row.get(2)?,
            weight: row.get(3)?,
        })
    }

    fn enrichment_row(row: &Row) -> rusqlite::Result<EnrichmentFact> {
        Ok(EnrichmentFact {
            namespace: row.get(0)?,
            key: row.get(1)?,
            value: row.get(2)?,
            fetched_at: row.get(3)?,
        })
    }

    fn edge_row(row: &Row) -> rusqlite::Result<Edge> {
        Ok(Edge {
            from_id: row.get(0)?,
            to_id: row.get(1)?,
            edge_type: row.get(2)?,
            rank: row.get(3)?,
            fetched_at: row.get(4)?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The acceptance test ADR-0003 and issue #11 ask for: a write attempted
    /// through a connection opened the way every `Reader` connection is
    /// opened fails at SQLite's own gate. This opens a plain file with the
    /// crate's real `OPEN_FLAGS` constant — not a hand-copied approximation
    /// of it — so drift between this test and `Reader::open` is impossible.
    #[test]
    fn the_flags_every_reader_connection_uses_reject_a_write() {
        let file = tempfile::NamedTempFile::new().expect("create a temp file");
        {
            // Set up with an ordinary read-write connection; OPEN_FLAGS
            // alone cannot create a file, by design.
            let setup = Connection::open(file.path()).expect("open for setup");
            setup
                .execute_batch("CREATE TABLE t (id INTEGER PRIMARY KEY);")
                .expect("create a table to attempt writing into");
        }

        let conn = Connection::open_with_flags(file.path(), OPEN_FLAGS).expect("open read-only");
        let result = conn.execute("INSERT INTO t (id) VALUES (1)", []);

        assert!(
            result.is_err(),
            "a connection opened with OPEN_FLAGS must not accept a write"
        );
    }
}
