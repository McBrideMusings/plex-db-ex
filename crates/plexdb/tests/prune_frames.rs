//! `prune-frames` against a `frames.db` shaped like the one `plexdb frames migrate`
//! builds (ADR-0024): a title Plex no longer has loses its vectors, cuts, shots, queue
//! row and state row, and nothing else does.

use std::collections::BTreeSet;
use std::path::Path;

use plexdb::frames::open_frames;
use plexdb::prune_frames::{describe, prune_orphans};

/// A Frame index built by the real chain, holding a kept title and a gone one.
fn build(path: &Path) {
    let conn = open_frames(path).expect("the real Frame index chain builds the file");
    let zeros = format!("[{}]", vec!["0"; 768].join(","));
    for item_id in ["kept", "gone"] {
        for shot in 0..3 {
            conn.execute(
                "INSERT INTO frame_vec (item_id, t_ms, shot_ix, vec) VALUES (?, ?, ?, vec_int8(?))",
                (item_id, shot * 2000, shot, &zeros),
            )
            .expect("vector");
        }
        conn.execute(
            "INSERT INTO frame_index_state (item_id, indexed_at) VALUES (?, '2026-01-01')",
            [item_id],
        )
        .expect("state");
        conn.execute(
            "INSERT INTO cuts (item_id, t_ms, source) VALUES (?, 500, 'scdet')",
            [item_id],
        )
        .expect("cut");
        conn.execute("INSERT INTO shots VALUES (?, 0, 0, 500)", [item_id])
            .expect("shot");
        conn.execute(
            "INSERT INTO frame_queue VALUES (?, '2026-01-01', 'test')",
            [item_id],
        )
        .expect("queue");
    }
}

#[test]
fn a_title_the_store_no_longer_holds_is_pruned_from_every_table_and_no_other() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("frames.db");
    build(&path);
    let live: BTreeSet<String> = ["kept".to_string()].into();

    let mut conn = open_frames(&path).expect("open");
    let stats = prune_orphans(&mut conn, &live).expect("prune");

    assert_eq!(stats.item_ids, ["gone"]);
    assert_eq!(
        stats.deleted,
        [
            ("frame_vec", 3),
            ("cuts", 1),
            ("shots", 1),
            ("frame_queue", 1),
            ("frame_index_state", 1)
        ]
    );
    assert_eq!(
        describe(&stats),
        [
            "pruned 1 title(s) no longer in items: 3 frame_vec row(s), 1 cuts row(s), \
             1 shots row(s), 1 frame_queue row(s), 1 frame_index_state row(s)"
                .to_string(),
            "pruned gone".to_string()
        ]
    );
    for table in [
        "frame_vec",
        "cuts",
        "shots",
        "frame_queue",
        "frame_index_state",
    ] {
        let kept: i64 = conn
            .query_row(
                &format!("SELECT count(*) FROM {table} WHERE item_id = 'kept'"),
                [],
                |row| row.get(0),
            )
            .expect("count");
        let all: i64 = conn
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .expect("count");
        assert_eq!(kept, all, "{table} holds only the kept title now");
        assert!(all > 0, "{table} still holds the kept title");
    }
}
