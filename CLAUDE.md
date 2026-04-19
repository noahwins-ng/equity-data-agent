# Equity Data Agent — Project Conventions

## Core Philosophy: Intelligence vs. Math

This project enforces a hard separation between calculation and reasoning:

- **Calculation Layer** (Python/SQL): Handles 100% of the math
- **Reasoning Layer** (LLM): Interprets pre-computed results only

### Non-Negotiable Rules

1. **The LLM never does arithmetic.** No RSI calculations, no percentage changes, no YoY growth in agent code. All math lives in Dagster assets or SQL.
2. **The Agent never touches the database.** It calls FastAPI endpoints via tools. If the agent needs data, it calls a tool that hits an API.
3. **Three roles, no overlap:**
   - Dagster = Worker (fetches and transforms data)
   - FastAPI = Interpreter (turns DB rows into readable reports)
   - LangGraph = Executive (reasons over reports, synthesizes theses)

## Architecture

```
Data Sources → Dagster → ClickHouse/Qdrant → FastAPI → LangGraph Agent
```

- **ClickHouse**: `equity_raw` (ingested data) + `equity_derived` (computed indicators)
- **Qdrant Cloud**: Vector store for news embeddings
- **All tables use ReplacingMergeTree** for idempotency

## Stack

- Python 3.12+, uv workspaces
- Dagster, FastAPI, LangGraph, ClickHouse, Qdrant Cloud
- LiteLLM Proxy (Ollama Cloud / Claude API)
- Langfuse for agent tracing

## Repo Structure

Monorepo with 4 packages under `packages/`:
- `shared` — Pydantic schemas, config, ticker registry (the glue)
- `dagster-pipelines` — assets, sensors, schedules
- `api` — FastAPI endpoints
- `agent` — LangGraph agent

## Code Style

- Lint: `ruff check`
- Format: `ruff format`
- Type check: `pyright`
- Test: `pytest`

## Git Workflow

### Branching
- One branch per Linear issue
- Use Linear's auto-generated branch names: `noahwinsdev/qnt-XX-description`

### Commit Format
```
QNT-XX: type(scope): description
```
Examples:
```
QNT-34: feat(shared): add Settings class with env-aware config
QNT-37: feat(db): add ClickHouse DDL migrations
QNT-41: fix(dagster): handle yfinance 429 with exponential backoff
```
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

### Pull Requests
- One PR per issue
- PR body must include `Closes QNT-XX` for Linear auto-close
- Squash merge to main
- Run `make build` locally before pushing if `Dockerfile`, `docker-compose.yml`, or any `pyproject.toml` was changed

## Environment

- **Dev**: MacBook M4, ClickHouse via SSH tunnel (`CLICKHOUSE_HOST=localhost`)
- **Prod**: Hetzner CX41, Docker Compose (`CLICKHOUSE_HOST=clickhouse`)
- Switch via `ENV=dev|prod` in `.env`

## Ticker Scope

10 US equities — defined in `packages/shared/src/shared/tickers.py`. Adding a ticker = adding one string. Never hardcode ticker lists in business logic.

## Working Docs

The `docs/` folder is the shared brain for this project. Read relevant sections when starting work on a new area.

- `docs/architecture/` — how the system works (read first)
- `docs/patterns.md` — established code recipes (read before implementing)
- `docs/decisions/` — ADRs: why we chose X over Y (read when questioning a design choice)
- `docs/guides/` — how to do common tasks
- `docs/api/*.http` — API test files (open with VS Code REST Client)

When making a significant architectural decision, create a new ADR using `docs/decisions/TEMPLATE.md`.

## Observability

- **Langfuse**: Agent tracing — LLM calls, tool calls, latency
- **Sentry**: FastAPI error tracking in production
- **Health Monitor**: Cron on Hetzner (every 15 min) — checks API `/health` + Docker services, logs failures. `make monitor-log` to check. Session-start hook auto-warns on failures.
- **Alerting**: Two independent channels (QNT-101) — external uptime probe on `/api/v1/health` (BetterStack free tier, alerts ≤3 min) + `docker-events-notify.service` on Hetzner streaming die/kill/oom/restart to a Discord webhook (alerts ≤30 s). Setup: `docs/guides/uptime-monitoring.md`. Self-monitored via heartbeat file + optional external heartbeat URL. Install: `make events-notify-install`. Test: `make events-notify-test`.
- **Ops Runbook**: `docs/guides/ops-runbook.md` — failure-mode catalog with symptoms, diagnosis, response, and prevention. Grep this first when prod breaks; every Ops & Reliability ticket extends it.
- **ClickHouse Play**: `http://localhost:8123/play` — SQL editor for data exploration (via SSH tunnel)
- **Dagster UI**: `http://localhost:3000` — asset lineage, run history, sensor status

## Common Commands

### Dev Commands (Makefile)
```bash
make setup                          # First-time: hooks + deps + .env
make dev-dagster                    # Start Dagster UI (terminal 1)
make dev-api                        # Start FastAPI (terminal 2)
make dev-litellm                    # Start LiteLLM proxy on :4000 (terminal 3, from Phase 5)
make dev-frontend                   # Start Next.js on localhost:3001 (terminal 4)
make tunnel                         # SSH tunnel to Hetzner ClickHouse (required for dev)
make test                           # Run pytest
make lint                           # ruff check + pyright
make format                         # Auto-format code
make migrate                        # Run ClickHouse DDL migrations (via HTTP)
make seed                           # Quick seed: 30 days, 3 tickers (fast dev data)
make types                          # Generate TS types from FastAPI OpenAPI schema
make build                          # Build prod Docker images locally (run when changing Dockerfile, docker-compose.yml, or deps)
make rollback                       # Rollback prod to previous commit and rebuild (emergency use)
make monitor-install                # Install health monitor cron on Hetzner (every 15 min)
make monitor-log                    # Show recent prod health failures
make events-notify-install          # Install docker-events → Discord notifier on Hetzner (QNT-101)
make events-notify-status           # Show notifier systemd status + last heartbeat
make events-notify-test             # Kill litellm to verify the Discord webhook path end-to-end
make issue QNT=34                   # Checkout branch for Linear issue
make pr QNT=34 TITLE="description"  # Push + create PR
```

### Workflow Commands (Claude Code slash commands)

#### Session & Cycle
```
/session-check            # Full context restore: branch + Linear + AC status
/status                   # Quick glance: branch, commits, uncommitted (no API calls)
/cycle-start              # Start of week: show cycle issues, check plan staleness, suggest next pick
/cycle-end                # End of week: summarize shipped, roll over incomplete, prompt retro if milestone done
/retro                    # End of milestone: review, capture lessons, review upcoming phases, sync docs
```

#### Issue Lifecycle
```
/go QNT-34                # Full orchestrator: pick → implement → sanity-check → review → ship
/pick QNT-34              # Start an issue: checkout branch + Linear In Progress + show acceptance criteria
/implement QNT-34         # Implement: read patterns → write code → WIP commits → targeted tests → AC validation
/sanity-check QNT-34      # Pre-PR gate: lint + types + tests + AC (code/dev/prod classification) → In Review
/review QNT-34            # Adversarial code review: logic errors, security, architecture, edge cases
/ship QNT-34              # Ship: squash WIPs → tick project-plan.md → PR → CI → merge → post-deploy verify → Done
/fix QNT-34               # Error recovery: diagnose failure → fix → resume pipeline from failed step
/sync-linear QNT-34       # Manual override: sync issue status when Linear has drifted
```

#### Docs & Scope
```
/change-scope             # Formalise a requirement change (add/drop/modify): spec + Linear + ADR if warranted
/sync-docs                # Reconcile project-plan.md with Linear: tick Done, remove Cancelled, surface gaps
```

#### Ops
```
/server-audit             # Audit Hetzner prod: durability / host / security / drift. Proposes Linear tickets for gaps, files on approval.
```

### Hooks (Automatic — no manual invocation)

Configured in `.claude/settings.json`, scripts in `.claude/hooks/`:

| Hook | Trigger | Effect |
|------|---------|--------|
| session-start | Session begins | Auto-detects branch, injects QNT context, warns on prod health failures |
| auto-format | After Edit/Write | Runs `ruff format` on every Python file edited |
| protect-repo | Before Bash | Blocks force push, push to main, hard reset, rm -rf |
| check-uncommitted | Session ends | Warns about uncommitted work |
