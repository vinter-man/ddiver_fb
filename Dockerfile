# syntax=docker/dockerfile:1
#
# The brain (ddiver-fb): the only part of this repository that runs in a container. The collector
# runs at home, natively, next to a real Chrome (DESIGN.md "Process model"), and never ships here.
#
# ddiver-core comes from the ddiver repository (private until 2026-10-24), pinned in uv.lock to
# the tag core-v0.1.0 as an https URL (what the Mac and CI use). Inside this build git rewrites that URL to ssh, and
# the fetch authenticates through BuildKit's ssh mount with a key that can read the ddiver
# repository (on the VPS: its read-only deploy key). Build from the repository root with an
# ssh-agent that holds that key and no other GitHub key, since GitHub stops at the first key it
# accepts and a deploy key for another repository would be accepted and then refused:
#
#   eval "$(ssh-agent -s)" && ssh-add ~/.ssh/<ddiver deploy key>
#   GIT_SHA=$(git rev-parse HEAD) docker compose build     # compose passes `ssh: [default]`
#   ssh-agent -k
#
# or, without compose:
#
#   docker build --ssh default --build-arg GIT_SHA=$(git rev-parse HEAD) -t ddiver-fb:latest .
#
# The key only ever reaches the build through the ssh mount, so it never lands in a layer, and
# github.com is checked against deploy/brain/github_known_hosts (pinned; the fingerprints are the
# ones GitHub publishes), never trusted on first use.

FROM python:3.12-slim AS builder

# The lock's https://github.com/ URLs are fetched over ssh. The rewrite lives in this stage's
# /etc/gitconfig only, so the lock stays the one every other install reads, and the runtime
# stage has no git at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system url."ssh://git@github.com/".insteadOf "https://github.com/"

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ssh/ssh_known_hosts -o BatchMode=yes"
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
COPY deploy/brain/github_known_hosts /etc/ssh/ssh_known_hosts

WORKDIR /app
# Dependencies first, from the lockfile and every member's pyproject (uv needs them all to read
# the workspace), so a code change does not reinstall Telethon. `--package ddiver-fb` keeps the
# collector's own dependencies (Patchright and its browser tooling) out of this image entirely,
# and `--no-dev` the core's test fakes (ddiver-core-testing). The git checkout of the core stays
# in the cache mount; only the built package reaches the venv.
COPY pyproject.toml uv.lock ./
COPY packages/fbwire/pyproject.toml ./packages/fbwire/
COPY packages/fbcollector/pyproject.toml ./packages/fbcollector/
COPY packages/ddiver_fb/pyproject.toml ./packages/ddiver_fb/
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=ssh \
    uv sync --frozen --no-dev --no-install-workspace --package ddiver-fb
# Then the code. --no-editable installs the packages into the venv as ordinary wheels, so the
# runtime stage needs the venv and nothing else.
COPY packages/fbwire ./packages/fbwire
COPY packages/ddiver_fb ./packages/ddiver_fb
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=ssh \
    uv sync --frozen --no-dev --no-editable --package ddiver-fb


FROM python:3.12-slim

# Passed by the deploy so the running container can say which commit it is; last, so a new
# value does not invalidate the layers above it.
ARG GIT_SHA=unknown

# INGEST_HOST=0.0.0.0 is the container's own interface: compose publishes the port on the
# VPS's 127.0.0.1 only (tests/test_brain_packaging.py holds it to that).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    INGEST_HOST=0.0.0.0 \
    INGEST_PORT=8781 \
    DDIVER_COMMIT=$GIT_SHA

RUN useradd --create-home --uid 10002 ddiverfb \
    && mkdir -p /data \
    && chown -R ddiverfb:ddiverfb /data

WORKDIR /app
COPY --from=builder --chown=ddiverfb:ddiverfb /app/.venv /app/.venv

USER ddiverfb
VOLUME ["/data"]
EXPOSE 8781

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["ddiver-fb", "health"]

ENTRYPOINT ["ddiver-fb"]
CMD ["run"]
