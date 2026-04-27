# Working Docs

Living documentation for the equity-data-agent codebase. This is the shared context for both human developers and AI assistants. When starting a session, read the relevant sections to understand how things work and why they were built that way.

## Project
- [project-requirement.md](project-requirement.md) — Full requirements, architecture, data model, infrastructure, deployment
- [project-plan.md](project-plan.md) — Phase-by-phase delivery checklists, synced with Linear via `/ship` and `/sync-docs`
- [patterns.md](patterns.md) — Established code recipes — read before implementing
- [AC-templates.md](AC-templates.md) — Default acceptance criteria for common PR classes (infra/CI, etc.)
- [design-frontend-plan.md](design-frontend-plan.md) — Phase 6 frontend feasibility assessment + scope cuts against the canonical mock
- [model-bench-2026-04.md](model-bench-2026-04.md) — Free-tier LLM bench results: Llama-3.3-70B default, Llama-4-Scout fallback

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
- [012-domain-conventions-in-reports-not-prompts.md](decisions/012-domain-conventions-in-reports-not-prompts.md)
- [013-stay-on-bespoke-compose-not-coolify.md](decisions/013-stay-on-bespoke-compose-not-coolify.md)

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
- [phase-1-data-ingestion.md](retros/phase-1-data-ingestion.md) — Data ingestion: yfinance assets, ClickHouse loaders, sensors, schedules
- [phase-2-calculation-layer.md](retros/phase-2-calculation-layer.md) — Calculation layer: technical indicators, fundamental ratios, multi-timeframe aggregation
- [phase-2-ac-audit.md](retros/phase-2-ac-audit.md) — Mid-phase AC audit after the Apr 16 deploy-green-but-prod-stale outage
- [phase-3-api-layer.md](retros/phase-3-api-layer.md) — API layer: report and data endpoints, asset checks, OpenAPI contract
- [phase-4-narrative-data.md](retros/phase-4-narrative-data.md) — Narrative data: news ingestion, embeddings, Qdrant, semantic search
- [phase-4-asset-check-audit.md](retros/phase-4-asset-check-audit.md) — Asset-check composite-key aggregation audit (QNT-122 follow-up)

### [design/](design/)
Canonical visual references for the Phase 6 frontend (TERMINAL/NINE mock + iteration history).

- [design/README.md](design/README.md) — v1 vs v2 mock provenance + source-of-truth links

### [screenshots/](screenshots/)
Portfolio screenshots embedded in the top-level README.

- [screenshots/README.md](screenshots/README.md) — Capture recipe + re-shoot cadence for the three artifacts

### [api/](api/)
HTTP request files for testing FastAPI endpoints. Open with VS Code REST Client extension.

- [health.http](api/health.http) — Health check
- [reports.http](api/reports.http) — Report endpoints (technical, fundamental, news, summary)
- [data.http](api/data.http) — Data endpoints (OHLCV, indicators — JSON arrays for charts)
- [tickers.http](api/tickers.http) — Ticker registry
- [search.http](api/search.http) — Semantic news search
