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
- [x] Verify: SSH tunnel to ClickHouse works, Dagster UI starts locally, CI pipeline passes

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
- [ ] Implement `ohlcv_raw` Dagster asset (yfinance → ClickHouse) — QNT-41
    - `StaticPartitionsDefinition` by ticker
    - Backfill: `period="2y"`, Incremental: `period="5d"`
    - Rate limiting: 1-2s sleep between tickers, exponential backoff on 429s
- [ ] Implement `fundamentals` Dagster asset (yfinance → ClickHouse) — QNT-42
    - `StaticPartitionsDefinition` by ticker
    - Fetches all available quarterly + annual data each run
- [ ] Add Dagster schedules: daily for OHLCV (~5-6 PM ET), weekly for fundamentals — QNT-43
- [ ] Implement Dagster resource for ClickHouse client (shared across assets) — QNT-40
- [ ] Implement `make seed` — quick seed script (30 days, 3 tickers) for fast local dev data setup without a full backfill — QNT-82
- [ ] Verify: Run backfill for all 10 tickers, confirm data in ClickHouse, check Dagster lineage graph

---

### Phase 2 — Calculation Layer
**Scope**: Technical indicators, fundamental ratio computation, and multi-timeframe aggregation.

- [ ] Implement `ohlcv_weekly` and `ohlcv_monthly` Dagster aggregation assets — QNT-70
- [ ] `ohlcv_weekly`:
    - Reads from `ohlcv_raw`, aggregates daily bars → weekly (Monday-based) OHLCV
    - Aggregation via pandas groupby (`toMonday(date)`): open=first, close=last, adj_close=last, high=max, low=min, volume=sum
    - **Skip the current incomplete week** — only emit bars for weeks where the last trading day has passed (avoids partial bars that would distort indicators)
    - Downstream dependency on `ohlcv_raw` asset
- [ ] `ohlcv_monthly`:
    - Reads from `ohlcv_raw`, aggregates daily bars → monthly OHLCV
    - Same aggregation logic (open=first, close=last, adj_close=last, high=max, low=min, volume=sum) with `toStartOfMonth(date)` grouping
    - **Skip the current incomplete month** — same rationale as weekly
    - Downstream dependency on `ohlcv_raw` asset
- [ ] Implement `technical_indicators` Dagster assets (daily, weekly, monthly) — QNT-44
    - Computes RSI-14, MACD (12/26/9), SMA-20/50, EMA-12/26, Bollinger Bands (20,2)
    - **Price input: `adj_close`** — use adjusted close to avoid false signals at stock split boundaries
    - **Warm-up**: indicators are `null` until enough prior data exists (RSI-14: 14 rows, SMA-50: 50 rows, MACD signal: 35 rows). Rows are still written with nulls — FastAPI and frontend handle display.
    - Same indicator code, three input sources: `ohlcv_raw`, `ohlcv_weekly`, `ohlcv_monthly`
    - Writes to `technical_indicators_daily`, `_weekly`, `_monthly`
    - Uses pandas/numpy — all math in Python, never in the LLM
- [ ] Implement `fundamental_summary` Dagster asset (15 ratios) — QNT-45
    - **Valuation**: P/E, EV/EBITDA, P/B, P/S, EPS
    - **Growth**: revenue YoY%, net income YoY%, FCF YoY%
    - **Profitability**: net margin%, gross margin%, ROE, ROA
    - **Cash**: FCF yield
    - **Leverage**: D/E
    - **Liquidity**: current ratio
    - Downstream dependency on BOTH `fundamentals` AND `ohlcv_raw` — price-based ratios (P/E, P/B, P/S, FCF yield) require latest close price from `ohlcv_raw`
- [ ] Add Dagster sensors to trigger downstream recomputation when raw data refreshes — QNT-46
    - `ohlcv_raw` materialization → triggers `ohlcv_weekly`, `ohlcv_monthly`, `technical_indicators_daily`, `fundamental_summary`
    - `fundamentals` materialization → triggers `fundamental_summary`
    - This means price-based ratios (P/E, P/B, P/S, FCF yield) update daily with fresh close prices, while statement-based ratios (margins, growth) update weekly with fresh fundamentals
- [ ] Add Dagster asset checks for data quality validation — QNT-68
    - e.g., no NaN close prices, volume > 0, RSI within 0-100, no future dates
- [ ] Verify: Run full pipeline Raw → Aggregation → Indicators, spot-check calculations against external sources (e.g., TradingView)

---

### Phase 3 — API Layer
**Scope**: FastAPI endpoints serving machine-readable data (frontend charts) and human-readable reports (agent).
**Dependencies**: Requires Phase 2 (data must exist in ClickHouse). Can proceed in parallel with Phase 4 — news endpoints gracefully degrade to empty responses until Phase 4 populates `news_raw`.

**Report endpoints (text — for the agent):**
- [ ] `GET /api/v1/reports/technical/{ticker}` — formatted text report with indicator context — QNT-48
- [ ] `GET /api/v1/reports/fundamental/{ticker}` — formatted text report with ratio context — QNT-49
- [ ] `GET /api/v1/reports/news/{ticker}` — recent news summary with sentiment (returns top-N headlines + brief sentiment narrative). Sentiment is computed by FastAPI at query time via simple keyword/headline analysis (positive/negative/neutral count) — not LLM-generated. Depends on Phase 4 `news_raw` data — returns 200 with `{"report": "No news data available."}` until Phase 4 populates data. — QNT-79
- [ ] `GET /api/v1/reports/summary/{ticker}` — combined text overview: latest price context, RSI interpretation, trend narrative, and sector context. Sector context derived from a static mapping in `shared/tickers.py`. Used by the agent as a quick "at a glance" tool. — QNT-50
- [ ] Report formatting: human-readable strings with context (e.g., "RSI at 72.3 — approaching overbought territory")

**Data endpoints (JSON — for the frontend):**
- [ ] `GET /api/v1/ohlcv/{ticker}?timeframe=daily|weekly|monthly` — returns `[{time, open, high, low, close, adj_close, volume}]` for TradingView chart rendering. `time` is an ISO date string `"YYYY-MM-DD"` — QNT-76
- [ ] `GET /api/v1/indicators/{ticker}?timeframe=daily|weekly|monthly` — returns `[{time, rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, ema_12, ema_26, bb_upper, bb_middle, bb_lower}]` as row-oriented time-series (`null` during indicator warm-up period) — QNT-77
    - **Warm-up periods**: RSI-14: 14, EMA-12: 12, EMA-26/MACD/MACD signal: 35, SMA-20/BB: 20, SMA-50: 50. All non-null from row 50 onward.
- [ ] `GET /api/v1/fundamentals/{ticker}` — latest fundamental ratios as structured JSON for the ticker detail page ratios table — QNT-80
- [ ] `GET /api/v1/dashboard/summary` — returns `[{ticker, price, daily_change_pct, rsi_14, rsi_signal, trend_status}]` for ALL tickers in a single response. Avoids N+1 requests on dashboard load. — QNT-81
    - `price`: latest available `close` from `ohlcv_raw` (real market price, NOT `adj_close`)
    - `daily_change_pct`: `(latest_close - prev_close) / prev_close * 100` — trivial presentation arithmetic (see §2.1)
    - `rsi_signal`: `"overbought"` (RSI > 70), `"oversold"` (RSI < 30), `"neutral"` (30–70)
    - `trend_status`: `"bullish"` (close > SMA-50), `"bearish"` (close < SMA-50), `"neutral"` (warm-up)

**Utility endpoints:**
- [ ] `GET /api/v1/tickers` — returns the ticker list from `shared.tickers.TICKERS` — QNT-78
- [ ] `GET /api/v1/health` — health check with ClickHouse + Qdrant connectivity status — QNT-51

**Cross-cutting:**
- [ ] CORS middleware configured (allow production domain, `*.vercel.app` for preview deploys, and `localhost:3001` for dev)
- [ ] Ticker validation: all `{ticker}` path endpoints AND the `POST /agent/chat` request body validate the ticker against `shared.tickers.TICKERS` and return `404 {"detail": "Ticker not found"}` for unknown tickers
- [ ] No API authentication in initial scope — the API is read-only and serves public market data
- [ ] Verify: Hit all endpoints with VS Code REST Client (`.http` files), confirm chart data arrays are correctly structured, check OpenAPI docs at `/docs`

---

### Phase 4 — Narrative Data
**Scope**: News ingestion, embedding, and semantic search via Qdrant.

- [ ] Evaluate and select free news API (NewsAPI.org, GNews, or RSS feeds) — QNT-52
- [ ] Implement `news_raw` Dagster asset (free API → `equity_raw.news_raw` in ClickHouse) — QNT-53
    - Schedule: every 4 hours during market hours
    - Dedup key: `id = hash(ticker + url)`
    - Stores: `headline`, `body`, `source`, `url`, `published_at` per ticker
- [ ] Create Qdrant `equity_news` collection (384-dim Float32, cosine distance) — auto-create in the Qdrant Dagster resource on first use, or via a setup script
- [ ] Implement `news_embeddings` Dagster asset (`news_raw` → Qdrant Cloud) — QNT-54
    - Embeds `headline` text using `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
    - Sensor-triggered when `news_raw` materializes new rows
    - Stores vector + full payload (headline, source, url, published_at, ticker) in Qdrant
- [ ] `GET /api/v1/search/news?ticker=NVDA&query=earnings` — QNT-55
    - Returns `[{headline, source, url, published_at, score}]` — top-N results ranked by cosine similarity
    - Both `ticker` and `query` are required. Returns `[]` if Qdrant is unreachable or no news data exists.
- [ ] Verify: Search for recent news about a ticker, confirm relevance ranking

---

### Phase 5 — Agent Layer
**Scope**: LangGraph agent with tools that call FastAPI endpoints.

- [ ] Configure LiteLLM proxy via `litellm_config.yaml` — QNT-59
    - **Default**: routes to Ollama Cloud (`https://ollama.com/v1`) via `OLLAMA_API_KEY`
    - **Override**: routes to Claude API via `ANTHROPIC_API_KEY`
    - Model alias: `equity-agent/default` — zero agent code changes to switch backends
- [ ] Define LangGraph state schema (ticker under analysis, gathered reports, thesis draft) — QNT-56
- [ ] Implement tools — QNT-57
    - `get_summary_report` → calls `/reports/summary/{ticker}` (agent calls this first)
    - `get_technical_report` → calls `/reports/technical/{ticker}`
    - `get_fundamental_report` → calls `/reports/fundamental/{ticker}`
    - `get_news_report` → calls `/reports/news/{ticker}`
    - `search_news` → calls `/search/news`
- [ ] Build agent graph: plan → gather data → analyze → synthesize thesis — QNT-56
- [ ] System prompt enforcing the "interpret, don't calculate" mandate — QNT-58
- [ ] Agent CLI: `python -m agent analyze NVDA` — run single-ticker analysis from terminal — QNT-60
- [ ] `POST /api/v1/agent/chat` SSE endpoint for frontend chat page — QNT-56
    - **Request**: `{"ticker": "NVDA", "message": "Analyze this stock"}` — stateless, single-analysis
    - **SSE events**: `tool_call` → `thinking` → `thesis` → `done`
- [ ] Agent evaluation framework — QNT-67
- [ ] Verify: Run agent on 2-3 tickers, review thesis quality, confirm zero hallucinated calculations in Langfuse traces

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

- [ ] Integrate Langfuse for agent trace logging (thoughts, tool calls, latency) — QNT-61
- [ ] Integrate Sentry for FastAPI error tracking (`sentry-sdk[fastapi]`, uses `SENTRY_DSN` from `.env`) — QNT-64
- [ ] Add Dagster alerting on asset materialization failures — QNT-62
- [ ] Implement retry logic for flaky external API calls (yfinance, news APIs) — QNT-63
- [ ] Load test FastAPI endpoints (confirm response times under 10 tickers) — QNT-65
- [ ] Write integration tests for critical paths (ingestion → calculation → report → agent) — QNT-64
- [ ] Write README with setup instructions and architecture diagram — QNT-66
- [ ] Verify: End-to-end run on all 10 tickers, review Langfuse dashboard, confirm no orphaned errors in Sentry
