//! `plexdb latent-users [--account ID]...` — cluster a shared Plex account's plays into
//! latent users by device fingerprint and report taste divergence between the
//! clusters (`plexdb/commands/latent_users.py`, `plexdb/clusters.py`).
//!
//! Read-only: it opens the store read-only and writes nothing. It reaches Plex's
//! `/accounts` to name each account in the report. The report text matches the Python
//! command byte for byte, and `admin parity latent-users --fake plex` checks it.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::time::Duration;

use plexdb_store::{Config, StoreError, open_readonly, py_strip};
use rusqlite::Connection;
use serde_json::Value;

use crate::decisions::py_repr;
use crate::pyval::py_int_text;
use crate::steps::Failure;

pub const NAME: &str = "latent-users";

/// An IP must recur on at least this many plays before it is trusted as signal.
const MIN_IP_OCCURRENCES: usize = 2;
/// An IP may join devices only if it appears under no more accounts than this.
const MAX_IP_ACCOUNTS_TO_JOIN: i64 = 1;
/// A cluster with fewer plays than this is folded into the `unattributed` bucket.
const LATENT_USER_FLOOR: usize = 20;
/// How many keywords a cluster's profile keeps.
const TOP_N_KEYWORDS: usize = 20;
const KEYWORDS_NAMESPACE: &str = "keywords";
const KEYWORD_KEY: &str = "keyword";
/// `DEFAULT_SHARED_ACCOUNT_IDS` in `plexdb/config.py`.
const DEFAULT_SHARED_ACCOUNT_IDS: &str = "1,3670670";
/// Variables bound per `IN (...)` query, well under SQLite's limit.
const IN_CHUNK: usize = 500;
const PLEX_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, PartialEq, Eq)]
struct Cluster {
    key_tier: &'static str,
    key_value: Option<String>,
    client_identifiers: Vec<String>,
    ips: Vec<String>,
    platforms: Vec<String>,
    item_ids: Vec<String>,
    units: Vec<(String, i64)>,
    play_count: usize,
    joined_by_ips: Vec<String>,
}

struct DiscountedJoinIp {
    ip: String,
    reason: String,
}

struct AccountClusters {
    play_count: usize,
    distinct_client_identifiers: usize,
    clusters: Vec<Cluster>,
    transient_ips: Vec<String>,
    discounted_join_ips: Vec<DiscountedJoinIp>,
}

struct KeywordProfile {
    units_total: usize,
    units_with_coverage: usize,
    total_depth: i64,
    covered_depth: i64,
    top_keywords: Vec<(String, i64)>,
}

impl KeywordProfile {
    fn has_coverage(&self) -> bool {
        self.units_with_coverage > 0
    }
}

struct PlayRow {
    item_id: String,
    client_identifier: Option<String>,
    ip: Option<String>,
    platform: Option<String>,
    unit_id: String,
}

/// Python truthiness of an optional string: present and not empty.
fn some_text(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|text| !text.is_empty())
}

/// Every `plex_account_id` with a play, ascending.
fn account_ids_with_plays(conn: &Connection) -> rusqlite::Result<Vec<i64>> {
    let mut stmt =
        conn.prepare("SELECT DISTINCT plex_account_id FROM plays ORDER BY plex_account_id")?;
    stmt.query_map([], |row| row.get(0))?.collect()
}

fn account_play_rows(conn: &Connection, plex_account_id: i64) -> rusqlite::Result<Vec<PlayRow>> {
    let mut stmt = conn.prepare(
        "SELECT p.item_id, p.client_identifier, p.ip, p.platform, \
         COALESCE(i.show_item_id, p.item_id) AS unit_id \
         FROM plays p JOIN items i ON i.item_id = p.item_id \
         WHERE p.plex_account_id = ?1",
    )?;
    stmt.query_map([plex_account_id], |row| {
        Ok(PlayRow {
            item_id: row.get(0)?,
            client_identifier: row.get(1)?,
            ip: row.get(2)?,
            platform: row.get(3)?,
            unit_id: row.get(4)?,
        })
    })?
    .collect()
}

/// How many distinct accounts each IP appears under, across the whole store.
fn ip_account_counts(conn: &Connection) -> rusqlite::Result<HashMap<String, i64>> {
    let mut stmt = conn.prepare(
        "SELECT ip, COUNT(DISTINCT plex_account_id) AS n FROM plays \
         WHERE ip IS NOT NULL GROUP BY ip",
    )?;
    stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?
    .collect()
}

fn find(parent: &mut HashMap<String, String>, x: &str) -> String {
    parent.entry(x.to_string()).or_insert_with(|| x.to_string());
    let mut root = x.to_string();
    while parent[&root] != root {
        root = parent[&root].clone();
    }
    root
}

fn union(parent: &mut HashMap<String, String>, a: &str, b: &str) {
    let (ra, rb) = (find(parent, a), find(parent, b));
    if ra != rb {
        parent.insert(ra, rb);
    }
}

type Joins = (
    HashMap<String, String>,
    HashMap<String, Vec<String>>,
    Vec<DiscountedJoinIp>,
);

/// Which `client_identifier`s merge, on which IPs, and which candidate IPs were
/// discounted. The canonical id of a group is its smallest member.
fn join_devices_on_recurring_ips(rows: &[PlayRow], ip_accounts: &HashMap<String, i64>) -> Joins {
    let distinct: BTreeSet<&str> = rows
        .iter()
        .filter_map(|r| some_text(&r.client_identifier))
        .collect();

    let mut occurrences: HashMap<&str, usize> = HashMap::new();
    let mut clients_by_ip: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    for r in rows {
        if let (Some(client), Some(ip)) = (r.client_identifier.as_deref(), r.ip.as_deref()) {
            *occurrences.entry(ip).or_default() += 1;
            clients_by_ip.entry(ip).or_default().insert(client);
        }
    }

    let mut parent: HashMap<String, String> = HashMap::new();
    let mut joining_ips: HashMap<&str, BTreeSet<&str>> = HashMap::new();
    let mut discounted = Vec::new();
    for (ip, clients) in &clients_by_ip {
        if clients.len() < 2 {
            continue;
        }
        if occurrences[ip] < MIN_IP_OCCURRENCES {
            discounted.push(DiscountedJoinIp {
                ip: ip.to_string(),
                reason: format!("seen on only {} play(s) across devices", occurrences[ip]),
            });
            continue;
        }
        let accounts = ip_accounts.get(*ip).copied().unwrap_or(0);
        if accounts > MAX_IP_ACCOUNTS_TO_JOIN {
            discounted.push(DiscountedJoinIp {
                ip: ip.to_string(),
                reason: format!(
                    "seen under {accounts} different accounts, so treated as shared \
                     infrastructure rather than a household"
                ),
            });
            continue;
        }
        let ordered: Vec<&str> = clients.iter().copied().collect();
        for other in &ordered[1..] {
            union(&mut parent, ordered[0], other);
        }
        for client in ordered {
            joining_ips.entry(client).or_default().insert(ip);
        }
    }

    let mut groups: HashMap<String, BTreeSet<&str>> = HashMap::new();
    for client in &distinct {
        groups
            .entry(find(&mut parent, client))
            .or_default()
            .insert(client);
    }
    let mut canonical: HashMap<String, String> = HashMap::new();
    for members in groups.values() {
        let rep = members.iter().next().expect("a group has a member");
        for member in members {
            canonical.insert(member.to_string(), rep.to_string());
        }
    }

    let mut joined_sets: HashMap<String, BTreeSet<&str>> = HashMap::new();
    for (client, ips) in &joining_ips {
        let rep = canonical
            .get(*client)
            .cloned()
            .unwrap_or_else(|| client.to_string());
        joined_sets.entry(rep).or_default().extend(ips);
    }
    let joined_by = joined_sets
        .into_iter()
        .map(|(rep, ips)| (rep, ips.into_iter().map(String::from).collect()))
        .collect();

    (canonical, joined_by, discounted)
}

fn sorted_set<'a>(values: impl Iterator<Item = &'a str>) -> Vec<String> {
    values
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(String::from)
        .collect()
}

fn cluster_account_plays(
    conn: &Connection,
    plex_account_id: i64,
) -> rusqlite::Result<AccountClusters> {
    let rows = account_play_rows(conn, plex_account_id)?;
    let distinct: BTreeSet<&str> = rows
        .iter()
        .filter_map(|r| some_text(&r.client_identifier))
        .collect();

    let mut ip_occurrences: HashMap<&str, usize> = HashMap::new();
    for r in &rows {
        if r.client_identifier.is_none()
            && let Some(ip) = r.ip.as_deref()
        {
            *ip_occurrences.entry(ip).or_default() += 1;
        }
    }
    let eligible: BTreeSet<&str> = ip_occurrences
        .iter()
        .filter(|(_, count)| **count >= MIN_IP_OCCURRENCES)
        .map(|(ip, _)| *ip)
        .collect();
    let transient_ips = sorted_set(
        ip_occurrences
            .keys()
            .filter(|ip| !eligible.contains(**ip))
            .copied(),
    );

    let (canonical, joined_by, mut discounted) =
        join_devices_on_recurring_ips(&rows, &ip_account_counts(conn)?);
    discounted.sort_by(|a, b| a.ip.cmp(&b.ip));

    type Key = (&'static str, Option<String>);
    let mut order: Vec<Key> = Vec::new();
    let mut buckets: HashMap<Key, Vec<&PlayRow>> = HashMap::new();
    for row in &rows {
        let key: Key = if let Some(client) = row.client_identifier.as_deref() {
            let rep = canonical.get(client).map_or(client, String::as_str);
            ("client_identifier", Some(rep.to_string()))
        } else if let Some(ip) = row.ip.as_deref().filter(|ip| eligible.contains(ip)) {
            ("ip", Some(ip.to_string()))
        } else if let Some(platform) = row.platform.as_deref() {
            ("platform", Some(platform.to_string()))
        } else {
            ("unclustered", None)
        };
        if !buckets.contains_key(&key) {
            order.push(key.clone());
        }
        buckets.entry(key).or_default().push(row);
    }

    let mut clusters: Vec<Cluster> = order
        .into_iter()
        .map(|(tier, value)| {
            let members = &buckets[&(tier, value.clone())];
            let mut depth: BTreeMap<&str, i64> = BTreeMap::new();
            for r in members {
                *depth.entry(r.unit_id.as_str()).or_default() += 1;
            }
            let joined = match (&value, tier) {
                (Some(rep), "client_identifier") => joined_by.get(rep).cloned().unwrap_or_default(),
                _ => Vec::new(),
            };
            Cluster {
                key_tier: tier,
                key_value: value,
                client_identifiers: sorted_set(
                    members
                        .iter()
                        .filter_map(|r| some_text(&r.client_identifier)),
                ),
                ips: sorted_set(members.iter().filter_map(|r| some_text(&r.ip))),
                platforms: sorted_set(members.iter().filter_map(|r| some_text(&r.platform))),
                item_ids: sorted_set(members.iter().map(|r| r.item_id.as_str())),
                units: depth.into_iter().map(|(u, n)| (u.to_string(), n)).collect(),
                play_count: members.len(),
                joined_by_ips: joined,
            }
        })
        .collect();

    let tier_order = |tier: &str| match tier {
        "client_identifier" => 0,
        "ip" => 1,
        "platform" => 2,
        _ => 3,
    };
    clusters.sort_by(|a, b| {
        (tier_order(a.key_tier), a.key_value.as_deref().unwrap_or(""))
            .cmp(&(tier_order(b.key_tier), b.key_value.as_deref().unwrap_or("")))
            .then(b.play_count.cmp(&a.play_count))
    });

    Ok(AccountClusters {
        play_count: rows.len(),
        distinct_client_identifiers: distinct.len(),
        clusters,
        transient_ips,
        discounted_join_ips: discounted,
    })
}

/// Every unit the account played, with play depth, over every play it has.
fn account_units(conn: &Connection, plex_account_id: i64) -> rusqlite::Result<Vec<(String, i64)>> {
    let rows = account_play_rows(conn, plex_account_id)?;
    let mut depth: BTreeMap<String, i64> = BTreeMap::new();
    for r in rows {
        *depth.entry(r.unit_id).or_default() += 1;
    }
    Ok(depth.into_iter().collect())
}

fn build_keyword_profile(
    conn: &Connection,
    units: &[(String, i64)],
) -> rusqlite::Result<KeywordProfile> {
    if units.is_empty() {
        return Ok(KeywordProfile {
            units_total: 0,
            units_with_coverage: 0,
            total_depth: 0,
            covered_depth: 0,
            top_keywords: Vec::new(),
        });
    }
    let depth_by_unit: HashMap<&str, i64> = units.iter().map(|(u, n)| (u.as_str(), *n)).collect();
    let ids: Vec<&str> = units.iter().map(|(u, _)| u.as_str()).collect();

    let mut counts: HashMap<String, i64> = HashMap::new();
    let mut covered: BTreeSet<&str> = BTreeSet::new();
    for chunk in ids.chunks(IN_CHUNK) {
        let placeholders = vec!["?"; chunk.len()].join(",");
        let mut stmt = conn.prepare(&format!(
            "SELECT DISTINCT item_id, value FROM enrichment \
             WHERE namespace = ? AND key = ? AND item_id IN ({placeholders})"
        ))?;
        let params = [KEYWORDS_NAMESPACE, KEYWORD_KEY]
            .into_iter()
            .chain(chunk.iter().copied());
        let found = stmt.query_map(rusqlite::params_from_iter(params), |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in found {
            let (item_id, value) = row?;
            *counts.entry(value).or_default() += 1;
            let unit = depth_by_unit
                .get_key_value(item_id.as_str())
                .map(|(unit, _)| *unit)
                .expect("a row is for a unit asked about");
            covered.insert(unit);
        }
    }

    let mut top: Vec<(String, i64)> = counts.into_iter().collect();
    top.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    top.truncate(TOP_N_KEYWORDS);
    Ok(KeywordProfile {
        units_total: depth_by_unit.len(),
        units_with_coverage: covered.len(),
        total_depth: depth_by_unit.values().sum(),
        covered_depth: covered.iter().map(|unit| depth_by_unit[unit]).sum(),
        top_keywords: top,
    })
}

/// Jaccard overlap of two top-N keyword sets; `None` when either has no coverage.
fn profile_overlap(a: &KeywordProfile, b: &KeywordProfile) -> Option<f64> {
    if !a.has_coverage() || !b.has_coverage() {
        return None;
    }
    let set_a: BTreeSet<&str> = a.top_keywords.iter().map(|(k, _)| k.as_str()).collect();
    let set_b: BTreeSet<&str> = b.top_keywords.iter().map(|(k, _)| k.as_str()).collect();
    let union = set_a.union(&set_b).count();
    if union == 0 {
        return None;
    }
    Some(set_a.intersection(&set_b).count() as f64 / union as f64)
}

fn account_header(plex_account_id: i64, names: &HashMap<i64, String>) -> String {
    match names.get(&plex_account_id).filter(|name| !name.is_empty()) {
        Some(name) => format!("== account {plex_account_id} ({name}) =="),
        None => format!("== account {plex_account_id} =="),
    }
}

fn keyword_profile_lines(profile: &KeywordProfile, indent: &str) -> Vec<String> {
    if profile.has_coverage() {
        let top: Vec<String> = profile
            .top_keywords
            .iter()
            .map(|(keyword, count)| format!("{keyword} ({count})"))
            .collect();
        vec![format!(
            "{indent}keyword profile ({}/{} unit(s) with tmdb_keywords coverage, spanning \
             {}/{} play(s) deep): {}",
            profile.units_with_coverage,
            profile.units_total,
            profile.covered_depth,
            profile.total_depth,
            top.join(", ")
        )]
    } else {
        vec![format!(
            "{indent}keyword profile: no enrichment coverage (0/{} unit(s) carry tmdb_keywords)",
            profile.units_total
        )]
    }
}

fn or_none(items: &[String]) -> String {
    if items.is_empty() {
        "(none)".into()
    } else {
        items.join(", ")
    }
}

fn render_shared_account(
    conn: &Connection,
    plex_account_id: i64,
    names: &HashMap<i64, String>,
) -> rusqlite::Result<String> {
    let account = cluster_account_plays(conn, plex_account_id)?;
    let kept: Vec<&Cluster> = account
        .clusters
        .iter()
        .filter(|c| c.play_count >= LATENT_USER_FLOOR)
        .collect();
    let sub_floor: Vec<&Cluster> = account
        .clusters
        .iter()
        .filter(|c| c.play_count < LATENT_USER_FLOOR)
        .collect();
    let mut lines = vec![
        account_header(plex_account_id, names),
        format!(
            "structural baseline: {} play(s), {} distinct client_identifier(s), {} cluster(s) \
             found, {} at or above the {LATENT_USER_FLOOR}-play floor",
            account.play_count,
            account.distinct_client_identifiers,
            account.clusters.len(),
            kept.len()
        ),
    ];
    if !sub_floor.is_empty() {
        let plays: usize = sub_floor.iter().map(|c| c.play_count).sum();
        let devices: usize = sub_floor
            .iter()
            .map(|c| c.client_identifiers.len().max(1))
            .sum();
        lines.push(format!(
            "unattributed: {plays} play(s) across {} cluster(s) under the {LATENT_USER_FLOOR}-play \
             floor ({devices} device(s)) — not a latent user, no keyword profile, no pairwise \
             overlap",
            sub_floor.len()
        ));
    }
    if !account.transient_ips.is_empty() {
        lines.push(format!(
            "transient IP(s) excluded from clustering (fewer than {MIN_IP_OCCURRENCES} plays \
             with no client_identifier, so treated as passing traffic rather than a household): \
             {}",
            account.transient_ips.join(", ")
        ));
    }
    if !account.discounted_join_ips.is_empty() {
        lines.push("IP(s) discounted from joining devices:".into());
        for discounted in &account.discounted_join_ips {
            lines.push(format!("  {}: {}", discounted.ip, discounted.reason));
        }
    }

    let mut profiles = Vec::new();
    for (ix, cluster) in kept.iter().enumerate() {
        let profile = build_keyword_profile(conn, &cluster.units)?;
        lines.push(String::new());
        let key_value = cluster
            .key_value
            .as_deref()
            .map_or("None".to_string(), py_repr);
        lines.push(format!(
            "cluster {} [{}={key_value}]",
            ix + 1,
            cluster.key_tier
        ));
        lines.push(format!(
            "  client_identifier(s): {}",
            or_none(&cluster.client_identifiers)
        ));
        if !cluster.joined_by_ips.is_empty() {
            lines.push(format!(
                "  joined by ip(s): {}",
                cluster.joined_by_ips.join(", ")
            ));
        }
        lines.push(format!("  ip(s) seen: {}", or_none(&cluster.ips)));
        lines.push(format!("  platform(s): {}", or_none(&cluster.platforms)));
        lines.push(format!(
            "  plays: {}, distinct items: {}, distinct units (a show counts once, not once per \
             episode): {}",
            cluster.play_count,
            cluster.item_ids.len(),
            cluster.units.len()
        ));
        lines.extend(keyword_profile_lines(&profile, "  "));
        profiles.push(profile);
    }

    lines.push(String::new());
    if kept.len() < 2 {
        lines.push("pairwise overlap: n/a (fewer than two clusters)".into());
    } else {
        lines.push(format!(
            "pairwise keyword-profile overlap (Jaccard over top-{TOP_N_KEYWORDS} keywords):"
        ));
        for i in 0..kept.len() {
            for j in (i + 1)..kept.len() {
                match profile_overlap(&profiles[i], &profiles[j]) {
                    None => lines.push(format!(
                        "  cluster {} vs cluster {}: n/a (no enrichment coverage on one or both)",
                        i + 1,
                        j + 1
                    )),
                    Some(overlap) => lines.push(format!(
                        "  cluster {} vs cluster {}: {overlap:.2}",
                        i + 1,
                        j + 1
                    )),
                }
            }
        }
    }
    Ok(lines.join("\n"))
}

fn render_personal_account(
    conn: &Connection,
    plex_account_id: i64,
    names: &HashMap<i64, String>,
) -> rusqlite::Result<String> {
    let units = account_units(conn, plex_account_id)?;
    let play_count: i64 = units.iter().map(|(_, depth)| depth).sum();
    let profile = build_keyword_profile(conn, &units)?;
    let mut lines = vec![
        account_header(plex_account_id, names),
        format!("plays: {play_count}"),
    ];
    lines.extend(keyword_profile_lines(&profile, ""));
    Ok(lines.join("\n"))
}

/// The full report, one section per account (`clusters.render_report`).
fn render_report(
    conn: &Connection,
    plex_account_ids: Option<&[i64]>,
    shared_account_ids: &[i64],
    names: &HashMap<i64, String>,
) -> rusqlite::Result<String> {
    let ids = match plex_account_ids {
        Some(ids) => ids.to_vec(),
        None => account_ids_with_plays(conn)?,
    };
    let mut sections = Vec::new();
    for id in ids {
        sections.push(if shared_account_ids.contains(&id) {
            render_shared_account(conn, id, names)?
        } else {
            render_personal_account(conn, id, names)?
        });
    }
    Ok(sections.join("\n\n") + "\n")
}

/// `PLEXDB_SHARED_ACCOUNT_IDS` (`config._shared_account_ids_from_env`): an explicitly
/// blank value means no shared account.
fn shared_account_ids_from_env() -> Result<Vec<i64>, StoreError> {
    let raw = std::env::var("PLEXDB_SHARED_ACCOUNT_IDS")
        .unwrap_or_else(|_| DEFAULT_SHARED_ACCOUNT_IDS.to_string());
    if py_strip(&raw).is_empty() {
        return Ok(Vec::new());
    }
    raw.split(',')
        .map(py_strip)
        .filter(|part| !part.is_empty())
        .map(|part| {
            py_int_text(part).ok_or_else(|| {
                StoreError::Config(format!(
                    "PLEXDB_SHARED_ACCOUNT_IDS must be a comma-separated list of whole numbers, \
                     got {}",
                    py_repr(&raw)
                ))
            })
        })
        .collect()
}

/// The name of each real person Plex lists (`plex_client.valid_accounts`): Plex's id-0
/// placeholder and any blank name are left out.
fn account_names(accounts: &[Value]) -> HashMap<i64, String> {
    accounts
        .iter()
        .filter_map(|account| {
            let id = account.get("id")?.as_i64().filter(|id| *id != 0)?;
            let name = account.get("name")?.as_str().filter(|n| !n.is_empty())?;
            Some((id, name.to_string()))
        })
        .collect()
}

/// `GET <base>/accounts` (`LivePlexClient.accounts`): the token rides in a header.
fn fetch_accounts(base: &str, token: &str) -> Result<Vec<Value>, Failure> {
    let url = format!("{}/accounts", base.trim_end_matches('/'));
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(PLEX_TIMEOUT))
        .max_redirects(0)
        .max_redirects_will_error(false)
        .http_status_as_error(false)
        .build()
        .into();
    let mut response = agent
        .get(&url)
        .header("Accept", "application/json")
        .header("X-Plex-Token", token)
        .call()
        .map_err(|err| {
            Failure::Failed(format!(
                "cannot reach Plex at {url}: {}",
                crate::frame_encoder::error_kind(&err)
            ))
        })?;
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        return Err(Failure::Failed(format!(
            "cannot reach Plex at {url}: HTTP {status}"
        )));
    }
    let body: Value = response.body_mut().read_json().map_err(|_| {
        Failure::Failed(format!(
            "Plex returned a response that is not JSON for {url}"
        ))
    })?;
    let Value::Object(body) = body else {
        return Err(Failure::Failed(format!(
            "Plex returned a body for {url} that is not an object"
        )));
    };
    Ok(body
        .get("MediaContainer")
        .and_then(|container| container.get("Account"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default())
}

fn parse_accounts(args: &[String]) -> Result<Vec<i64>, Failure> {
    let usage = || Failure::Usage("usage: latent-users [--account PLEX_ACCOUNT_ID]...".into());
    let mut ids = Vec::new();
    let mut words = args.iter();
    while let Some(word) = words.next() {
        let value: &str = if word == "--account" {
            words.next().ok_or_else(usage)?
        } else if let Some(value) = word.strip_prefix("--account=") {
            value
        } else {
            return Err(usage());
        };
        ids.push(py_int_text(value).ok_or_else(|| {
            Failure::Usage(format!(
                "argument --account: invalid int value: {}",
                py_repr(value)
            ))
        })?);
    }
    Ok(ids)
}

pub fn run(args: &[String]) -> Result<(), Failure> {
    let accounts = parse_accounts(args)?;
    let url = std::env::var("PLEX_URL").unwrap_or_default();
    let token = std::env::var("PLEX_TOKEN").unwrap_or_default();
    let (url, token) = (py_strip(&url), py_strip(&token));
    if url.is_empty() || token.is_empty() {
        return Err(Failure::Config(
            "PLEX_URL and PLEX_TOKEN must be set in .env to name accounts in the report".into(),
        ));
    }
    let shared = shared_account_ids_from_env().map_err(crate::steps::failure)?;
    let names = account_names(&fetch_accounts(url, token)?);
    let store = Config::from_env().map_err(crate::steps::failure)?;
    let conn = open_readonly(&store.store_path).map_err(crate::steps::failure)?;
    let ids = (!accounts.is_empty()).then_some(accounts.as_slice());
    let report = render_report(&conn, ids, &shared, &names)
        .map_err(|err| Failure::Failed(err.to_string()))?;
    print!("{report}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> Connection {
        let conn = Connection::open_in_memory().expect("memory db");
        conn.execute_batch(
            "CREATE TABLE items (item_id TEXT PRIMARY KEY, show_item_id TEXT);
             CREATE TABLE plays (item_id TEXT, plex_account_id INTEGER, client_identifier TEXT,
                                 ip TEXT, platform TEXT);
             CREATE TABLE enrichment (item_id TEXT, namespace TEXT, key TEXT, value TEXT);",
        )
        .expect("schema");
        conn
    }

    fn play(conn: &Connection, item: &str, account: i64, client: Option<&str>, ip: Option<&str>) {
        conn.execute("INSERT OR IGNORE INTO items (item_id) VALUES (?1)", [item])
            .expect("item");
        conn.execute(
            "INSERT INTO plays VALUES (?1, ?2, ?3, ?4, NULL)",
            rusqlite::params![item, account, client, ip],
        )
        .expect("play");
    }

    #[test]
    fn two_devices_on_one_recurring_private_ip_merge_under_the_smaller_id() {
        let conn = store();
        for n in 0..3 {
            play(
                &conn,
                &format!("m{n}"),
                1,
                Some("b-device"),
                Some("10.0.0.5"),
            );
            play(
                &conn,
                &format!("n{n}"),
                1,
                Some("a-device"),
                Some("10.0.0.5"),
            );
        }
        let account = cluster_account_plays(&conn, 1).expect("cluster");
        assert_eq!(account.clusters.len(), 1);
        assert_eq!(account.clusters[0].key_value.as_deref(), Some("a-device"));
        assert_eq!(account.clusters[0].joined_by_ips, ["10.0.0.5"]);
    }

    #[test]
    fn an_ip_under_two_accounts_is_discounted_not_joined() {
        let conn = store();
        for n in 0..3 {
            play(&conn, &format!("m{n}"), 1, Some("a"), Some("192.168.0.2"));
            play(&conn, &format!("n{n}"), 1, Some("b"), Some("192.168.0.2"));
        }
        play(&conn, "other", 2, Some("c"), Some("192.168.0.2"));
        let account = cluster_account_plays(&conn, 1).expect("cluster");
        assert_eq!(account.clusters.len(), 2);
        assert_eq!(account.discounted_join_ips[0].ip, "192.168.0.2");
        assert!(
            account.discounted_join_ips[0]
                .reason
                .starts_with("seen under 2 different")
        );
    }

    #[test]
    fn a_personal_account_reports_one_user_by_name() {
        let conn = store();
        play(&conn, "m1", 7, Some("a"), None);
        let names = HashMap::from([(7, "Madi".to_string())]);
        let report = render_report(&conn, None, &[], &names).expect("report");
        assert_eq!(
            report,
            "== account 7 (Madi) ==\nplays: 1\nkeyword profile: no enrichment coverage \
             (0/1 unit(s) carry tmdb_keywords)\n"
        );
    }

    #[test]
    fn the_blank_and_zero_accounts_are_left_out_of_the_names() {
        let accounts: Vec<Value> = serde_json::from_str(
            r#"[{"id":0,"name":"x"},{"id":3,"name":""},{"id":4,"name":"Jo"},{"name":"n"}]"#,
        )
        .expect("json");
        assert_eq!(
            account_names(&accounts),
            HashMap::from([(4, "Jo".to_string())])
        );
    }

    #[test]
    fn clusters_under_the_floor_fold_into_one_unattributed_line() {
        let conn = store();
        for n in 0..LATENT_USER_FLOOR {
            play(&conn, &format!("m{n}"), 1, Some("big"), None);
        }
        play(&conn, "x", 1, Some("small-1"), None);
        play(&conn, "y", 1, Some("small-2"), None);
        let report = render_report(&conn, Some(&[1]), &[1], &HashMap::new()).expect("report");
        assert!(
            report.contains("1 at or above the 20-play floor"),
            "{report}"
        );
        assert!(
            report.contains(
                "unattributed: 2 play(s) across 2 cluster(s) under the 20-play floor (2 device(s))"
            ),
            "{report}"
        );
        assert!(
            report.contains("pairwise overlap: n/a (fewer than two clusters)"),
            "{report}"
        );
    }
}
