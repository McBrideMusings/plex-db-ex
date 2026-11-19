//! An analyzer past its timeout, run through a real harvest. It both starts a child and
//! holds the run lock, so it has a process to itself: a child another test started would
//! hold a copy of the lock file and keep the released lock held.

mod common;

use std::time::Duration;

use common::audio::{MIN_20, World};
use plexdb::harvest_audio::{ProcessAnalyzer, harvest_running};

#[cfg(unix)]
#[test]
fn an_analyzer_past_its_timeout_is_killed_with_its_children_and_recorded_failed() {
    use std::os::unix::fs::PermissionsExt;
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let script = w._dir.path().join("slow.sh");
    // `sleep` is a child of the shell: killing only the shell would leave it holding the pipes.
    std::fs::write(&script, "#!/bin/sh\nsleep 30\necho '{}'\n").unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let analyzer = ProcessAnalyzer {
        program: script.to_string_lossy().into_owned(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(1),
    };

    let started = std::time::Instant::now();
    let report = w.run(&analyzer).expect("run");

    assert!(
        started.elapsed() < Duration::from_secs(10),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(report.failed, 1);
    let (outcome, _, _, error) = w.state(1);
    assert_eq!(outcome, "failed");
    assert_eq!(error.as_deref(), Some("analyzer timed out after 1s"));
    assert!(!harvest_running(&w.store), "the run lock is released");
}
