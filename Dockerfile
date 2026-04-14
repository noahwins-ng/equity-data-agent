# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace config — separate layer so deps are cached independently of source changes
COPY pyproject.toml uv.lock ./
COPY packages/shared/pyproject.toml       packages/shared/pyproject.toml
COPY packages/dagster-pipelines/pyproject.toml packages/dagster-pipelines/pyproject.toml
COPY packages/api/pyproject.toml          packages/api/pyproject.toml
COPY packages/agent/pyproject.toml        packages/agent/pyproject.toml

# Install external deps only (skip workspace packages — source not copied yet)
RUN uv sync --frozen --no-dev --no-install-workspace

# Copy source
COPY packages/ packages/

# Install workspace packages from source
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# ── Dagster target ────────────────────────────────────────────────────────────
FROM base AS dagster
# Command is set per-service in docker-compose.yml:
#   webserver: dagster-webserver -m dagster_pipelines.definitions -h 0.0.0.0 -p 3000
#   daemon:    dagster-daemon run -m dagster_pipelines.definitions

# ── API target ────────────────────────────────────────────────────────────────
FROM base AS api
# Command is set in docker-compose.yml:
#   uvicorn api.main:app --host 0.0.0.0 --port 8000
