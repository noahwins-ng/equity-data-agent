# Working Docs

Living documentation for the equity-data-agent codebase. This is the shared context for both human developers and AI assistants. When starting a session, read the relevant sections to understand how things work and why they were built that way.

## Project
- [project-requirement.md](project-requirement.md) — Full requirements, architecture, data model, infrastructure, deployment
- [project-plan.md](project-plan.md) — Phase-by-phase delivery checklists, synced with Linear via `/ship` and `/sync-docs`
- [patterns.md](patterns.md) — Established code recipes — read before implementing
- [AC-templates.md](AC-templates.md) — Default acceptance criteria for common PR classes (infra/CI, etc.)

## Structure

### [architecture/](architecture/)
How the system works. Read these to understand the big picture.

- [system-overview.md](architecture/system-overview.md) — High-level data flow, component responsibilities, where code lives
- *(added as components are built)*

### [decisions/](decisions/)
Architecture Decision Records (ADRs). Read these to understand **why** we chose X over Y.

- [001-clickhouse-over-postgres.md](decisions/001-clickhouse-over-postgres.md)
- [002-monorepo-uv-workspaces.md](decisions/002-monorepo-uv-workspaces.md)
- [003-intelligence-vs-math.md](decisions/003-intelligence-vs-math.md)
- [004-batch-ingestion-over-streaming.md](decisions/004-batch-ingestion-over-streaming.md)
- [005-nextjs-vercel-over-python-native-frontend.md](decisions/005-nextjs-vercel-over-python-native-frontend.md)
- [006-multi-timeframe-via-aggregation.md](decisions/006-multi-timeframe-via-aggregation.md)
- [007-minimal-agent-graph-first.md](decisions/007-minimal-agent-graph-first.md)
- [008-no-vercel-ai-sdk.md](decisions/008-no-vercel-ai-sdk.md)
- [009-embedding-via-qdrant-cloud-inference.md](decisions/009-embedding-via-qdrant-cloud-inference.md)
- [010-dagster-production-topology.md](decisions/010-dagster-production-topology.md)
- [011-llm-routing-groq-default-gemini-override.md](decisions/011-llm-routing-groq-default-gemini-override.md)

### [guides/](guides/)
How to do common tasks. Operational runbooks.

- [dev-workflow.md](guides/dev-workflow.md) — Weekly cadence: how commands chain together (Monday kickoff → daily work → Friday close)
- [local-dev-setup.md](guides/local-dev-setup.md) — Getting started from a clean clone
- [hetzner-bootstrap.md](guides/hetzner-bootstrap.md) — One-time production server setup on Hetzner CX41
- [project-setup-playbook.md](guides/project-setup-playbook.md) — Reusable checklist for bootstrapping new projects
- [ops-runbook.md](guides/ops-runbook.md) — Failure-mode catalog: symptoms, diagnosis, response, prevention (check here first when prod breaks)

### [retros/](retros/)
End-of-phase retrospectives. What shipped, what was hard, lessons learned.

- [phase-0-foundation.md](retros/phase-0-foundation.md) — Foundation: monorepo, shared package, Docker, migrations, CI/CD, Hetzner bootstrap

### [api/](api/)
HTTP request files for testing FastAPI endpoints. Open with VS Code REST Client extension.

- [health.http](api/health.http) — Health check
- [reports.http](api/reports.http) — Report endpoints (technical, fundamental, news, summary)
- [data.http](api/data.http) — Data endpoints (OHLCV, indicators — JSON arrays for charts)
- [tickers.http](api/tickers.http) — Ticker registry
- [search.http](api/search.http) — Semantic news search
