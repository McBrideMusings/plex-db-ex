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

FROM --platform=linux/amd64 python:3.12-slim

# Pinned to the version the checkout resolves `uv.lock` with, so the image's
# dependency resolution is the one that was tested rather than whatever `latest`
# meant on build day.
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

WORKDIR /app

# The lockfile and the package, and nothing else — no tests, no docs, no Rust
# crate, no `data/`. `.dockerignore` is what keeps a 75 MB store out of the
# build context by accident.
COPY pyproject.toml uv.lock ./
COPY plexdb ./plexdb

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Paths inside the container, mounted from the host. Not secrets, so unlike
# every URL and token they may carry a real value here: `/data` is the store's
# own volume, `/snapshot` is the volume the station container already reads, so
# `publish` writes where the consumer looks and nothing copies anything
# (issue #43). Credentials arrive from the host environment and appear in no
# committed file.
ENV PLEXDB_PATH=/data/plexdb.db \
    PLEXDB_SNAPSHOT_PATH=/snapshot/plexdb.snapshot.db

VOLUME ["/data", "/snapshot"]

# The read-only tag explorer `plexdb schedule` serves beside the sweep when
# PLEXDB_EXPLORE_PORT is set, reading the snapshot. Documentation only: whether
# the port is published, and on which host address, is `[docker_run]`'s call.
EXPOSE 5194

ENTRYPOINT ["plexdb", "schedule"]
