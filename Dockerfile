# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# QNT-385: restore uv layer caching. Copy the lockfile + every workspace
# member's pyproject.toml FIRST, then install external deps without the
# workspace source (--no-install-workspace). This dependency layer is cached
# and only re-runs when the lockfile or a pyproject.toml changes — a code-only
# deploy (the common case on the CX41's serialized deploy queue) reuses it and
# skips the full dependency install entirely.
COPY pyproject.toml uv.lock ./
COPY packages/shared/pyproject.toml packages/shared/pyproject.toml
COPY packages/dagster-pipelines/pyproject.toml packages/dagster-pipelines/pyproject.toml
COPY packages/api/pyproject.toml packages/api/pyproject.toml
COPY packages/agent/pyproject.toml packages/agent/pyproject.toml
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace

# Now copy the source and install the workspace packages themselves. Deps are
# already present from the cached layer above, so this is fast.
COPY . .
RUN uv sync --frozen --no-dev --all-packages

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /dagster_home

# ── Dagster target ────────────────────────────────────────────────────────────
FROM base AS dagster

# ── API target ────────────────────────────────────────────────────────────────
FROM base AS api
