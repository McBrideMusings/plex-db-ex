//! The `harvest-audio` tests that start real processes: the `plexdb-rs` binary, the
//! analyzer and the duration probe. They take no run lock in this process, so a child that
//! holds a copy of an open lock file never makes another test here see `Busy`.

mod common;

use std::path::Path;
use std::time::Duration;

use common::audio::{MIN_20, World};
use plexdb::harvest_audio::{AnalyzeError, Analyzer, ProcessAnalyzer};

#[test]
fn without_libraries_the_command_exits_not_configured_and_analyzes_nothing() {
    let w = World::new();
    w.part(1, "1", "a.wav", b"aaaa", MIN_20);
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .arg("harvest-audio")
        .env("PLEXDB_PATH", &w.store)
        .env_remove("PLEXDB_AUDIO_LIBRARIES")
        .output()
        .expect("run plexdb-rs");
    assert_eq!(out.status.code(), Some(78));
    assert!(String::from_utf8_lossy(&out.stderr).contains("PLEXDB_AUDIO_LIBRARIES is not set"));
    assert!(!w.has_state(1));

    let status = std::process::Command::new(env!("CARGO_BIN_EXE_plexdb-rs"))
        .args(["audio", "status"])
        .env("PLEXDB_PATH", &w.store)
        .env_remove("PLEXDB_AUDIO_LIBRARIES")
        .output()
        .expect("run plexdb-rs");
    assert_eq!(status.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&status.stdout).contains("off: PLEXDB_AUDIO_LIBRARIES"));
}

#[cfg(unix)]
#[test]
fn the_analyzer_process_runs_niced_single_threaded_and_reports_a_failure_by_its_stderr() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("analyze.sh");
    std::fs::write(
        &script,
        "#!/bin/sh\n\
         case \"$1\" in *bad*) echo 'no audio stream' >&2; exit 3;; esac\n\
         printf '{\"file\": \"%s\", \"omp\": \"%s\", \"openblas\": \"%s\", \"nice\": %s}\\n' \
         \"$1\" \"$OMP_NUM_THREADS\" \"$OPENBLAS_NUM_THREADS\" \"$(ps -o ni= -p $$)\"\n",
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let analyzer = ProcessAnalyzer {
        program: script.to_string_lossy().into_owned(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(60),
    };

    let facts = analyzer.analyze(Path::new("/x/good.wav")).expect("facts");
    assert_eq!(facts["omp"], "1");
    assert_eq!(facts["openblas"], "1");
    assert_eq!(facts["nice"], 19);

    match analyzer.analyze(Path::new("/x/bad.wav")) {
        Err(AnalyzeError::Failed(message)) => {
            assert!(
                message.contains("exited 3") && message.contains("no audio stream"),
                "{message}"
            )
        }
        other => panic!("expected failed, got {other:?}"),
    }
    let missing = ProcessAnalyzer {
        program: "/nonexistent/analyzer".into(),
        ffprobe: "unused".into(),
        timeout: Duration::from_secs(60),
    };
    assert!(matches!(
        missing.analyze(Path::new("/x/a.wav")),
        Err(AnalyzeError::Unavailable(_))
    ));
}

#[cfg(unix)]
#[test]
fn the_duration_probe_runs_ffprobe_and_reports_its_failure() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("ffprobe.sh");
    std::fs::write(
        &script,
        "#!/bin/sh\n\
         for last; do :; done\n\
         case \"$last\" in *bad*) echo 'moov atom not found' >&2; exit 1;; *none*) echo N/A; exit 0;; esac\n\
         echo 12.5\n",
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let analyzer = ProcessAnalyzer {
        program: "unused".into(),
        ffprobe: script.to_string_lossy().into_owned(),
        timeout: Duration::from_secs(60),
    };

    assert_eq!(
        analyzer.duration_ms(Path::new("/x/a.mp4")),
        Ok(Some(12_500))
    );
    assert_eq!(analyzer.duration_ms(Path::new("/x/none.mp4")), Ok(None));
    match analyzer.duration_ms(Path::new("/x/bad.mp4")) {
        Err(AnalyzeError::Failed(message)) => {
            assert!(
                message.contains("exited 1") && message.contains("moov atom"),
                "{message}"
            )
        }
        other => panic!("expected failed, got {other:?}"),
    }
    let missing = ProcessAnalyzer {
        program: "unused".into(),
        ffprobe: "/nonexistent/ffprobe".into(),
        timeout: Duration::from_secs(60),
    };
    assert!(matches!(
        missing.duration_ms(Path::new("/x/a.mp4")),
        Err(AnalyzeError::Unavailable(_))
    ));
}
