# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy full workspace (respects .dockerignore)
COPY . .

# Install all workspace deps in one pass — simpler and reliable for uv workspaces
RUN uv sync --frozen --no-dev --all-packages

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /dagster_home

# ── Dagster target ────────────────────────────────────────────────────────────
FROM base AS dagster

# ── API target ────────────────────────────────────────────────────────────────
FROM base AS api
