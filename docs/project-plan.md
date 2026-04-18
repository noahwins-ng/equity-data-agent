# Equity Data Agent — Project Plan

Progress tracking for the phased build-out. Each item maps to one or more Linear issues.
Updated automatically by `/ship` and `/sync-docs`.

---

### Phase 0 — Foundation
**Scope**: Repo scaffolding, infrastructure, and CI/CD.

- [x] Initialize monorepo with uv workspaces (4 packages)
- [x] Set up root `pyproject.toml` with shared dev dependencies (ruff, pyright, pytest)
- [x] Create `shared` package with `Settings`, ticker registry (`TICKERS` list + `TICKER_METADATA` dict with sector/industry per ticker), and base Pydantic schemas
- [x] Write `docker-compose.yml` with dev/prod profiles
- [x] Write `Dockerfile` (multi-stage: base with uv deps → dagster target, api target) — shared by dagster, dagster-daemon, and api services
- [x] Set up ClickHouse with DDL migration scripts (raw + derived databases)
- [x] Configure GitHub Actions for CI (lint + test) and CD (SSH deploy)
- [x] Create `.env.example` with all required environment variables
- [x] Bootstrap Hetzner CX41 production server: provision VPS, install Docker, configure GitHub deploy secrets, first manual deploy — QNT-83
- [x] Integration test infrastructure + prod health visibility — QNT-85
    - `/health` endpoint with ClickHouse connectivity check (200 ok / 503 degraded)
    - `tests/integration/` with auto-skip when ClickHouse unreachable locally
    - CI: ClickHouse service container + integration test step on every PR
    - Deploy pipeline: post-deploy health check gate (fails if API doesn't come up within 60s)
    - `make check-prod` and `make test-integration` helpers
- [x] Claude Code slash command framework (12 commands in `.claude/commands/`) and dev workflow docs (`docs/guides/dev-workflow.md`, this `project-plan.md`) — QNT-84
- [x] Verify: SSH tunnel to ClickHouse works, Dagster UI starts locally, CI pipeline passes

---

### Ops & Reliability
**Scope**: Cross-phase hardening that responds to prod incidents. Each item is reactive — triggered by a specific failure mode rather than a planned Phase deliverable. Lives outside the Phase 0–7 axis because the work cuts across phases.

- [x] CD hard gate: verify prod `git rev-parse HEAD` equals merged commit SHA — QNT-88
    - **Triggered by**: Apr 16 2026 outage during Phase 2 Calculation Layer work — CD reported success while prod was 17 commits behind main. Root cause was a SCP'd hotfix that blocked `git pull` during deploy, masked by a passing /health check on stale code.
- [x] CD hard gate: verify Dagster definitions module loads expected asset / check / schedule counts — QNT-89
    - **Triggered by**: Same Apr 16 2026 outage — container uptime didn't prove the deployed Python actually loaded the asset graph we thought we shipped.
- [x] Harden /go pipeline with three-class AC taxonomy (code vs dev-exec vs prod-exec) — QNT-90
    - **Triggered by**: Apr 16 2026 retrospective during Phase 3 API Layer work — `/sanity-check` had been marking AC ✓ based on code inspection alone, so "shipped but broken in prod" was technically allowed. Introduced the three-class AC taxonomy + evidence requirements + `/ship` post-deploy hard gates.
- [x] Add `restart: unless-stopped` to prod services in docker-compose.yml — QNT-95
    - **Triggered by**: Apr 18 2026 outage immediately after shipping QNT-51 (Phase 3 `/health` endpoint) — Hetzner VPS rebooted for a kernel update at 04:00 UTC, all 6 containers cleanly exited with `Exited (0)`, nothing came back up. ~48 min API outage until manual `docker compose --profile prod up -d`. Docker default restart policy is `no`.
- [x] Alert on pending kernel reboots (health-monitor log + unattended-upgrades mail via Resend SMTP) — QNT-96
    - **Triggered by**: Same Apr 18 2026 outage — `/var/run/reboot-required` had been set 21 hours earlier by `unattended-upgrades`, but no-one saw it. Fix adds a `REBOOT REQUIRED` line to `scripts/health-monitor.sh` (surfaced by `make monitor-log` + session-start hook) and wires `Unattended-Upgrade::Mail` through a postfix → Resend SMTP relay (documented in `docs/guides/hetzner-bootstrap.md` §10).
- [ ] Create ops runbook skeleton with failure-mode catalog — QNT-99
    - **Triggered by**: Apr 19 2026 retro — the Ops & Reliability work has turned specific incidents into permanent detectors, but there's no consolidated document to grep when something breaks at 3am. Runbook is the index into the muscle memory. Small scaffolding ticket; subsequent Ops & Reliability tickets add their own entries.
- [ ] Harden docker-compose.yml: HEALTHCHECK + log rotation + resource limits — QNT-100
    - **Triggered by**: Apr 19 2026 retro on the bespoke-compose vs OSS-PaaS tradeoff — evaluated adopting Coolify to get these defaults for free, concluded that configuring them directly is strictly better (fewer moving parts, no new critical infra). Closes three specific gaps: "sick but still up" (no healthchecks), "disk fills with logs" (no rotation), "one leaky service OOMs the box" (no resource limits).
- [ ] Alerting pipeline: uptime monitoring + container state notifications — QNT-101
    - **Triggered by**: Same Apr 19 2026 retro — Apr 18 outage surfaced that `/health` failures go into a log file nobody reads. Need real pager (SMS/email) for downtime + Discord notifications for container state changes.
- [ ] Encrypt .env at rest with SOPS — QNT-102
    - **Triggered by**: Same Apr 19 2026 retro — plaintext `.env` on VPS = all credentials leak on compromise. Replace with SOPS-encrypted file + decrypt-on-deploy. (ClickHouse backup ticket deferred: current data <1GB, re-ingestible from yfinance in 1-2h; revisit after Phase 4 news+embeddings populate.)
- [ ] Observability stack: Dozzle logs UI + Prometheus/Grafana/cAdvisor metrics — QNT-103
    - **Triggered by**: Same Apr 19 2026 retro — unified logs UI (Dozzle, lightweight) + resource trend visibility (Prometheus stack) are the Coolify UX wins we'd replicate directly. Enables diagnosing slow leaks before they become outages.
- [x] ~~Spike: evaluate Coolify for deploy/ops consolidation — QNT-97~~ (Cancelled 2026-04-19 — decision made directly without spike; superseded by QNT-99 through QNT-103.)
- [x] ~~Bootstrap Coolify on Hetzner CX41 — QNT-98~~ (Cancelled 2026-04-19 — decided not to adopt Coolify; gaps being addressed via targeted config tickets instead.)

---

### Phase 1 — Data Ingestion
**Scope**: Dagster assets that fetch and store OHLCV + fundamental data. Batch-only — no streaming.

**Ingestion Strategy**:
- **Partitioning**: `StaticPartitionsDefinition` by ticker (10 partitions). Per-ticker visibility, retry, and parallel execution in Dagster UI. **Max 3 concurrent partitions** to avoid yfinance rate limiting during backfill (configured via Dagster `TagConcurrencyLimit`).
- **Backfill**: One-time materialization of all partitions with `period="2y"` (2 years of history). Enough for all technical indicators and YoY comparisons. Triggered via manual materialization in Dagster UI with `period="2y"` asset config.
- **Incremental (daily OHLCV)**: Fetch last 5 trading days per ticker, `ReplacingMergeTree` deduplicates. No need to track "last fetched date." The daily schedule hardcodes `period="5d"` via `RunConfig`.
- **Incremental (weekly fundamentals)**: Fetch all available quarters (yfinance returns last 4 quarterly + 4 annual), `ReplacingMergeTree` deduplicates.
- **Schedule**: Daily OHLCV at ~5-6 PM ET (after market close, data settles). Weekly fundamentals on weekends.

**Deliverables**:
- [x] Implement `ohlcv_raw` Dagster asset (yfinance → ClickHouse) — QNT-41
    - `StaticPartitionsDefinition` by ticker
    - Backfill: `period="2y"`, Incremental: `period="5d"`
    - Rate limiting: 1-2s sleep between tickers, exponential backoff on 429s
- [x] Implement `fundamentals` Dagster asset (yfinance → ClickHouse) — QNT-42
    - `StaticPartitionsDefinition` by ticker
    - Fetches all available quarterly + annual data each run
- [x] Add Dagster schedules: daily for OHLCV (~5-6 PM ET), weekly for fundamentals — QNT-43
- [x] Implement Dagster resource for ClickHouse client (shared across assets) — QNT-40
- ~Implement `make seed`~ — cancelled: dev tunnels to prod ClickHouse, no local seed needed — QNT-82
- [ ] Verify: Run backfill for all 10 tickers, confirm data in ClickHouse, check Dagster lineage graph

---

### Phase 2 — Calculation Layer
**Scope**: Technical indicators, fundamental ratio computation, and multi-timeframe aggregation.

- [x] Implement `ohlcv_weekly` and `ohlcv_monthly` Dagster aggregation assets — QNT-70
- [x] `ohlcv_weekly`:
    - Reads from `ohlcv_raw`, aggregates daily bars → weekly (Monday-based) OHLCV
    - Aggregation via pandas groupby (`toMonday(date)`): open=first, close=last, adj_close=last, high=max, low=min, volume=sum
    - **Skip the current incomplete week** — only emit bars for weeks where the last trading day has passed (avoids partial bars that would distort indicators)
    - Downstream dependency on `ohlcv_raw` asset
- [x] `ohlcv_monthly`:
    - Reads from `ohlcv_raw`, aggregates daily bars → monthly OHLCV
    - Same aggregation logic (open=first, close=last, adj_close=last, high=max, low=min, volume=sum) with `toStartOfMonth(date)` grouping
    - **Skip the current incomplete month** — same rationale as weekly
    - Downstream dependency on `ohlcv_raw` asset
- [x] Implement `technical_indicators` Dagster assets (daily, weekly, monthly) — QNT-44
    - Computes RSI-14, MACD (12/26/9), SMA-20/50, EMA-12/26, Bollinger Bands (20,2)
    - **Price input: `adj_close`** — use adjusted close to avoid false signals at stock split boundaries
    - **Warm-up**: indicators are `null` until enough prior data exists (RSI-14: 14 rows, SMA-50: 50 rows, MACD signal: 35 rows). Rows are still written with nulls — FastAPI and frontend handle display.
    - Same indicator code, three input sources: `ohlcv_raw`, `ohlcv_weekly`, `ohlcv_monthly`
    - Writes to `technical_indicators_daily`, `_weekly`, `_monthly`
    - Uses pandas/numpy — all math in Python, never in the LLM
- [x] Implement `fundamental_summary` Dagster asset (15 ratios) — QNT-45
    - **Valuation**: P/E, EV/EBITDA, P/B, P/S, EPS
    - **Growth**: revenue YoY%, net income YoY%, FCF YoY%
    - **Profitability**: net margin%, gross margin%, ROE, ROA
    - **Cash**: FCF yield
    - **Leverage**: D/E
    - **Liquidity**: current ratio
    - Downstream dependency on BOTH `fundamentals` AND `ohlcv_raw` — price-based ratios (P/E, P/B, P/S, FCF yield) require latest close price from `ohlcv_raw`
- [x] Add Dagster sensors to trigger downstream recomputation when raw data refreshes — QNT-46
    - `ohlcv_raw` materialization → triggers `ohlcv_weekly`, `ohlcv_monthly`, `technical_indicators_daily`, `fundamental_summary`
    - `fundamentals` materialization → triggers `fundamental_summary`
    - This means price-based ratios (P/E, P/B, P/S, FCF yield) update daily with fresh close prices, while statement-based ratios (margins, growth) update weekly with fresh fundamentals
- [x] Add Dagster asset checks for data quality validation — QNT-68
    - e.g., no NaN close prices, volume > 0, RSI within 0-100, no future dates
- [x] Validation tests: indicators vs external sources — QNT-47
    - Snapshot tests with fixed datasets and expected outputs
    - Cross-reference RSI, MACD, P/E for 2-3 tickers against TradingView / Yahoo Finance
    - Tolerance: 1% for technical indicators, exact match for fundamental ratios; fixtures committed for determinism
- [x] Verify: Run full pipeline Raw → Aggregation → Indicators, spot-check calculations against external sources (e.g., TradingView) — covered by QNT-47 (canonical Wilder/Appel cross-reference tests) + QNT-68 asset checks + `docs/retros/phase-2-ac-audit.md`

---

### Phase 3 — API Layer
**Scope**: FastAPI endpoints serving machine-readable data (frontend charts) and human-readable reports (agent).
**Dependencies**: Requires Phase 2 (data must exist in ClickHouse). Can proceed in parallel with Phase 4 — news endpoints gracefully degrade to empty responses until Phase 4 populates `news_raw`.

**Report template — build this FIRST (QNT-69):**
- [x] Design **one** report template end-to-end against real ClickHouse data — QNT-69 **[start of Phase 3]**
    - **Target: the technical report** (`/reports/technical/{ticker}`). Build the full pipeline — query CH → format into a report string → expose at the endpoint — against live Phase 2 data. Iterate with eyes on the actual output until it reads well. THEN parameterise the pattern for fundamental / news / summary.
    - **Rationale**: the templates are where the "intelligence vs math" thesis actually lives in the product — they determine what the agent can reason over. Parameterising a bad template 4 times is waste; finding the right shape once and then applying it is not.
    - Structured sections (not walls of text), comparative context ("RSI 72.3 — above 70, approaching overbought"), historical context ("Revenue grew 23% YoY, accelerating from 18%"), explicit signal clarity (bullish / bearish / neutral).
    - **Null/N/M display conventions** (Phase 2 retro finding): P/E nulled when `|EPS| < $0.10` → "N/M (near-zero earnings)", quarterly P/E uses TTM net income, indicator warm-up nulls → "Insufficient data (N bars required)". These conventions apply to all report endpoints.
    - Templates stored under `packages/api/src/api/templates/` or as formatter functions in services.

**Report endpoints (text — for the agent; all apply the QNT-69 template pattern):**
- [x] `GET /api/v1/reports/technical/{ticker}` — formatted text report with indicator context — QNT-48 *(first concrete output of QNT-69)*
- [x] `GET /api/v1/reports/fundamental/{ticker}` — formatted text report with ratio context — QNT-49
- [x] `GET /api/v1/reports/news/{ticker}` — recent news summary. Depends on Phase 4 `news_raw` data — returns 200 with a well-formed text report containing an `N/M (no news ingested…)` block until Phase 4 populates data. Sentiment narrative lands when QNT-55 (Qdrant search) ships. — QNT-79
- [x] `GET /api/v1/reports/summary/{ticker}` — combined text overview: latest price context, RSI interpretation, trend narrative, and sector context. Sector context derived from a static mapping in `shared/tickers.py`. Used by the agent as a quick "at a glance" tool. — QNT-50

**Data endpoints (JSON — for the frontend):**
- [x] `GET /api/v1/ohlcv/{ticker}?timeframe=daily|weekly|monthly` — returns `[{time, open, high, low, close, adj_close, volume}]` for TradingView chart rendering. `time` is an ISO date string `"YYYY-MM-DD"` — QNT-76
- [x] `GET /api/v1/indicators/{ticker}?timeframe=daily|weekly|monthly` — returns `[{time, rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, ema_12, ema_26, bb_upper, bb_middle, bb_lower}]` as row-oriented time-series (`null` during indicator warm-up period) — QNT-77
    - **Warm-up periods**: RSI-14: 14, EMA-12: 12, EMA-26/MACD/MACD signal: 35, SMA-20/BB: 20, SMA-50: 50. All non-null from row 50 onward.
- [x] `GET /api/v1/fundamentals/{ticker}` — latest fundamental ratios as structured JSON for the ticker detail page ratios table — QNT-80
- [x] `GET /api/v1/dashboard/summary` — returns `[{ticker, price, daily_change_pct, rsi_14, rsi_signal, trend_status}]` for ALL tickers in a single response. Avoids N+1 requests on dashboard load. — QNT-81
    - `price`: latest available `close` from `ohlcv_raw` (real market price, NOT `adj_close`)
    - `daily_change_pct`: `(latest_close - prev_close) / prev_close * 100` — trivial presentation arithmetic (see §2.1)
    - `rsi_signal`: `"overbought"` (RSI > 70), `"oversold"` (RSI < 30), `"neutral"` (30–70)
    - `trend_status`: `"bullish"` (close > SMA-50), `"bearish"` (close < SMA-50), `"neutral"` (warm-up)

**Utility endpoints:**
- [ ] `GET /api/v1/tickers` — returns the ticker list from `shared.tickers.TICKERS` — QNT-78
- [x] `GET /api/v1/health` — health check with ClickHouse + Qdrant connectivity status + deploy identity (git SHA, Dagster asset/check counts) — QNT-51

**Cross-cutting:**
- [ ] CORS middleware configured (allow production domain, `*.vercel.app` for preview deploys, and `localhost:3001` for dev)
- [ ] Ticker validation: all `{ticker}` path endpoints AND the `POST /agent/chat` request body validate the ticker against `shared.tickers.TICKERS` and return `404 {"detail": "Ticker not found"}` for unknown tickers
- [ ] No API authentication in initial scope — the API is read-only and serves public market data
- [ ] Verify: Hit all endpoints with VS Code REST Client (`.http` files), confirm chart data arrays are correctly structured, check OpenAPI docs at `/docs`

---

### Phase 4 — Narrative Data
**Scope**: News ingestion, embedding, and semantic search via Qdrant.

- [ ] Ingest news via **RSS + `feedparser`** — QNT-52
    - Per-ticker Yahoo Finance RSS (`https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US`), plus 1–2 broad market feeds (e.g., Reuters markets RSS)
    - No paid news API evaluation — RSS is free, unrate-limited, and deterministic enough for a 10-ticker scope. The news-API comparison rabbit hole is not the portfolio story; RSS + embeddings + semantic search is.
- [ ] Implement `news_raw` Dagster asset (RSS feeds → `equity_raw.news_raw` in ClickHouse) — QNT-53
    - Schedule: every 4 hours during market hours, `default_status=RUNNING` (Phase 2 lesson: QNT-92)
    - Dedup key: `id = hash(ticker + url)`
    - Stores: `headline`, `body`, `source`, `url`, `published_at` per ticker
    - Downstream sensor (`news_raw` → `news_embeddings`) must batch all pending events per tick from day one (Phase 2 lesson: QNT-46 rewrite)
- [ ] Create Qdrant `equity_news` collection (384-dim Float32, cosine distance) — auto-create in the Qdrant Dagster resource on first use, or via a setup script
- [ ] Implement `news_embeddings` Dagster asset (`news_raw` → Qdrant Cloud) — QNT-54
    - Embeds `headline` text using `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
    - Sensor-triggered when `news_raw` materializes new rows
    - Stores vector + full payload (headline, source, url, published_at, ticker) in Qdrant
- [ ] `GET /api/v1/search/news?ticker=NVDA&query=earnings` — QNT-55
    - Returns `[{headline, source, url, published_at, score}]` — top-N results ranked by cosine similarity
    - Both `ticker` and `query` are required. Returns `[]` if Qdrant is unreachable or no news data exists.
- [ ] Add Dagster asset checks for `news_raw` and `news_embeddings` data quality — QNT-93
    - `news_raw`: no empty headlines, valid URLs, no future `published_at` dates, row count per ticker
    - `news_embeddings`: vector count matches source row count, no orphaned vectors
    - Phase 2 lesson: QNT-68 asset checks caught 2 real formula bugs — apply same pattern to news assets
- [ ] Verify: Search for recent news about a ticker, confirm relevance ranking

---

### Phase 5 — Agent Layer
**Scope**: LangGraph agent with tools that call FastAPI endpoints.

- [ ] Configure LiteLLM proxy via `litellm_config.yaml` — QNT-59
    - **Default**: routes to Ollama Cloud (`https://ollama.com/v1`) via `OLLAMA_API_KEY`
    - **Override**: routes to Claude API via `ANTHROPIC_API_KEY`
    - Model alias: `equity-agent/default` — zero agent code changes to switch backends
- [ ] Integrate Langfuse tracing — QNT-61 **[day-one of Phase 5, moved from Phase 7]**
    - `LangfuseResource` in the agent package; `@observe` decorator on every tool and graph node from the first commit of agent code — traces are needed *while* iterating on the prompt, not bolted on after shipping.
    - Portfolio artifact: one Langfuse trace screenshot is embedded in the README (QNT-66).
- [ ] Define LangGraph state schema (ticker under analysis, gathered reports, thesis draft) — QNT-56
- [ ] Implement tools — QNT-57
    - `get_summary_report` → calls `/reports/summary/{ticker}` (agent calls this first)
    - `get_technical_report` → calls `/reports/technical/{ticker}`
    - `get_fundamental_report` → calls `/reports/fundamental/{ticker}`
    - `get_news_report` → calls `/reports/news/{ticker}`
    - `search_news` → calls `/search/news`
- [ ] Build agent graph — **3 nodes: plan → gather → synthesize** (per ADR-007) — QNT-56
    - No critique / reflect / retry loop until the baseline has failed in specific, observed ways. Adding loops prematurely is indistinguishable from the baseline working.
- [ ] System prompt enforcing the "interpret, don't calculate" mandate — QNT-58
- [ ] Agent CLI: `python -m agent analyze NVDA` — run single-ticker analysis from terminal — QNT-60
    - **Built before the SSE endpoint** — ~50× faster prompt iteration without a frontend round-trip. Pairs with the eval harness below.
- [ ] Agent evaluation framework — QNT-67 **[highest-priority Phase 5 item — the single biggest AI-Engineer hiring signal]**
    - Lives under `packages/agent/evals/`. Three eval types — all required, not optional:
    - **(a) Numeric-claim hallucination detector** (`evals/hallucination.py`): regex every number out of the agent's thesis; assert each appears verbatim in one of the report strings the agent received as tool output. Any mismatch = test failure. Operationalises the ADR-003 contract.
    - **(b) Golden set** (`evals/golden_set.py` + `evals/goldens/questions.yaml`): 15–20 curated `(ticker, question, reference_thesis, expected_tools)` pairs. Per run, track LLM-as-judge score + cosine similarity of generated thesis vs reference thesis. Commit `evals/history.csv` so prompt-version quality is visible in `git log -p`.
    - **(c) Tool-call correctness** (`evals/tool_calls.py`): for each golden-set question, assert the expected tool was called — e.g., valuation questions MUST call `get_fundamental_report`, technical questions MUST call `get_technical_report`.
    - Design goal: harness is reusable enough to extract as a standalone repo later.
- [ ] `POST /api/v1/agent/chat` SSE endpoint for frontend chat page — QNT-56
    - **Built after the CLI + evals** — same graph, different transport. The CLI shakes out prompt regressions before they reach the UI.
    - **Request**: `{"ticker": "NVDA", "message": "Analyze this stock"}` — stateless, single-analysis
    - **SSE events**: `tool_call` → `thinking` → `thesis` → `done`
- [ ] Portfolio README — QNT-66 **[moved from Phase 7 — front-page recruiter artifact]**
    - Architecture diagram (mermaid, reused from `project-requirement.md` §3.1)
    - One Langfuse trace screenshot (a full `plan → gather → synthesize` run)
    - One Dagster lineage screenshot (the `ohlcv_raw → indicators → fundamental_summary` graph)
    - One agent-thesis screenshot (CLI output, NVDA or similar)
    - One-paragraph hallucination-resistance pitch (ties ADR-003 + QNT-67 eval harness)
    - This matters more than anything in Phase 7. Recruiters read the README before opening any code file.
- [ ] 30-second CLI demo screencast — QNT-94
    - Record `python -m agent analyze NVDA` producing a thesis end-to-end; commit as `docs/demo.mp4` (or host and link from README above-the-fold)
    - Single most-watched portfolio artifact. Must show: command invocation → first tool call → streamed thinking → final thesis, within ≤45s (target 30s).
- [ ] Verify: Run agent on 2-3 tickers, review thesis quality, confirm zero hallucinated calculations in Langfuse traces; hallucination eval passes on all golden-set questions; README renders correctly on GitHub with all screenshots and the embedded/linked demo

---

### Phase 6 — Frontend
**Scope**: Next.js dashboard, ticker detail with TradingView charts, and agent chat interface. Deployed on Vercel.
**Dependencies**: Requires Phase 3. Requires Phase 5 for agent chat page. News sidebar gracefully degrades if Phase 4 is not yet complete.

- [ ] Initialize Next.js app in `frontend/` with Tailwind CSS
- [ ] Dashboard page (`/`) — ticker cards showing price, daily change, RSI signal, trend status
    - Calls `GET /api/v1/dashboard/summary` (single request for all tickers — no N+1)
- [ ] Ticker detail page (`/ticker/[symbol]`) — full analysis view
    - TradingView Lightweight Charts: candlestick + volume (`GET /api/v1/ohlcv/{ticker}`). **Chart renders `adj_close` as the candlestick close value** to avoid split discontinuities
    - Timeframe toggle: daily / weekly / monthly (swaps chart + indicator data)
    - Technical indicator overlays: SMA, EMA, Bollinger Bands on chart; RSI, MACD as separate panes
    - Fundamental ratios table: 15 ratios in 5 categories (`GET /api/v1/fundamentals/{ticker}`)
    - Recent news sidebar (`GET /api/v1/search/news?ticker={ticker}`) — gracefully degrades if Phase 4 not deployed
- [ ] Agent chat page (`/chat`) — conversational interface
    - Calls `POST /api/v1/agent/chat` with SSE streaming
    - **No Vercel AI SDK** — use native `fetch` + `ReadableStream`. Optionally add `eventsource-parser` (~2KB) for SSE line parsing.
    - Displays agent thesis with markdown rendering
    - Shows which tools the agent called (transparency)
- [ ] Generate TypeScript types from FastAPI's `/openapi.json` via `openapi-typescript` (`make types`) — do not handwrite types in `lib/api.ts`
- [ ] Deploy to Vercel, set `NEXT_PUBLIC_API_URL` in Vercel dashboard
- [ ] Verify: Dashboard loads all 10 tickers, chart renders with timeframe toggle, agent chat streams a thesis

---

### Phase 7 — Observability & Polish
**Scope**: Tracing, alerting, and production hardening.

- [ ] Integrate Sentry for FastAPI error tracking (`sentry-sdk[fastapi]`, uses `SENTRY_DSN` from `.env`) — QNT-86
- [ ] Add Dagster alerting on asset materialization failures — QNT-62
- [ ] Implement retry logic for flaky external API calls (yfinance, news APIs) — QNT-63
- [ ] Load test FastAPI endpoints (confirm response times under 10 tickers) — QNT-65
- [ ] Write integration tests for critical paths (ingestion → calculation → report → agent) — QNT-64
- [ ] Verify: End-to-end run on all 10 tickers, review Langfuse dashboard, confirm no orphaned errors in Sentry
