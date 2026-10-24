# The writer, packaged for the Unraid host its sources and its consumer already
# live on (issue #43).
#
# The entrypoint is `plexdb schedule` and nothing else. There is no shell
# wrapper here, and there is deliberately nowhere in this file that names a
# step, an order, or what a step's failure means — `plexdb sweep` owns all
# three by reading each command module's own `ORDER` and `SWEEP` (ADR-0014),
# and `plexdb schedule` adds a clock and no more (`plexdb/schedule.py`).
#
# Pinned to linux/amd64 in the FROM rather than left to `--platform` on the
# build command, because it is always built from an arm64 Mac and always run on
# an amd64 host: an image that silently came out arm64 would fail on the host
# and nowhere before it.

# `plexdb-read` is the reader crate's command line, and the one search
# implementation `plexdb search` runs (ADR-0026). `plexdb-rs` is the Rust writer,
# which runs the sweep steps moved from Python (ADR-0001); without it those steps
# fail and the sweep reports them. Built on Debian bookworm so their glibc is no
# newer than the Python image's.
FROM --platform=linux/amd64 rust:1.97-slim-bookworm AS rust

WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
RUN cargo build --release --locked -p plexdb-reader --bin plexdb-read -p plexdb --bin plexdb-rs

FROM --platform=linux/amd64 python:3.12-slim

# Pinned to the version the checkout resolves `uv.lock` with, so the image's
# dependency resolution is the one that was tested rather than whatever `latest`
# meant on build day.
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

# ffmpeg and ffprobe, from Debian: the frame harvester cuts and samples a title's
# video with them, reading it over HTTP from Plex (no media mount). The paths can
# be overridden with PLEXDB_FFMPEG and PLEXDB_FFPROBE; `plexdb frames status`
# reports `ffmpeg_found`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=rust /build/target/release/plexdb-read /usr/local/bin/plexdb-read
COPY --from=rust /build/target/release/plexdb-rs /usr/local/bin/plexdb-rs

# The lockfile and the package, and nothing else — no tests, no docs, no `data/`. `.dockerignore` is what keeps a 75 MB store out of the
# build context by accident.
COPY pyproject.toml uv.lock ./
COPY plexdb ./plexdb

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PLEXDB_RS_BIN=/usr/local/bin/plexdb-rs

# Paths inside the container, mounted from the host. Not secrets, so unlike
# every URL and token they may carry a real value here: `/data` is the store's
# own volume, `/snapshot` is the volume the station container already reads, so
# `publish` writes where the consumer looks and nothing copies anything
# (issue #43). `/explore-data` is Plex DB-EX's own mount — saved queries,
# decision files, tokenizer, clips cache — never `/snapshot`: Plex DB-EX has no
# login, so anyone who reaches the port could otherwise write into the
# directory the station reads (plex-db-ex-oyg.3; `SavedQueries` additionally
# caps entry count and total size). `/frames` holds the Frame index, frames.db
# (ADR-0024): tens of gigabytes at full library and rebuildable, so it has a
# mount of its own and the store's backups, which copy plexdb.db alone, never
# include it. `/media` is the library, mounted read-only where Plex's own
# container mounts it, so the file paths Plex reports open here unchanged; the
# frame harvester and the Search tab read video there. Credentials arrive
# from the host environment and appear in no committed file.
ENV PLEXDB_PATH=/data/plexdb.db \
    PLEXDB_SNAPSHOT_PATH=/snapshot/plexdb.snapshot.db \
    PLEXDB_EXPLORE_SAVED_PATH=/explore-data/explore-queries.json \
    PLEXDB_FRAMES_DIR=/frames \
    PLEXDB_MEDIA_DIR=/media

VOLUME ["/data", "/snapshot", "/explore-data", "/frames"]

# The read-only Plex DB-EX page `plexdb schedule` serves beside the sweep when
# PLEXDB_EXPLORE_PORT is set, reading the snapshot. Documentation only: whether
# the port is published, and on which host address, is `[docker_run]`'s call.
EXPOSE 5194

ENTRYPOINT ["plexdb", "schedule"]
