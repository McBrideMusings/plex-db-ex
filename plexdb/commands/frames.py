"""`plexdb frames` — the Frame index's operator surface (ADR-0024).

`plexdb frames status` prints one JSON object describing `frames.db`; its shape
is documented in docs/schema.md. `plexdb frames queue <item_id>` asks the
harvester to index a title ahead of the backlog. `plexdb frames migrate` brings
`frames.db` to this build's schema. Each creates `frames.db` and applies its
migrations when needed, and none writes `plexdb.db`. `plexdb frames
probe-encoder` embeds one image and one phrase through the configured encoder
(ADR-0025) and touches neither file. `plexdb frames quant-agreement <phrase>…`
compares int8 against fp16 rankings over `frames-float.db` (`plexdb/frame_quant.py`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from typing import Any

from ..config import Config
from ..frame_encoder import (
    PROBE_IMAGE,
    PROBE_PHRASE,
    Embedded,
    configured_reachable,
    from_config,
)
from ..frame_quant import agreement, float_path, load, open_float
from ..frames import (
    FRAME_DIMS,
    decoder_report,
    frames_path,
    migrate_configured,
    open_frames,
    queue,
    section_titles,
    status,
)
from ..store import open_readonly

NAME = "frames"
#: With the other look-and-ask commands after the sweep's own steps. No `SWEEP`:
#: the harvester, not the nightly run, fills the Frame index.
ORDER = 75


def _cmd_status(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = frames_path(config)
    reachable = configured_reachable(config)
    with open_frames(path) as frames_conn, open_readonly(config.store_path) as store_conn:
        report = status(
            frames_conn,
            section_titles(store_conn),
            path=path,
            hours=config.frame_hours,
            now=datetime.now().astimezone(),
            encoder_reachable=reachable,
            ffmpeg_found=shutil.which(config.ffmpeg) is not None
            and shutil.which(config.ffprobe) is not None,
            decoder=decoder_report(path, config.frame_decode),
        )
    print(json.dumps(report, indent=2))
    return 0


def _call(answer: Embedded) -> dict[str, Any]:
    return {
        "dims": len(answer.vectors[0]),
        "first_byte_ms": round(answer.first_byte_s * 1000),
        "latency_ms": round(answer.total_s * 1000),
    }


def _cmd_probe_encoder(_args: argparse.Namespace) -> int:
    encoder = from_config(Config.from_env())
    try:
        # The phrase goes first and again last: the first call pays any model
        # load, the last is warm, and the difference is the cold-start wait.
        cold = encoder.timed_texts([PROBE_PHRASE])
        image = encoder.timed_images([PROBE_IMAGE.read_bytes()])
        text = encoder.timed_texts([PROBE_PHRASE])
    finally:
        encoder.close()
    report = {
        "encoder_url": encoder.base_url,
        "model": encoder.model,
        "served_by": image.model,
        "image": _call(image),
        "text": {**_call(text), "phrase": PROBE_PHRASE},
        "cold_start_wait_ms": max(0, round((cold.first_byte_s - text.first_byte_s) * 1000)),
        "frame_dims": FRAME_DIMS,
    }
    print(json.dumps(report, indent=2))
    widths = {len(image.vectors[0]), len(text.vectors[0])}
    if len(widths) != 1:
        print(f"error: image and text vectors differ in width: {sorted(widths)}", file=sys.stderr)
        return 1
    if widths != {FRAME_DIMS}:
        print(
            f"error: the encoder returns {widths.pop()}-wide vectors; frame_vec holds {FRAME_DIMS}",
            file=sys.stderr,
        )
        return 1
    # An answer naming no model is not a mismatch; one naming another model is.
    if {image.model, text.model} - {encoder.model, ""}:
        print(
            f"error: asked for {encoder.model}, the encoder answered as "
            f"{' and '.join(sorted({image.model, text.model}))}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = frames_path(config)
    with open_readonly(config.store_path) as store_conn:
        known = (
            store_conn.execute("SELECT 1 FROM items WHERE item_id = ?", (args.item_id,)).fetchone()
            is not None
        )
    with open_frames(path) as frames_conn:
        added = queue(
            frames_conn,
            args.item_id,
            known=known,
            by=args.by,
            now=datetime.now().astimezone(),
        )
        waiting = int(frames_conn.execute("SELECT count(*) FROM frame_queue").fetchone()[0])
    verb = "queued" if added else "already queued"
    print(f"{verb} {args.item_id}; {waiting:,} title(s) in the queue")
    return 0


def _positive(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{raw} is not a positive count")
    return value


def _cmd_quant_agreement(args: argparse.Namespace) -> int:
    config = Config.from_env()
    path = frames_path(config)
    floats = float_path(path)
    if not floats.is_file():
        print(
            f"error: {floats} does not exist; run harvest-frames --keep-float first",
            file=sys.stderr,
        )
        return 1
    encoder = from_config(config)
    try:
        vectors = encoder.embed_texts(args.phrases)
    finally:
        encoder.close()
    with open_frames(path) as frames_conn, open_float(floats) as float_conn:
        paired = load(frames_conn, float_conn)
    ks = args.top or [10, 50]
    report = {
        "titles": paired.titles,
        "titles_without_floats": paired.titles_without_floats,
        "phrases": [
            {"phrase": phrase, **agreement(paired, vec, ks=ks)}
            for phrase, vec in zip(args.phrases, vectors, strict=True)
        ],
    }
    print(json.dumps(report, indent=2))
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    config = Config.from_env()
    frames_path(config)
    print(migrate_configured(config))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        NAME,
        help="report on the Frame index, queue a title for it, migrate it, or probe its encoder",
    )
    actions = parser.add_subparsers(dest="frames_command", required=True)

    migrate_parser = actions.add_parser(
        "migrate", help="create frames.db or bring it to the current schema; safe to re-run"
    )
    migrate_parser.set_defaults(func=_cmd_migrate)

    status_parser = actions.add_parser(
        "status", help="print frames.db's coverage, size, hours and failures as JSON"
    )
    status_parser.set_defaults(func=_cmd_status)

    queue_parser = actions.add_parser(
        "queue", help="index this title next; exits non-zero for an item_id not in items"
    )
    queue_parser.add_argument("item_id")
    queue_parser.add_argument(
        "--by", default="cli", help="who asked, stored as queued_by (default: cli)"
    )
    queue_parser.set_defaults(func=_cmd_queue)

    probe_parser = actions.add_parser(
        "probe-encoder",
        help="embed one image and one phrase through the configured encoder and print "
        "the model, vector widths and latencies as JSON",
    )
    probe_parser.set_defaults(func=_cmd_probe_encoder)

    quant_parser = actions.add_parser(
        "quant-agreement",
        help="rank Shots for each phrase from the int8 index and from frames-float.db's "
        "vectors cast to fp16, and print how many of each top k match, as JSON",
    )
    quant_parser.add_argument("phrases", nargs="+", metavar="phrase")
    quant_parser.add_argument(
        "--top",
        type=_positive,
        action="append",
        help="a k to compare; repeat for several (default: 10 and 50)",
    )
    quant_parser.set_defaults(func=_cmd_quant_agreement)
