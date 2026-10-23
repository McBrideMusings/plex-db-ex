"""The sweep steps that have moved to Rust, run through the `plexdb-rs` binary.

The writer is moving to one Rust binary a sweep step at a time (ADR-0001). Until
the move ends, `plexdb-rs steps --json` declares each moved step's name, order and
failure policy, the sweep merges them into its plan (`sweep.plan`), and `plexdb
<step>` hands a moved step to the binary (`cli.main`), so `plexdb idle <step>` and
`admin host-exec <step>` keep working.

Both implementations take the same writer lock (`store.writing`). A store this
process holds exclusive (`store.holding`, as `plexdb idle <command>` does) is
named to the child in `PLEXDB_HELD_STORE`, because the child's own shared lock on
a new file handle would wait on this process forever.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config
from .errors import ConfigError, PlexdbError
from .store import held_path

__all__ = ["HELD_STORE_VAR", "NOT_CONFIGURED", "RustStep", "RustStepError", "run", "steps"]

#: The exit code `plexdb-rs` uses for a setting that is absent (`EX_CONFIG`); the
#: sweep reports the step as skipped, as it does a Python step's `ConfigError`.
NOT_CONFIGURED = 78

HELD_STORE_VAR = "PLEXDB_HELD_STORE"


class RustStepError(PlexdbError):
    """A step `plexdb-rs` ran exited non-zero; the message is its `error:` line."""


@dataclass(frozen=True)
class RustStep:
    name: str
    order: int
    #: `required` or `best-effort`, the values of `sweep.Step`.
    policy: str


def binary(config: Config) -> str:
    """The `plexdb-rs` binary, or a `ConfigError` saying how to get one."""
    found = shutil.which(config.rs_bin)
    if found is None:
        raise ConfigError(
            f"cannot run {config.rs_bin!r}: not found. The container image carries plexdb-rs; "
            "elsewhere build it with `cargo build -p plexdb --bin plexdb-rs` and set "
            "PLEXDB_RS_BIN to target/debug/plexdb-rs (`admin dev` does both)"
        )
    return found


def steps(config: Config | None = None) -> list[RustStep]:
    """Every step `plexdb-rs` declares, as `plexdb-rs steps --json` lists them."""
    config = config or Config.from_env()
    argv = [binary(config), "steps", "--json"]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RustStepError(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()}")
    try:
        declared = json.loads(done.stdout)
        return [RustStep(str(s["name"]), int(s["order"]), str(s["policy"])) for s in declared]
    except (ValueError, KeyError, TypeError) as err:
        raise RustStepError(f"{' '.join(argv)} printed no step list: {err}") from None


def run(args: Sequence[str], config: Config | None = None) -> int:
    """Run `plexdb-rs <args>`, its output on this process's stdout.

    Returns 0 when it succeeds. Raises `ConfigError` when it exits
    `NOT_CONFIGURED` and `RustStepError` on any other failure, each carrying the
    binary's own `error:` line, so `cli.main` and the sweep report it as they
    report a Python step's.
    """
    config = config or Config.from_env()
    env = dict(os.environ)
    held = held_path(config.store_path)
    if held is not None:
        env[HELD_STORE_VAR] = str(held)
    else:
        env.pop(HELD_STORE_VAR, None)
    sys.stdout.flush()
    done = subprocess.run(
        [binary(config), *args], env=env, stderr=subprocess.PIPE, text=True, check=False
    )
    if done.returncode == 0:
        sys.stderr.write(done.stderr)
        return 0
    message = done.stderr.strip().removeprefix("error: ") or f"exit code {done.returncode}"
    if done.returncode == NOT_CONFIGURED:
        raise ConfigError(message)
    raise RustStepError(message)
