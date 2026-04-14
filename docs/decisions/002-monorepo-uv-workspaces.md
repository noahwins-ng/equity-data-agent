# ADR-002: Monorepo with uv workspaces over separate repos

**Date**: 2026-04-12
**Status**: Accepted

## Context
The system has 4 logical components (shared schemas, Dagster pipelines, FastAPI, LangGraph agent). Need to decide on repo structure.

## Decision
Single monorepo using uv workspaces with 4 packages under `packages/`.

## Alternatives Considered
- **Separate repos**: Independent deployment and CI. But introduces cross-repo dependency management, schema drift between services, and slower iteration for a solo developer.
- **Single flat package**: Simplest structure. But no import boundaries — easy to accidentally import Dagster dependencies in the API package or create circular dependencies.

## Consequences
- **Positive**: Single CI pipeline, shared schemas prevent drift, atomic commits across components, `uv sync` resolves everything.
- **Negative**: Slightly more complex initial setup (workspace config). All services share the same git history.
- **Mitigated by**: uv workspaces handle dependency isolation cleanly. For a solo dev, shared git history is a feature, not a bug.
