# Equity Data Agent - Project Plan

> Implementation narratives for completed items live in [`project-plan-archive.md`](project-plan-archive.md). This file is the scannable checklist; open items keep their full detail inline.

Progress tracking for the phased build-out. Each item maps to one or more Linear issues.
Updated automatically by `/ship` and `/sync-docs`.

---

### Phase 0 - Foundation
**Scope**: Repo scaffolding, infrastructure, and CI/CD.

- [x] Initialize monorepo with uv workspaces (4 packages) - QNT-34
- [x] Set up root `pyproject.toml` with shared dev dependencies (ruff, pyright, pytest)
- [x] Create `shared` package with `Settings`, ticker registry (`TICKERS` list + `TICKER_METADATA` dict with sector/industry per ticker), and base Pydantic schemas - QNT-35
- [x] Write `docker-compose.yml` with dev/prod profiles - QNT-36
- [x] Write `Dockerfile` (multi-stage: base with uv deps → dagster target, api target) - shared by dagster, dagster-daemon, and api services
- [x] Set up ClickHouse with DDL migration scripts (raw + derived databases) - QNT-37
- [x] Configure GitHub Actions for CI (lint + test) and CD (SSH deploy) - QNT-38
- [x] Create `.env.example` with all required environment variables - QNT-39
- [x] Bootstrap Hetzner CX41 production server: provision VPS, install Docker, configure GitHub deploy secrets, first manual deploy - QNT-83
- [x] Integration test infrastructure + prod health visibility - QNT-85
- [x] Claude Code slash command framework (12 commands in `.claude/commands/`) and dev workflow docs (`docs/guides/dev-workflow.md`, this `project-plan.md`) - QNT-84
- [x] Verify: SSH tunnel to ClickHouse works, Dagster UI starts locally, CI pipeline passes

---

### Ops & Reliability
**Scope**: Cross-phase hardening that responds to prod incidents. Each item is reactive - triggered by a specific failure mode rather than a planned Phase deliverable. Lives outside the Phase 0-7 axis because the work cuts across phases.

- [x] CD hard gate: verify prod `git rev-parse HEAD` equals merged commit SHA - QNT-88
- [x] CD hard gate: verify Dagster definitions module loads expected asset / check / schedule counts - QNT-89
- [x] Harden /go pipeline with three-class AC taxonomy (code vs dev-exec vs prod-exec) - QNT-90
- [x] Add `restart: unless-stopped` to prod services in docker-compose.yml - QNT-95
- [x] Alert on pending kernel reboots (health-monitor log + unattended-upgrades mail via Resend SMTP) - QNT-96
- [x] Create ops runbook skeleton with failure-mode catalog - QNT-99
- [x] Harden docker-compose.yml: HEALTHCHECK + log rotation + resource limits - QNT-100
- [x] Alerting pipeline: uptime monitoring + container state notifications - QNT-101
- [x] Autoheal sidecar for unhealthy long-running containers - QNT-104
- [x] Raise dagster-daemon mem_limit 1g → 2g after Apr 20 weekly-fundamentals OOM cascade - QNT-111
- [x] Two-layer deploy-window retry protection for sensor + schedule jobs - QNT-110
- [x] Cap Dagster concurrent runs at 3 with QueuedRunCoordinator to prevent backfill fan-out OOM - QNT-113
- [x] Raise dagster-daemon mem_limit 2g → 3g after QNT-113 sizing math under-estimated per-worker peak - QNT-115
- [x] Enable run_monitoring + backfill tag concurrency in dagster.yaml - QNT-114
- [x] Bind-mount dagster.yaml so repo edits actually reach the running daemon - QNT-112
- [x] Docs: swap uptime monitoring guide from BetterStack to UptimeRobot - QNT-105
- [x] API accepts HEAD on /health endpoints for HEAD-only uptime probes - QNT-106
- [x] Polish docker-events-notify: fix `<no value>` exit-code display + correct restart-policy docs - QNT-107
- [x] Suppress docker-events alerts during CD deploy window - QNT-109
- [x] Encrypt .env at rest with SOPS - QNT-102
- [x] Widen fundamental_summary P/E asset-check band to symmetric [-10000, 10000] - QNT-119
- [x] Resource hygiene: narrow retry-on-Exception in ClickHouse + Qdrant resources - QNT-117
- [x] Audit asset checks for composite-key aggregation correctness - QNT-122
- [x] Migrate Dagster to production topology: code-server split + DockerRunLauncher - QNT-116
- [ ] Lazy-import heavy deps in asset modules to shrink per-subprocess RSS - QNT-118 **[deferred 2026-05-06; revisit-when triggers in ticket body]**
    - **Triggered by**: QNT-116 follow-up (2026-04-22) - every Dagster subprocess (code server, run workers, sensor/schedule evaluators) pays top-level `import pandas / numpy / qdrant-client / yfinance / clickhouse-connect` at startup, even when the subprocess never touches the library. QNT-115's revised per-subprocess peak was ~360 MB; hypothesis is the majority is heavy library imports, and deferring them into function bodies should drop per-subprocess RSS to ~120-150 MB. Scope: `from __future__ import annotations` + function-local imports across every module in `packages/dagster-pipelines/src/dagster_pipelines/`; `TYPE_CHECKING` guards for any Pydantic resource fields that annotate heavy external types. Compounds QNT-116's topology gains (more subprocesses to amortize across) but ships independently so rollback is orthogonal. Measurement AC compares pre/post `docker stats` on `dagster-code-server` under a sensor-tick storm.
    - **Deferred 2026-05-06 (Phase 7 close review)**: post-QNT-116, dagster-code-server steady-state RSS is 351 MB inside a 2 GB cgroup (17% util) and run-workers live in their own ephemeral cgroups - the shared-budget ratchet that motivated this work is gone, so the measurement AC has no current load to validate against. Revisit-when triggers in the Linear body: code-server peak >80% of 2 GB, OR a new asset module pushes peak past 1.5 GB, OR `Backoff.EXPONENTIAL` retries fail because of fresh-subprocess startup latency.
- [x] CD: restart services whose bind-mounted config changed - QNT-124
- [x] Guard dagster.yaml env_vars against Settings drift - QNT-125
- [x] Migrate orphaned `packages/*/tests/` into `tests/` so pytest actually runs them - QNT-127
- [ ] Rotate GROQ_API_KEY + GEMINI_API_KEY leaked in 2026-04-24 session transcript - QNT-126
    - **Triggered by**: 2026-04-24 session transcript review - both API keys echoed in plaintext into a local Claude Code session transcript during the QNT-125 outage debug. Transcripts persist on disk under `~/.claude/projects/` and aren't encrypted at rest. Rotation issues new keys at Groq + Google AI Studio, updates SOPS-encrypted `.env` on prod (per QNT-102), redeploys LiteLLM, and verifies the proxy routes cleanly under both `equity-agent/default` and `equity-agent/gemini` post-rotation.
- [x] Security: scanner suite in CI (pip-audit + npm audit + bandit + gitleaks + trivy weekly) - QNT-160
- [x] Auto-apply ClickHouse migrations on every deploy - QNT-146
- [x] Vercel ISR Writes overrun: 60s revalidate vs EOD data cadence - QNT-166
- [x] Disk reclaim + DockerRunLauncher auto_remove (1080 stopped containers, 53 GB build cache, /dev/sda1 at 85%) - QNT-167
- [x] Switch frontend from ISR to SSG + Vercel Deploy Hook - QNT-168
- [x] Forward VERCEL_DEPLOY_HOOK_URL to Dagster run-worker containers - QNT-171
- [ ] Adopt a state-tracking migration tool for ClickHouse - QNT-147
    - **Triggered by**: QNT-146 (PR #157) closed the manual-gate bug class by auto-applying `migrations/*.sql` on every deploy via curl + `IF NOT EXISTS`, but two convention-only safeguards remain that a single distracted PR can break: (1) editing a committed migration silently drifts dev vs prod (prod no-ops via IF NOT EXISTS, fresh dev runs the edited version); (2) a non-idempotent migration (DML, RENAME, complex ALTER) corrupts data on every deploy. A state-tracking tool (`golang-migrate` / `dbmate` / Atlas) catches both mechanically via `schema_migrations` table + content checksums. Adopt when ANY trigger fires: first DML migration, first non-additive ALTER, need to edit a committed migration, second ClickHouse instance, or 50+ migration files. Until then QNT-146's idempotent-by-convention is good enough.
- [x] CD: serialize prod deploys + namespace temp-file to fix parallel-deploy race - QNT-170
- [x] Tame ClickHouse system-log creep (TTL on `text_log` / `trace_log` / `metric_log` / `asynchronous_metric_log`) - QNT-169
- [x] obs-smoke: pre-prod assertion that every Prometheus target / Grafana panel / alert rule has data - QNT-172
- [x] Per-ticker news relevance filter at ingest - QNT-173
- [x] TTM balance-sheet ratios + frontend ebitda/roe fix (QNT-179 round 2)
- [x] Drop empty fundamental periods at ingestion instead of zero-filling - QNT-179
- [x] Migrate Langfuse instrumentation to LangGraph CallbackHandler + add sample_rate - QNT-181
- [x] Calibrate Quarterly fundamentals tab to use TTM for P/E, ROE, ROA, FCF yield - QNT-180
- [x] Replace ephemeral cloudflared quick-tunnel with named Cloudflare tunnel - QNT-177
- [x] Right-size grafana mem_limit 256m → 384m (steady-state 87% trips ContainerMemoryHigh) - QNT-306

---

### Phase 1 - Data Ingestion
**Scope**: Dagster assets that fetch and store OHLCV + fundamental data. Batch-only - no streaming.

**Ingestion Strategy**:
- **Partitioning**: `StaticPartitionsDefinition` by ticker (10 partitions). Per-ticker visibility, retry, and parallel execution in Dagster UI. **Max 3 concurrent partitions** to avoid yfinance rate limiting during backfill (configured via Dagster `TagConcurrencyLimit`).
- **Backfill**: One-time materialization of all partitions with `period="2y"` (2 years of history). Enough for all technical indicators and YoY comparisons. Triggered via manual materialization in Dagster UI with `period="2y"` asset config.
- **Incremental (daily OHLCV)**: Fetch last 5 trading days per ticker, `ReplacingMergeTree` deduplicates. No need to track "last fetched date." The daily schedule hardcodes `period="5d"` via `RunConfig`.
- **Incremental (weekly fundamentals)**: Fetch all available quarters (yfinance returns last 4 quarterly + 4 annual), `ReplacingMergeTree` deduplicates.
- **Schedule**: Daily OHLCV at ~5-6 PM ET (after market close, data settles). Weekly fundamentals on weekends.

**Deliverables**:
- [x] Implement `ohlcv_raw` Dagster asset (yfinance → ClickHouse) - QNT-41
- [x] Implement `fundamentals` Dagster asset (yfinance → ClickHouse) - QNT-42
- [x] Add Dagster schedules: daily for OHLCV (~5-6 PM ET), weekly for fundamentals - QNT-43
- [x] Implement Dagster resource for ClickHouse client (shared across assets) - QNT-40
- [x] Verify: Run backfill for all 10 tickers, confirm data in ClickHouse, check Dagster lineage graph - verified 2026-04-19 in prod: `ohlcv_raw` has 504 rows/ticker (2024-04-15 → 2026-04-17) for all 10 tickers; `fundamentals` has 9-11 quarters/ticker; derived tables populated (weekly=1040, monthly=240, tech_daily=5040, fund_summary=101); Dagster asset graph loads 8 assets + 17 checks + 2 schedules + 2 sensors

---

### Phase 2 - Calculation Layer
**Scope**: Technical indicators, fundamental ratio computation, and multi-timeframe aggregation.

- [x] Implement `ohlcv_weekly` and `ohlcv_monthly` Dagster aggregation assets - QNT-70
- [x] `ohlcv_weekly`:
- [x] `ohlcv_monthly`:
- [x] Implement `technical_indicators` Dagster assets (daily, weekly, monthly) - QNT-44
- [x] Implement `fundamental_summary` Dagster asset (15 ratios) - QNT-45
- [x] Add Dagster sensors to trigger downstream recomputation when raw data refreshes - QNT-46
- [x] Add Dagster asset checks for data quality validation - QNT-68
- [x] Null out P/E when EPS is near zero (|EPS| < $0.10) to honor N/M convention - QNT-87
- [x] Fix P/E to use TTM earnings on quarterly rows in fundamental_summary - QNT-91
- [x] Set `default_status=RUNNING` on all sensors and schedules - QNT-92
- [x] Validation tests: indicators vs external sources - QNT-47
- [x] Verify: Run full pipeline Raw → Aggregation → Indicators, spot-check calculations against external sources (e.g., TradingView) - covered by QNT-47 (canonical Wilder/Appel cross-reference tests) + QNT-68 asset checks + `docs/retros/phase-2-ac-audit.md`

---

### Phase 3 - API Layer
**Scope**: FastAPI endpoints serving machine-readable data (frontend charts) and human-readable reports (agent).
**Dependencies**: Requires Phase 2 (data must exist in ClickHouse). Can proceed in parallel with Phase 4 - news endpoints gracefully degrade to empty responses until Phase 4 populates `news_raw`.

**Report template - build this FIRST (QNT-69):**
- [x] Design **one** report template end-to-end against real ClickHouse data - QNT-69 **[start of Phase 3]**

**Report endpoints (text - for the agent; all apply the QNT-69 template pattern):**
- [x] `GET /api/v1/reports/technical/{ticker}` - formatted text report with indicator context - QNT-48 *(first concrete output of QNT-69)*
- [x] `GET /api/v1/reports/fundamental/{ticker}` - formatted text report with ratio context - QNT-49
- [x] `GET /api/v1/reports/news/{ticker}` - recent news summary. Depends on Phase 4 `news_raw` data - returns 200 with a well-formed text report containing an `N/M (no news ingested…)` block until Phase 4 populates data. Sentiment narrative lands when QNT-55 (Qdrant search) ships. - QNT-79
- [x] `GET /api/v1/reports/summary/{ticker}` - combined text overview: latest price context, RSI interpretation, trend narrative, and sector context. Sector context derived from a static mapping in `shared/tickers.py`. Used by the agent as a quick "at a glance" tool. - QNT-50

**Data endpoints (JSON - for the frontend):**
- [x] `GET /api/v1/ohlcv/{ticker}?timeframe=daily|weekly|monthly` - returns `[{time, open, high, low, close, adj_close, volume}]` for TradingView chart rendering. `time` is an ISO date string `"YYYY-MM-DD"` - QNT-76
- [x] `GET /api/v1/indicators/{ticker}?timeframe=daily|weekly|monthly` - returns `[{time, rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, ema_12, ema_26, bb_upper, bb_middle, bb_lower}]` as row-oriented time-series (`null` during indicator warm-up period) - QNT-77
- [x] `GET /api/v1/fundamentals/{ticker}` - latest fundamental ratios as structured JSON for the ticker detail page ratios table - QNT-80
- [x] `GET /api/v1/dashboard/summary` - returns `[{ticker, price, daily_change_pct, rsi_14, rsi_signal, trend_status}]` for ALL tickers in a single response. Avoids N+1 requests on dashboard load. - QNT-81

**Utility endpoints:**
- [x] `GET /api/v1/tickers` - returns the ticker list from `shared.tickers.TICKERS` - QNT-78
- [x] `GET /api/v1/health` - health check with ClickHouse + Qdrant connectivity status + deploy identity (git SHA, Dagster asset/check counts) - QNT-51

**Cross-cutting:**
- [x] CORS middleware configured (allow production domain, `*.vercel.app` for preview deploys, and `localhost:3001` for dev) - `packages/api/src/api/main.py:131-137`
- [x] Ticker validation: all `{ticker}` path endpoints AND the `POST /agent/chat` request body validate the ticker against `shared.tickers.TICKERS` and return `404 {"detail": "Ticker not found"}` for unknown tickers - enforced in `packages/api/src/api/routers/data.py` and all report template formatters
- [x] No API authentication in initial scope - the API is read-only and serves public market data
- [x] Verify: Hit all endpoints with VS Code REST Client (`.http` files), confirm chart data arrays are correctly structured, check OpenAPI docs at `/docs` - verified 2026-04-19 via prod `curl` pass: 10 OpenAPI paths, correct row counts (504 daily / 104 weekly / 24 monthly / 10 fundamental / 10 dashboard), all `{ticker}` endpoints 404 on BOGUS, `/docs` 200

---

### Phase 4 - Narrative Data
**Scope**: News ingestion, embedding, and semantic search via Qdrant.

- [x] Ingest news via **RSS + `feedparser`** - QNT-52
- [x] Implement `news_raw` Dagster asset (RSS feeds → `equity_raw.news_raw` in ClickHouse) - QNT-53
- [x] Create Qdrant `equity_news` collection (384-dim Float32, cosine distance) - auto-create in the Qdrant Dagster resource on first use, or via a setup script (shipped as part of QNT-54 - `QdrantResource.ensure_collection` runs at asset-start)
- [x] Implement `news_embeddings` Dagster asset (`news_raw` → Qdrant Cloud) - QNT-54
- [x] `GET /api/v1/search/news?ticker=NVDA&query=earnings` - QNT-55
- [x] Add Dagster asset checks for `news_raw` and `news_embeddings` data quality - QNT-93
- [x] Namespace Qdrant point IDs by ticker - QNT-120
- [ ] Verify: Search for recent news about a ticker, confirm relevance ranking

---

### Phase 5 - Agent Layer
**Scope**: LangGraph agent with tools that call FastAPI endpoints.

- [x] Configure LiteLLM proxy via `litellm_config.yaml` - QNT-59
- [x] Demote Gemini override from Pro to Flash - QNT-123
- [x] Integrate Langfuse tracing - QNT-61 **[day-one of Phase 5, moved from Phase 7]**
- [x] Define LangGraph state schema (ticker under analysis, gathered reports, thesis draft) - QNT-56
- [x] Implement tools - QNT-57
- [x] Build agent graph - **3 nodes: plan → gather → synthesize** (per ADR-007) - QNT-56
- [x] System prompt enforcing the "interpret, don't calculate" mandate - QNT-58
- [x] Agent CLI: `python -m agent analyze NVDA` - run single-ticker analysis from terminal - QNT-60
- [x] Surface canonical fundamental thresholds in the fundamental-report template (parameterised QNT-136) - QNT-137
- [x] Surface canonical TA thresholds in the technical-report template so the agent quotes them verbatim - QNT-136
- [x] Restructure thesis output to Setup / Bull Case / Bear Case / Verdict - QNT-133
- [x] Agent evaluation framework - QNT-67 **[highest-priority Phase 5 item - the single biggest AI-Engineer hiring signal]**
- [x] Investigate + fix the 3 hallucination findings the QNT-67 baseline flagged - QNT-128
- [x] Re-run Llama-3.3-70B bench on a fresh TPD bucket - QNT-138
- [x] Bench free-tier candidate models against QNT-67 goldens (portfolio artifact) - QNT-129
- [x] `POST /api/v1/agent/chat` SSE endpoint for frontend chat page - QNT-56 **[shipped via QNT-74 in Phase 6]**
- [x] Portfolio README - QNT-66 **[moved from Phase 7 - front-page recruiter artifact]**
- [x] Portfolio screenshots committed + recruiter-framing of README + docs/ sync - QNT-139
- [ ] Verify: Run agent on 2-3 tickers, review thesis quality, confirm zero hallucinated calculations in Langfuse traces; hallucination eval passes on all golden-set questions; README renders correctly on GitHub with all screenshots

---

### Phase 6 - Frontend
**Scope**: Next.js terminal-style dashboard (TERMINAL/NINE design v2), ticker detail with TradingView charts, persistent agent chat panel. Deployed on Vercel.
**Dependencies**: Requires Phase 3. Requires Phase 5 for agent chat panel (depends on QNT-133 structured thesis output). News card gracefully degrades if Phase 4 not complete.
**Design reference**: `docs/design-frontend-plan.md` is the canonical UI spec - locks the three-pane layout (watchlist / detail / analyst), the elements each pane owns, the EOD framing (no live-tick chrome), and the field substitutions (EBITDA margin for op margin, ROE/ROA for ROIC, Quarterly/Annual/TTM tabs, no FWD).

- [x] Phase 6 frontend design assessment + canonical mocks - QNT-135 **[lands BEFORE any architecture or page work]**
- [x] ADR-014: Next.js rendering mode per page (SSG / SSR / CSR) + cache strategy - QNT-121 **[written BEFORE any page code]**
- [x] ADR-015: News-source + sentiment topology - QNT-140 **[written BEFORE QNT-72/73 consumer pages; gates QNT-131 / QNT-132]**
- [x] Initialize Next.js app in `frontend/` with Tailwind CSS - QNT-71
- [x] **Phase 6 backend support: indicators + fundamentals + sparkline + SPY** - QNT-134 **[gates QNT-72 + QNT-73]**
- [x] **News ingest migration: Yahoo RSS → Finnhub `/company-news`** - QNT-141 **[gates QNT-72 + QNT-73 news cards; per ADR-015]**
- [x] **Bound `news_embeddings` to recent + delta-only upsert - protect Qdrant free tier post-QNT-141** - QNT-142 **[durable fix for the QNT-141 backfill blast radius; calibration multiplier surfaced via `feedback_full_refresh_multiplier.md`]**
- [x] **Shift `news_raw_schedule` from 4h cadence to daily (02:00 ET)** - QNT-143 **[aligns ingest cadence with design v2 EOD framing; cleans the only sub-daily schedule in the project]**
- [x] **`dagster.yaml` env_vars drifted from Settings post-QNT-141 - bidirectional CI guard** - QNT-144 **[durable fix for the silent run-drop discovered while verifying QNT-143's first manual tick]**
- [x] **Qdrant GC: delete `news_embeddings` points whose `published_at` aged past the 7-day window** - QNT-145 **[converges Qdrant on ADR-009's literal rolling-7d definition; closes the QNT-142 out-of-scope advisory before it could trip the orphan check]**
- [x] **News pill: resolve true outlet beyond Finnhub redirect labels** - QNT-148 **[corrects ~78% of weekly news pills mislabelled "Yahoo"/"Benzinga"/"CNBC" by Finnhub redirect; ADR-016 commits the cross-ticker storage + dedup contract]**
- [x] **Ticker detail: responsive labels at narrow viewports + period-aware P/E + simplified quote header** - QNT-153 **[third polish round on top of QNT-151/QNT-152; closed the 14" MacBook layout gap and surfaced one data-quality bug (QNT-154 filed)]**
- [x] **Price chart: 1M/3M/6M/YTD buttons zoom (not filter), older candles stay accessible** - QNT-155 **[surfaced 2026-05-01 during QNT-153 testing; TradingView-convention fix]**
- [x] **Price chart: per-pane legends so sub-panes are self-identifying** - QNT-157 **[surfaced 2026-05-01 alongside QNT-155 testing; TradingView-convention follow-on]**
- [x] **Price chart polish round (post-QNT-157 user-test bundle)** - QNT-158 **[surfaced 2026-05-01 during QNT-157 user-testing; one-PR bundle of five chart-correctness fixes]**
- [x] **Watchlist polish: terminal title block + per-ticker logos + middle-pane logo + data: URL inlining** - QNT-162 **[surfaced 2026-05-02 cosmetic + perf round on the watchlist column]**
- [x] **Logos broken in prod: Finnhub now returns static2.finnhub.io, SSRF allowlist only permits static.finnhub.io** - QNT-163 **[surfaced 2026-05-02 from QNT-161 dev API logs spam - every ticker logged "host mismatch"]**
- [x] **Watchlist (left pane) + landing page** (`/`) - QNT-72
- [x] **Ticker detail (middle pane)** (`/ticker/[symbol]`) - QNT-73
- [x] **Analyst chat (right pane)** - QNT-74
- [x] **Agent chat SSE: production-hardening follow-up (disconnect cleanup, LLM timeout, error sanitization)** - QNT-150 **[QNT-74 adversarial-review punch list - six advisories that didn't block ship, bundled because they share an operational concern: the SSE path can hang or leak under failure modes the happy-path tests don't exercise]**
- [x] **Agent chat SSE: emit intent event from classify_node so streaming label is correct from frame 0** - QNT-159 **[QNT-156 follow-up surfaced during user testing the same day; one-line user-visible bug, two-line root-cause]**
- [x] **Agent: extend intent set with comparison + conversational + domain-redirect fallback** - QNT-156 **[follow-on to QNT-149; closes the two visible gaps the two-intent baseline left]**
- [x] Adapt agent response shape to question intent (skip forced thesis on quick-fact prompts) - QNT-149
- [ ] **Backend support tickets that gate the design rendering**:
    - QNT-140 - ✓ **Done.** News-source + sentiment topology ADR (ADR-015) committed Finnhub `/company-news` + topology (a) async classifier asset.
    - QNT-141 - News ingest migration (Yahoo RSS → Finnhub `/company-news`); lands the schema columns (`publisher_name`, `image_url`, `sentiment_label`) that QNT-72/73 news cards consume. Per ADR-015.
    - QNT-131 - **Deferred 2026-04-28 to Backlog.** Classifier scoped to FinBERT (local CPU inference, no LLM coupling) when revisited. News cards in v1 ship without the `pend` chip. Trigger: UX evidence the chip is load-bearing, or thesis-eval signal that per-article sentiment moves the needle. See ADR-015 §"Revision history" (post-QNT-141, alongside QNT-142). The `sentiment_label` column from QNT-141 stays in place defaulted to `'pending'` for the future revisit.
    - QNT-132 - ✓ **Done 2026-04-28.** `/api/v1/health` provenance block (gates the bottom strip on QNT-73). **Trimmed 2026-04-28** to `sources` + `jobs.next_ingest_local` only. SENTIMENT/AGENT rows dropped - sentiment is `null` post-QNT-131 deferral; agent runtime is a constant. The remaining 2-line strip still carries the "no hardcoded UI strings" architecture signal on the values that actually flip (vendor swap, schedule shift). Suffix derived from the schedule's `execution_timezone` (single source of truth) - change the schedule, the strip flips.
    - QNT-133 - agent thesis output restructured to Setup / Bull Case / Bear Case / Verdict (gates the thesis card render in QNT-74) - *Phase 5 milestone*
- [x] ~~Generate TypeScript types from FastAPI's `/openapi.json` via `openapi-typescript` (`make types`)~~ - **Re-scoped to hand-maintained contract 2026-07-18 (QNT-384).** Codegen was never viable: the data endpoints return raw `list[dict[str, Any]]` (no `response_model`), so the OpenAPI schema carries almost none of the shapes `lib/api.ts` reads, and the dominant surface - the SSE streaming chat contract (`RetrievedSource`, `DoneEvent`, ...) - is not expressible in OpenAPI at all. `make types` and the "do not handwrite" rule are removed; `lib/api.ts` is the deliberately hand-maintained contract, documented as such at the drift seam. Real codegen would first require adding response models to `data.py` - its own ticket if ever wanted.
- [x] **Public-chat abuse prevention: rate limit + per-IP cost cap + auth model + CORS lockdown** - QNT-161 **[gates QNT-75; landed before the public deploy because the Hetzner chat endpoint becomes an availability-bomb the moment the URL leaks]**
- [x] **Deploy frontend to Vercel** - QNT-75 **[Phase 6 deploy ticket; ADR-018 commits the project to a free Cloudflare quick tunnel over a paid named-tunnel domain]**
- [x] **Cross-cutting**: Ticker list is sourced from `GET /api/v1/tickers` across every page (watchlist, detail-page switcher, chat-panel selector) - never hardcoded. Inherits QNT-78's ⏳ PENDING AC; hardcoding the list anywhere defeats the endpoint's purpose. Phase 3 lesson. **Verified 2026-05-02 via prod walkthrough**: watchlist server-fetches from `/api/v1/tickers`; chat-panel selector pulls the same list; no hardcoded ticker arrays in `frontend/src/`.
- [x] Verify: Watchlist renders 10 tickers + SPY with sparklines; ticker detail shows quote header / chart / technicals / fundamentals / news / provenance strip with all data; chat panel streams tool calls in real time and renders structured thesis; provenance strip values flip when a config value changes (QNT-132 verification). **Verified 2026-05-02 in prod** via https://equity-data-agent-ynr2.vercel.app - all surfaces render with live data; CORS preflight returns the Vercel origin; SSE chat streams tool calls + structured thesis end-to-end.

---

### Phase 7 - Observability & Polish
**Scope**: Tracing, alerting, and production hardening.

- [x] Observability stack: Dozzle logs UI + Prometheus/Grafana/cAdvisor metrics - QNT-103 **[unified log/metrics dashboards on top of the per-IP rate-limit and breaker-trip alerting that QNT-161 already wired in Phase 6; QNT-86 (Sentry) lands first - see Phase 6 retro phase-review notes for the dependency ordering]**
- [x] Integrate Sentry for FastAPI error tracking (`sentry-sdk[fastapi]`, uses `SENTRY_DSN` from `.env`) - QNT-86
- [x] Add Dagster alerting on asset materialization failures - QNT-62
- [x] Implement retry logic for flaky external API calls (yfinance, news APIs) - QNT-63
- [x] Load test FastAPI endpoints (confirm response times under 10 tickers) - QNT-65
- [x] Write integration tests for critical paths (ingestion → calculation → report → agent) - QNT-64
- [ ] Verify: End-to-end run on all 10 tickers, review Langfuse dashboard, confirm no orphaned errors in Sentry

---

### Post v1 Improvements
**Scope**: Targeted upgrades to the v1 surfaces (agent grounding, report quality) once the core stack ships and feedback starts to land. Each item is incremental - no new phases, no new infra.

- [x] Resolve company names to tickers in the chat parser (not just symbols) - QNT-257
- [x] Eliminate duplicate frontend mounts and redundant ticker-page fetches - QNT-246
- [x] Non-data states: landing market overview + chat empty-state + loading skeletons - QNT-250
- [x] Recalibrate responsive breakpoints so technicals/fundamentals cards stop clipping - QNT-248
- [x] Mobile polish sweep: footer wrap, chart label clip, touch targets, reduced-motion scroll - QNT-249
- [x] Chat streaming a11y live region + memoize RunBlock for token-level render perf - QNT-247
- [x] Correctness cleanup: SSG relative news dates + tool_result binding - QNT-252
- [x] Accessibility & semantics baseline: labels, color-scheme, skip-link, tab roles, focus ring - QNT-251
- [x] Split chat-panel.tsx into card components + shared TabStrip - QNT-253
- [x] Mobile chat history lost when closing the chat panel - QNT-256
- [x] Push deterministic eval scores onto prod Langfuse traces - QNT-182
- [x] Enrich news report + add company knowledge tool - QNT-175

- [x] Focused-analysis intents + composer cleanup - QNT-176

- [x] Chat empty-state suggested questions + header cleanup - QNT-178

- [x] Regime-polarity rule for Bull/Bear bullets + comparison-shape mirror - QNT-183

- [x] Strengthen anti-SIGNAL rule in FOCUSED with paired counter-example - QNT-184

- [x] Prior-session momentum-delta rule for Bull/Bear and focused-summary bullets - QNT-185

- [x] Heuristic-token expansion for intent classifier (natural-language focused triggers) - QNT-186
- [x] Verdict-action direction post-check (first deterministic tripwire) - QNT-193
- [x] Verdict-action trigger-shape rule + post-check extension - QNT-194
- [x] RSI momentum polarity misclassification fix - QNT-198
- [x] Structured-output retry on synthesize nodes (measure-first, then with_retry) - QNT-196
- [x] Financial-advice disclaimer footer at every .to_markdown() - QNT-195
- [x] Propagate `_prompt_version()` hash to Langfuse trace metadata - QNT-187
- [x] Migrate prompt versioning to Langfuse Prompt Management (git → Langfuse push on deploy) - QNT-199
- [x] classifier_source dimension on intent/model trace tags - QNT-189
- [x] Golden-set expansion: tripwire fixtures + intent-breadth records - QNT-190
- [x] Per-axis judge with analyst_logic domain axis - QNT-191
- [x] Weekly online eval loop - sample prod traces via existing judge.py - QNT-192
- [x] Responsive frontend layout + neutral confidence bar color - QNT-200
- [x] Responsive layout rework - fluid panels + tablet tier - QNT-201
- [x] Valuation context + freshness in fundamental report - QNT-204
- [x] Synthesis voice - stance, setup template, focused-path rewrite - QNT-205
- [x] Signal verdict v2 - weighted vote + value-trap / growth-at-a-price labels - QNT-206
- [x] Reports v2 - D/W/M technical + Q/A/TTM fundamental + drop news sentiment + company CONTEXT NOW - QNT-207
- [x] Thesis v2 -- per-aspect framing + Overweight/Neutral/Underweight verdict + focused/comparison reshape -- QNT-208
- [x] Analyst voice ADR + persona prompt refresh across synthesis -- QNT-210
- [x] Dynamic thesis plan -- LLM-chosen tools with rationale -- QNT-213
- [x] Conversation message history in graph context - QNT-216
- [x] Judge-model dialogue eval harness for analyst-likeness and exploration quality - QNT-214
- [x] Determinism + variance-aware gating for the dialogue eval harness - QNT-218
- [x] Exploration supervisor route for broad iterative dialogue - QNT-215
- [x] Capture narrate streaming tokens + scriptable per-node token/latency baseline - QNT-219
- [x] Hot-path efficiency: compact reports + per-node model tiering + deterministic exploration - QNT-220
- [x] Runtime numeric grounding guard + answer-groundedness confidence - QNT-221
- [x] Wire semantic news-search tool (RAG) into the graph -- QNT-222
- [x] N-way comparison (3-4 tickers) via lean metrics payload -- QNT-224
- [x] Synthesis model strategy + tail/provider routing (ADR-021) -- QNT-223
- [x] Correctness sweep: question-ticker rebase, focused probe-close, and narrate disclaimer strip - QNT-228
- [x] Eval + observability integrity: judge vs reference, pinned judge, prompt_version coverage, fallback-fire visibility - QNT-230
- [x] Streaming smoothness: synthesize progress animation + early card emit + one prose surface per turn - QNT-229
- [x] Turn-cost trims: quick_fact single-LLM-call + per-intent history budget - QNT-232
- [x] Agent routing polish: comparison URL/thread context, single stable exploration intent label, heuristic consolidation, and timeout partials - QNT-233
- [x] Comparison latency measurement + eval reliability under provider pressure - QNT-234
- [x] RAG golden set: needs_news_search firing + retrieval-relevance tripwire - QNT-231
- [x] Capability suggestions must be concrete answerable prompts - QNT-244
- [x] Per-conversation chat thread: ticker-agnostic thread_id so cross-ticker navigation does not fragment memory - QNT-245
- [x] Warm-thread conversational fallback (no cold-start card mid-thread) - QNT-217
- [x] Conditional flow control -- clarify node + short-circuit edges from classify -- QNT-212
- [x] Streaming narrative wrapper -- narrate node + narrative_chunk SSE + prose bubble -- QNT-211
- [x] Session memory (SQLite checkpointer) + followup intent + thread_id boundary -- QNT-209

---

### Post v1 - DE Enhancement
**Scope**: Data-engineering hardening surfaced by the `docs/de-improvement-v1.md` audit - corporate-action correctness, registry enforcement, and the ticker-universe swap. Each item is incremental; no new phases or infra.

- [x] Ticker lifecycle foundation: add/remove guide + registry assert - QNT-236

- [x] Ticker universe swap: remove V/JPM/UNH, add MU/AMD/INTC - QNT-237

- [x] Corporate-action OHLCV refresh: prevent split/dividend history corruption - QNT-235

- [x] Warehouse hardening: migration state tracking + schema comments - QNT-238

- [x] Test integrity: real-engine coverage for comparison-metrics + CI skip-floor guard - QNT-239

- [x] Data observability dashboard + distribution anomaly checks - QNT-240
- [x] README DE positioning: medallion/dbt-equivalent vocabulary + scaling limits - QNT-241
- [x] dagster-dbt SPIKE: decision to decline dbt adoption at current scale - QNT-242
- [x] Reject/quarantine handling: persist + count dropped source rows - QNT-243
- [x] Clean-window golden + dialogue eval re-run for the new ticker set - QNT-255
- [x] Source-boundary data contracts: schema enforcement + two-tier drift policy - QNT-259
- [x] EDGAR 8-K earnings-release corpus: ingestion + embeddings (2nd RAG corpus) - QNT-260
- [x] RAG retrieval eval: labeled relevance set + recall@k/MRR/nDCG baseline + deterministic CI gate - QNT-261
- [x] RAG corpus snapshot export: producer side of the AWS (Track-3) re-platform seam - QNT-265
- [x] Hybrid retrieval (dense + BM25 RRF) + Cohere reranking - QNT-262
- [x] Multi-corpus retrieval routing (news vs 8-K earnings) - QNT-263
- [x] DeepEval LLM-judged generation eval (RAGAS set + custom G-Eval), nightly/on-dispatch - QNT-264
- [x] Recall-appropriate DeepEval goldens + re-derived floors + enforcement ON - QNT-275
- [x] Contextual retrieval: index-time LLM chunk-context enrichment (measured) - QNT-273
- [x] RAG impact eval: assert retrieved evidence reaches the answer - QNT-277
- [x] Foreground retrieved RAG evidence in synthesis (news + earnings) - QNT-276
- [x] Semantic search-trigger flag in the classifier (replace brittle keyword gates) - QNT-280
- [x] Rerank-score floor on the hybrid retrieval path (drop weak-relevance hits) - QNT-279
- [x] Make the rag_impact eval trustworthy: determinism + live end-to-end golden-query smoke set - QNT-278
- [x] Citation counter reports 0 on the narrative-only path despite retrieved sources - QNT-281
- [x] Restructure narrate as a BLUF analyst voice (bold call + synthesis prose + optional watch) - QNT-285

- [x] Frontend aesthetic pass: default-state void, inline-citation noise, palette tensions - QNT-286

- [x] De-duplicate consecutive source citations across all intents (keep the chip, drop the repeats) - QNT-287

- [x] Restore a subtle boxed pill for inline citations (borderless reads as noise) - QNT-295

- [x] Contextual RAG query rewriting: self-contained retrieval query from the classify call - QNT-289

- [x] Let followup turns fire RAG retrieval: close the warm-thread recall gap - QNT-290

- [x] Declarative per-intent routing policy table - consolidate scattered intent frozensets - QNT-288

- [x] graph.py code health - node module split + discriminated-union answer payload - QNT-294

- [x] Pay down the QNT-294 compat surface - retire legacy answer slots + record the monkeypatch decision - QNT-307

- [x] Harden the answer-projection contract - unify payload-pick precedence + return-dict guardrail - QNT-309

- [x] Unified tool registry contract - end bespoke RAG side channels per new corpus - QNT-291

- [x] Runtime numeric grounding for quick_fact turns - close the narrate-skip gap - QNT-296

- [x] Scale-aware numeric grounding: stop $5M passing as supported by $5B - QNT-297

- [x] Chat flow continuity: follow-up chips after analytical cards + plan-rationale status line - QNT-298

- [x] Warm-thread trust affordances: context-anchor chip + degraded-tool note + data as-of date - QNT-299

- [x] Output-contract hardening: verdict-vs-labels tripwire + normalizing enum validators - QNT-302

- [x] Claim-anchored retrieved-source citations + tiered citation UX - QNT-301

- [x] Senior-analyst voice v6: live-sample assessment across post-ADR-020 intents + supported rules - QNT-303

- [x] Citation-anchor integrity: drop hallucinated out-of-range retrieved-source ids - QNT-305

- [x] Turn hygiene: cancelled-turn checkpoint test + parallel report gather - QNT-300

- [x] Gather node: parallel cross-ticker comparison gather + followup early retrieved_sources emit (v3 G-3/G-9) - QNT-321

- [x] Paid inference primary for public launch: DeepSeek V4 Flash via OpenRouter + global breaker recalibration - QNT-258

- [x] ADR: paid synthesis economics + the free-tier simplification dividend - QNT-292

- [x] Re-anchor + retire the Groq free-tier fallback chain before the decommission - QNT-317

- [x] Revisit prompt caching on the paid synthesize call now the free-tier TPM wall is gone - QNT-318

- [x] Smoke each pinned OpenRouter provider serves the structured output (reasoning-off) - QNT-319

- [x] Tripwires: LiteLLM alias-map sync test + comparison RAG demand detector + small-alias liveness smoke (v3 G-13/G-14/G-15) - QNT-326
- [x] Graph flow: routing + narrate-substrate decisions written to state once (v3 G-1/G-2) - QNT-320
- [x] Graph flow: classify owns the turn boundary - explicit scratch-vs-durable state reset (v3 G-4) - QNT-323
- [x] Retrieval query is first-class on every classify path (v3 G-10/G-11) - QNT-322
- [x] Followup substrate: prior_answer generalized beyond Thesis to any analytical card (v3 G-5) - QNT-324
- [x] History-aware comparison ticker resolution (v3 G-12) - QNT-325
- [x] Spike: fold thesis plan pick into the classify call - thesis 4→3 LLM calls (v3 G-6) - QNT-327
- [x] Turn-boundary reset: interludes and followup preserve thread substrate (v3 R-1/R-2) - QNT-349
- [x] Prompt-layer hygiene: comparison disclosure alias miss + hardcoded ticker list + narrate prior-card label (v3 P-1/P-2/P-3) - QNT-350
- [x] Agent latency: deepseek-first provider pin, strict json_schema enforcement, synthesize verbosity cap - QNT-351
- [x] Report confidence honesty: wire the freshness factor + surface fetch failures in the prompt - QNT-355
- [x] Fundamental report: absolute SCALE block + margin-bps trajectory lines - QNT-354
- [x] Forward calendar: next-earnings-date ingest + report line for dated catalysts - QNT-357
- [x] Technical report: surface shipped indicators + volume + 52-week range + window returns + trend consensus - QNT-353
- [x] News digest quality: 280-char snippet-budget parity + same-story dedup - QNT-356
- [x] Comparison focus-from-axis: narrow the plan by named axis, render only gathered aspects, two-ticker output budget - QNT-358
- [x] Report labels leak into agent prose + fabricated peer-delta reduces grounding - QNT-359
- [x] Grounding false-miss on percentage precision: 1dp report percentages + trailing-zero canonicalisation - QNT-361
- [x] Thesis synthesize output budget: per-call 2500 ceiling after QNT-353/354 report growth outgrew the thesis-calibrated 1500 cap - QNT-370
- [x] Chat composer deadlock: settle aborted/EOF-without-done run status, extract the startRun event-reducer under node:test, disable auto-send chips mid-stream (2026-07-17 whole-project review, finding 1) - QNT-379
- [x] Chat abuse controls keyed on spoofable XFF: key the per-IP rate limit + token budget on CF-Connecting-IP (right-most XFF fallback), drop the stale Caddy/ufw trust-model claims for cloudflared ingress (2026-07-17 whole-project review, finding 2) - QNT-380
- [x] Frontend CI gate: add lint/typecheck/test steps to ci.yml's frontend job (was audit-only, so 939 lines of frontend unit tests were a local honor-system gate) + bump CI node 20->24 so `node --test` can run the `.test.ts` files. Surfaced + fixed a latent regression: PR #542's eslint 9->10 bump broke `next lint` - eslint-plugin-react's React-version auto-detection calls `context.getFilename()`, removed in ESLint 10, crashing every run ("react/display-name: contextOrFilename.getFilename is not a function"). Went unnoticed because lint never ran in CI and local node_modules was stale. Fix: pin `settings.react.version` in eslint.config.mjs to short-circuit detection (keeps the eslint 10 bump, no package.json/lockfile churn). Also re-scope the never-implemented `make types` claim to an explicitly hand-maintained `lib/api.ts` contract - codegen isn't viable (data endpoints return raw dict, SSE contract not in OpenAPI) (2026-07-17 whole-project review, findings 7-8) - QNT-384
- [x] Credentialed ClickHouse access: password on the default user via the official image entrypoint (compose env + :? interpolation guard), grafana_readonly scoped from ::/0 to RFC1918+loopback, creds wired through Settings into both app clients, the DockerRunLauncher env passthrough, and the migration runner (X-ClickHouse headers; stdlib .env fallback for CD's bare python3) (2026-07-17 whole-project review, finding 3) - QNT-381
- [x] Fundamentals ingest semantics: per-period share counts sourced from the balance sheet (Ordinary Shares Number as of period end, newest-period-only snapshot fallback, NULL otherwise) so historical EPS/EPS-TTM trends keep buyback effects instead of being rewritten with today's count each weekly ingest; missing Total Debt / Cash land NULL not 0.0 through fundamental_summary ratios to N/M rendering (a period missing debt no longer reads as debt-free); implied_shares_outstanding stamped only on the newest period; migrations 030-035 (Nullable columns, inert DEFAULT removed, column comments refreshed) (2026-07-17 whole-project review, findings 4-5) - QNT-382; follow-up: ebitda per period from the income statement (EBITDA/Normalized EBITDA line, NULL when absent - never the TTM info snapshot, a different unit on quarterly rows), EV/EBITDA + TTM EBITDA margin roll genuine 4Q sums mirroring the P/E treatment, market_cap stamped newest-period-only (unread downstream), migrations 036-039 incl. a data fix NULLing legacy TTM-snapshot ebitda values that would poison rolling windows
- [x] Per-shape synthesize output budget: fold the scattered per-call max_tokens constants (_COMPARISON_MAX_TOKENS/_THESIS_MAX_TOKENS) into one _OUTPUT_BUDGET table _structured_call consults by answer shape, + a guard test that fails if a new AnswerPayload union member ships unbudgeted - ends the reactive-sizing ratchet QNT-351/358/370 kept hand-turning. Live Langfuse data (reasoning-off, so output tokens == the max_tokens completion) overturned the review-finding premise: focused/exploration were NOT at the 1500 cliff (max 709/536 over n=43/6, >2x headroom) because the cards are structurally bounded and quote rather than expand, so both keep 1500 now stated explicitly; thesis 2500 / comparison 3000 unchanged; AC3 receipt = live exploration + all three focused sub-shapes finish_reason=stop (2026-07-17 whole-project review, finding 6) - QNT-383
- [x] Infra hygiene sweep: run-worker mem_limit (dagster.yaml container_kwargs `mem_limit: 2g`, anchored to dagster-code-server's equivalent import+materialize workload - a single runaway worker now OOMs local+loud at its own ceiling instead of invoking the host OOM killer that could pick clickhouse; review caught the first draft's false "3g matches the daemon cgroup" claim - the daemon is 512m post-QNT-116, so 2g/code-server is the honest anchor and 3x2g=6g is the host-safe concurrent worst case); corrected the ohlcv_raw_checks blocking-check comment (blocking gates only the in-job downstream, but the ohlcv ingest jobs select ohlcv_raw ONLY - the derived pipeline runs in a separate sensor-triggered ohlcv_downstream_job keyed on the materialization event, which fires regardless of check outcome; decision recorded: don't gate the sensor - alert loudly + ReplacingMergeTree self-heals same-key corruption, future-date new-key rows need a manual DELETE); Dockerfile uv layer caching restored (lockfile + 4 member pyprojects -> `--no-install-workspace` -> source copy, so code-only deploys skip the dep install; deps layer confirmed CACHED); docs: additive/forward-compatible migration rollback invariant (rollback restores code not schema), the permanent 2y refresh-horizon adjustment splice (pre-window history keeps the old split basis; OBV level + EMA seeds inherit it), and refreshed the concurrency pre-flight pattern + coordinator comment for the post-QNT-116 per-container topology (retired the obsolete daemon-cgroup `(daemon_mem-660)/360` formula) (2026-07-17 whole-project review, findings 9-13) - QNT-385
- [x] Registry-filter the no-ticker Qdrant search paths: /search/news and /search/earnings sent query_filter=None without a ticker, leaving the 117 off-registry points from the QNT-237 swap reachable (8 of 25 hits on "bank earnings"). One _ticker_filter helper now gates all four Qdrant queries in the module with a MatchAny over TICKERS; residue purge out of scope (2026-07-19 QNT-382 follow-up residue audit) - QNT-387

- [x] README factual-currency pass + repo metadata + hallucination-resistance baseline - QNT-282
