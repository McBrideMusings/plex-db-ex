//! The store's rows as resources: titles and what is filed under them, plays, accounts,
//! keywords and the judge's tables, each read whole for one title or one page at a time
//! for a list (`docs/api.md`).
//!
//! A page is keyset-paginated: the rows after the cursor, in the order of a unique key
//! that never changes, `limit + 1` of them fetched so that `has_more` is known without a
//! count.

use std::collections::BTreeMap;

use rusqlite::{OptionalExtension, Row, params_from_iter};

use crate::call::{Direction, Include, ItemFilter, PageArgs};
use crate::model::{
    Account, Item, Keyword, KeywordPair, KeywordRoles, MediaPart, Page, Play, PlexEntry,
    RoleDecision, RoleStatement, RoleVerdict,
};
use crate::{Edge, ExternalId, Reader, ReaderError};

/// Ids per `IN (...)` list: under every SQLite build's variable limit.
const IDS_PER_QUERY: usize = 500;

/// How many rows a page query asks for: one past the page, to learn whether more follow.
pub(crate) fn fetch(limit: usize) -> i64 {
    i64::try_from(limit).unwrap_or(i64::MAX).saturating_add(1)
}

/// `rows` cut to `limit`, with `next` the key of the last row kept when more follow.
pub(crate) fn page<T>(mut rows: Vec<T>, limit: usize, key: impl Fn(&T) -> String) -> Page<T> {
    let has_more = rows.len() > limit;
    rows.truncate(limit);
    let next = if has_more { rows.last().map(key) } else { None };
    Page {
        data: rows,
        has_more,
        next,
    }
}

const ITEM_COLUMNS: &str = "i.item_id, i.type, i.title, i.title_sort, i.show_title, \
     i.show_item_id, i.season, i.episode, i.year, i.duration_ms, i.content_rating, i.studio";

const PLAY_COLUMNS: &str = "history_key, item_id, plex_account_id, platform, viewed_at, \
     percent_complete, paused_counter, seconds_watched";

const PAIR_COLUMNS: &str =
    "keyword_a, keyword_b, jev_score, jev_model, judged_at, jev_error, decision, decided_at";

fn item_row(row: &Row) -> rusqlite::Result<Item> {
    Ok(Item {
        item_id: row.get(0)?,
        kind: row.get(1)?,
        title: row.get(2)?,
        title_sort: row.get(3)?,
        show_title: row.get(4)?,
        show_item_id: row.get(5)?,
        season: row.get(6)?,
        episode: row.get(7)?,
        year: row.get(8)?,
        duration_ms: row.get(9)?,
        content_rating: row.get(10)?,
        studio: row.get(11)?,
        enrichment: None,
        external_ids: None,
        audio_facts: None,
        plex: None,
    })
}

fn play_row(row: &Row) -> rusqlite::Result<Play> {
    Ok(Play {
        history_key: row.get(0)?,
        item_id: row.get(1)?,
        plex_account_id: row.get(2)?,
        platform: row.get(3)?,
        viewed_at: row.get(4)?,
        percent_complete: row.get(5)?,
        paused_counter: row.get(6)?,
        seconds_watched: row.get(7)?,
    })
}

fn pair_row(row: &Row) -> rusqlite::Result<KeywordPair> {
    Ok(KeywordPair {
        keyword_a: row.get(0)?,
        keyword_b: row.get(1)?,
        jev_score: row.get(2)?,
        jev_model: row.get(3)?,
        judged_at: row.get(4)?,
        jev_error: row.get(5)?,
        decision: row.get(6)?,
        decided_at: row.get(7)?,
    })
}

/// A page of keyword pairs continues after this pair: `next` is the JSON array
/// `[keyword_a, keyword_b]` as text, since no one keyword is unique across pairs.
pub(crate) fn pair_cursor(pair: &KeywordPair) -> String {
    serde_json::json!([pair.keyword_a, pair.keyword_b]).to_string()
}

impl Reader {
    /// Rows of `sql` for each of `ids`, grouped by the id in column 0; `map` reads the
    /// rest of the row. `sql` holds one `{ids}` where the `IN` list goes.
    fn by_ids<T>(
        &self,
        sql: &str,
        ids: &[&str],
        map: impl Fn(&Row) -> rusqlite::Result<T>,
    ) -> Result<BTreeMap<String, Vec<T>>, ReaderError> {
        let mut by_id: BTreeMap<String, Vec<T>> = BTreeMap::new();
        for chunk in ids.chunks(IDS_PER_QUERY) {
            let sql = sql.replace("{ids}", &vec!["?"; chunk.len()].join(","));
            let mut stmt = self.conn.prepare(&sql)?;
            let rows = stmt
                .query_map(params_from_iter(chunk.iter()), |row| {
                    Ok((row.get::<_, String>(0)?, map(row)?))
                })?
                .collect::<Result<Vec<_>, _>>()?;
            for (id, value) in rows {
                by_id.entry(id).or_default().push(value);
            }
        }
        Ok(by_id)
    }

    /// Whether `items` holds `item_id`.
    pub fn item_exists(&self, item_id: &str) -> Result<bool, ReaderError> {
        Ok(self
            .conn
            .query_row("SELECT 1 FROM items WHERE item_id = ?1", [item_id], |_| {
                Ok(())
            })
            .optional()?
            .is_some())
    }

    /// The title `item_id` with what `include` names, or `None` when the store has none.
    pub fn item(&self, item_id: &str, include: &Include) -> Result<Option<Item>, ReaderError> {
        let item = self
            .conn
            .query_row(
                &format!("SELECT {ITEM_COLUMNS} FROM items i WHERE i.item_id = ?1"),
                [item_id],
                item_row,
            )
            .optional()?;
        let Some(item) = item else {
            return Ok(None);
        };
        let mut items = [item];
        self.include(&mut items, include)?;
        let [item] = items;
        Ok(Some(item))
    }

    /// One page of titles matching `filter`, by `item_id`, each with what `include` names.
    pub fn items(
        &self,
        filter: &ItemFilter,
        include: &Include,
        page_args: &PageArgs,
    ) -> Result<Page<Item>, ReaderError> {
        let mut stmt = self.conn.prepare(&format!(
            "SELECT {ITEM_COLUMNS} FROM items i \
             WHERE i.item_id > ?1 \
               AND (?2 IS NULL OR i.type = ?2) \
               AND (?3 IS NULL OR i.show_item_id = ?3) \
               AND (?4 IS NULL OR EXISTS ( \
                     SELECT 1 FROM plex_items p WHERE p.item_id = i.item_id AND p.section_id = ?4)) \
             ORDER BY i.item_id LIMIT ?5"
        ))?;
        let rows = stmt
            .query_map(
                rusqlite::params![
                    page_args.after.as_deref().unwrap_or(""),
                    filter.kind,
                    filter.show,
                    filter.library,
                    fetch(page_args.limit),
                ],
                item_row,
            )?
            .collect::<Result<Vec<_>, _>>()?;
        let mut page = page(rows, page_args.limit, |item| item.item_id.clone());
        self.include(&mut page.data, include)?;
        Ok(page)
    }

    /// Fill each of `items`' included fields, one query per field for all of them.
    fn include(&self, items: &mut [Item], include: &Include) -> Result<(), ReaderError> {
        let ids: Vec<String> = items.iter().map(|i| i.item_id.clone()).collect();
        let ids: Vec<&str> = ids.iter().map(String::as_str).collect();
        if include.enrichment {
            let mut found =
                self.enrichment_for_many(ids.iter().copied(), include.namespace.as_deref())?;
            for item in items.iter_mut() {
                item.enrichment = Some(found.remove(&item.item_id).unwrap_or_default());
            }
        }
        if include.external_ids {
            let mut found = self.external_ids_by(&ids)?;
            for item in items.iter_mut() {
                item.external_ids = Some(found.remove(&item.item_id).unwrap_or_default());
            }
        }
        if include.audio_facts {
            let mut found = self.audio_facts_for_many(ids.iter().copied())?;
            for item in items.iter_mut() {
                item.audio_facts = Some(
                    found
                        .remove(&item.item_id)
                        .unwrap_or(serde_json::Value::Null),
                );
            }
        }
        if include.plex {
            let mut found = self.plex_by(&ids)?;
            for item in items.iter_mut() {
                item.plex = Some(found.remove(&item.item_id).unwrap_or_default());
            }
        }
        Ok(())
    }

    fn external_ids_by(
        &self,
        ids: &[&str],
    ) -> Result<BTreeMap<String, Vec<ExternalId>>, ReaderError> {
        self.by_ids(
            "SELECT item_id, ns, value, kind, last_seen FROM external_ids \
             WHERE item_id IN ({ids}) ORDER BY item_id, ns, kind, value",
            ids,
            |row| {
                Ok(ExternalId {
                    ns: row.get(1)?,
                    value: row.get(2)?,
                    kind: row.get(3)?,
                    last_seen: row.get(4)?,
                })
            },
        )
    }

    fn plex_by(&self, ids: &[&str]) -> Result<BTreeMap<String, Vec<PlexEntry>>, ReaderError> {
        self.by_ids(
            "SELECT item_id, rating_key, section_id, last_seen FROM plex_items \
             WHERE item_id IN ({ids}) ORDER BY item_id, rating_key",
            ids,
            |row| {
                Ok(PlexEntry {
                    rating_key: row.get(1)?,
                    section_id: row.get(2)?,
                    last_seen: row.get(3)?,
                })
            },
        )
    }

    /// Every id a source gives `item_id`, by `(ns, kind, value)`.
    pub fn external_ids_for(&self, item_id: &str) -> Result<Vec<ExternalId>, ReaderError> {
        Ok(self
            .external_ids_by(&[item_id])?
            .remove(item_id)
            .unwrap_or_default())
    }

    /// Every Plex library entry that resolves to `item_id`, by rating key.
    pub fn plex_for(&self, item_id: &str) -> Result<Vec<PlexEntry>, ReaderError> {
        Ok(self
            .plex_by(&[item_id])?
            .remove(item_id)
            .unwrap_or_default())
    }

    /// Every file Plex lists for `item_id`, by `part_id`.
    pub fn media_parts_for(&self, item_id: &str) -> Result<Vec<MediaPart>, ReaderError> {
        let mut stmt = self.conn.prepare(
            "SELECT part_id, item_id, rating_key, file, size, container, video_codec, \
                    audio_codec, height, bitrate, duration_ms, plex_updated_at, fetched_at \
             FROM media_part WHERE item_id = ?1 ORDER BY part_id",
        )?;
        let rows = stmt
            .query_map([item_id], |row| {
                Ok(MediaPart {
                    part_id: row.get(0)?,
                    item_id: row.get(1)?,
                    rating_key: row.get(2)?,
                    file: row.get(3)?,
                    size: row.get(4)?,
                    container: row.get(5)?,
                    video_codec: row.get(6)?,
                    audio_codec: row.get(7)?,
                    height: row.get(8)?,
                    bitrate: row.get(9)?,
                    duration_ms: row.get(10)?,
                    plex_updated_at: row.get(11)?,
                    fetched_at: row.get(12)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// Every play of `item_id`, oldest first.
    pub fn plays_for(&self, item_id: &str) -> Result<Vec<Play>, ReaderError> {
        let mut stmt = self.conn.prepare(&format!(
            "SELECT {PLAY_COLUMNS} FROM plays WHERE item_id = ?1 \
             ORDER BY viewed_at, history_key"
        ))?;
        let rows = stmt
            .query_map([item_id], play_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// One page of plays by `history_key`, one account's when `account` is given, and only
    /// those at or after `since` (Unix seconds) when it is.
    pub fn plays(
        &self,
        account: Option<i64>,
        since: Option<i64>,
        page_args: &PageArgs,
    ) -> Result<Page<Play>, ReaderError> {
        let mut stmt = self.conn.prepare(&format!(
            "SELECT {PLAY_COLUMNS} FROM plays \
             WHERE history_key > ?1 \
               AND (?2 IS NULL OR plex_account_id = ?2) \
               AND (?3 IS NULL OR viewed_at >= ?3) \
             ORDER BY history_key LIMIT ?4"
        ))?;
        let rows = stmt
            .query_map(
                rusqlite::params![
                    page_args.after.as_deref().unwrap_or(""),
                    account,
                    since,
                    fetch(page_args.limit),
                ],
                play_row,
            )?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(page(rows, page_args.limit, |play| play.history_key.clone()))
    }

    /// Every account the store has plays for, by id.
    pub fn accounts(&self) -> Result<Vec<Account>, ReaderError> {
        let mut stmt = self.conn.prepare(
            "SELECT plex_account_id, COUNT(*), MAX(viewed_at) FROM plays \
             GROUP BY plex_account_id ORDER BY plex_account_id",
        )?;
        let rows = stmt
            .query_map([], |row| {
                Ok(Account {
                    account_id: row.get(0)?,
                    plays: row.get(1)?,
                    last_viewed_at: row.get(2)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// Whether the store has a play by `account`: an account is known only through its plays.
    pub fn account_exists(&self, account: i64) -> Result<bool, ReaderError> {
        Ok(self
            .conn
            .query_row(
                "SELECT 1 FROM plays WHERE plex_account_id = ?1 LIMIT 1",
                [account],
                |_| Ok(()),
            )
            .optional()?
            .is_some())
    }

    /// Every title-level role statement about `item_id`, by `(keyword, role, source)`.
    pub fn role_statements_for(&self, item_id: &str) -> Result<Vec<RoleStatement>, ReaderError> {
        let mut stmt = self.conn.prepare(
            "SELECT keyword, role, source, stated_at FROM keyword_role_statements \
             WHERE item_id = ?1 ORDER BY keyword, role, source",
        )?;
        let rows = stmt
            .query_map([item_id], |row| {
                Ok(RoleStatement {
                    keyword: row.get(0)?,
                    role: row.get(1)?,
                    source: row.get(2)?,
                    stated_at: row.get(3)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// Edges of `edge_type` (every type when `None`) out of or into `item_id`.
    pub fn edges(
        &self,
        item_id: &str,
        edge_type: Option<&str>,
        direction: Direction,
    ) -> Result<Vec<Edge>, ReaderError> {
        match direction {
            Direction::Out => self.edges_from(item_id, edge_type),
            Direction::In => self.edges_to(item_id, edge_type),
        }
    }

    /// The stored `keyword` with its spellings and display name, or `None` when the store
    /// records neither.
    pub fn keyword(&self, keyword: &str) -> Result<Option<Keyword>, ReaderError> {
        let surfaces = self.surfaces_for_keyword(keyword)?;
        let name = self.display_name(keyword)?;
        if surfaces.is_empty() && name.is_none() {
            return Ok(None);
        }
        Ok(Some(Keyword {
            keyword: keyword.to_string(),
            name,
            surfaces,
        }))
    }

    /// One page of stored keywords — every keyword a spelling maps to or a person named —
    /// sorted. With `surface`, only the keyword that spelling maps to.
    pub fn keywords(
        &self,
        surface: Option<&str>,
        page_args: &PageArgs,
    ) -> Result<Page<Keyword>, ReaderError> {
        let after = page_args.after.as_deref().unwrap_or("");
        if let Some(surface) = surface {
            let found = match self.keyword_for_surface(surface)? {
                Some(keyword) if keyword.as_str() > after => self.keyword(&keyword)?,
                _ => None,
            };
            return Ok(page(found.into_iter().collect(), page_args.limit, |k| {
                k.keyword.clone()
            }));
        }
        let mut stmt = self.conn.prepare(
            "SELECT keyword FROM ( \
               SELECT keyword FROM keyword_forms UNION SELECT keyword FROM keyword_names \
             ) WHERE keyword > ?1 ORDER BY keyword LIMIT ?2",
        )?;
        let keywords = stmt
            .query_map(rusqlite::params![after, fetch(page_args.limit)], |row| {
                row.get::<_, String>(0)
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let keywords = page(keywords, page_args.limit, Clone::clone);
        let (Some(first), Some(last)) = (keywords.data.first(), keywords.data.last()) else {
            return Ok(Page {
                data: Vec::new(),
                has_more: false,
                next: None,
            });
        };
        // The page's spellings and names in two range reads, not two queries per keyword.
        let mut surfaces: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut stmt = self.conn.prepare(
            "SELECT keyword, surface FROM keyword_forms \
             WHERE keyword >= ?1 AND keyword <= ?2 ORDER BY keyword, surface",
        )?;
        for row in stmt.query_map((first, last), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (keyword, surface) = row?;
            surfaces.entry(keyword).or_default().push(surface);
        }
        let mut names: BTreeMap<String, String> = BTreeMap::new();
        let mut stmt = self.conn.prepare(
            "SELECT keyword, name FROM keyword_names WHERE keyword >= ?1 AND keyword <= ?2",
        )?;
        for row in stmt.query_map((first, last), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })? {
            let (keyword, name) = row?;
            names.insert(keyword, name);
        }
        Ok(Page {
            data: keywords
                .data
                .into_iter()
                .map(|keyword| Keyword {
                    name: names.remove(&keyword),
                    surfaces: surfaces.remove(&keyword).unwrap_or_default(),
                    keyword,
                })
                .collect(),
            has_more: keywords.has_more,
            next: keywords.next,
        })
    }

    /// Every judged or decided pair `keyword` is in, by `(keyword_a, keyword_b)`.
    pub fn pairs_for(&self, keyword: &str) -> Result<Vec<KeywordPair>, ReaderError> {
        let mut stmt = self.conn.prepare(&format!(
            "SELECT {PAIR_COLUMNS} FROM keyword_pairs \
             WHERE keyword_a = ?1 OR keyword_b = ?1 ORDER BY keyword_a, keyword_b"
        ))?;
        let rows = stmt
            .query_map([keyword], pair_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    /// One page of keyword pairs by `(keyword_a, keyword_b)`, only those with `decision`
    /// (`accepted`, `rejected`, or `undecided` for none) when it is given.
    pub fn pairs(
        &self,
        decision: Option<&str>,
        after: Option<(&str, &str)>,
        limit: usize,
    ) -> Result<Page<KeywordPair>, ReaderError> {
        let (a, b) = after.unwrap_or(("", ""));
        let mut stmt = self.conn.prepare(&format!(
            "SELECT {PAIR_COLUMNS} FROM keyword_pairs \
             WHERE (keyword_a, keyword_b) > (?1, ?2) \
               AND (?3 IS NULL OR decision = ?3 OR (?3 = 'undecided' AND decision IS NULL)) \
             ORDER BY keyword_a, keyword_b LIMIT ?4"
        ))?;
        let rows = stmt
            .query_map(rusqlite::params![a, b, decision, fetch(limit)], pair_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(page(rows, limit, pair_cursor))
    }

    /// A source's score and a person's decision for each role `keyword` plays, by role.
    pub fn roles_for(&self, keyword: &str) -> Result<KeywordRoles, ReaderError> {
        let mut stmt = self.conn.prepare(
            "SELECT role, source, score, model, error, stated_at FROM keyword_roles \
             WHERE keyword = ?1 ORDER BY role, source",
        )?;
        let verdicts = stmt
            .query_map([keyword], |row| {
                Ok(RoleVerdict {
                    role: row.get(0)?,
                    source: row.get(1)?,
                    score: row.get(2)?,
                    model: row.get(3)?,
                    error: row.get(4)?,
                    stated_at: row.get(5)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let mut stmt = self.conn.prepare(
            "SELECT role, decision, decided_at FROM keyword_role_decisions \
             WHERE keyword = ?1 ORDER BY role",
        )?;
        let decisions = stmt
            .query_map([keyword], |row| {
                Ok(RoleDecision {
                    role: row.get(0)?,
                    decision: row.get(1)?,
                    decided_at: row.get(2)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(KeywordRoles {
            keyword: keyword.to_string(),
            verdicts,
            decisions,
        })
    }

    /// Whether the stored tag network of `kind` holds `tag`.
    pub fn tag_exists(&self, kind: &str, tag: &str) -> Result<bool, ReaderError> {
        Ok(self
            .conn
            .query_row(
                "SELECT 1 FROM tag_network WHERE kind = ?1 AND value = ?2",
                [kind, tag],
                |_| Ok(()),
            )
            .optional()?
            .is_some())
    }
}
