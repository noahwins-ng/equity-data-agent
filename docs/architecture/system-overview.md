# System Overview

## Core Philosophy

**Intelligence vs. Math** — The LLM never calculates. It interprets pre-computed results.

| Layer | What it does | Technology | Where it lives |
|---|---|---|---|
| Calculation | Complex financial math (indicators, ratios, aggregations) | Python/SQL | `packages/dagster-pipelines/` |
| Serving | Turns DB rows into reports + JSON arrays; trivial presentation arithmetic (daily change %, RSI categories, trend labels) | FastAPI | `packages/api/` |
| Reasoning | Interprets reports, writes theses | LangGraph | `packages/agent/` |
| Shared | Schemas, config, ticker registry | Pydantic | `packages/shared/` |
| Presentation | Charts, agent chat UI | Next.js 15 | `frontend/` |

## Package Dependencies

```
shared              ← no internal deps
dagster-pipelines   ← shared
agent               ← shared (calls api via HTTP, never imports it)
api                 ← shared + agent (runs LangGraph graph in-process for SSE endpoint)
```

## Data Flow

```
yfinance ──→ Dagster ──→ ClickHouse (equity_raw.ohlcv_raw)
                              │
                              ▼
                         Dagster ──→ ClickHouse (equity_derived.ohlcv_weekly / ohlcv_monthly)
                                          │
                                          ▼
                                     Dagster ──→ ClickHouse (equity_derived.technical_indicators_daily/weekly/monthly)
                                                      │
                                                      ▼
                                                  FastAPI ◄─── Next.js (charts, agent chat UI) ← User
                                                      │
                                                      ▼
                                              LangGraph Agent ──→ Investment Thesis (SSE stream)
                                                      │
News API ──→ Dagster ──→ ClickHouse (equity_raw.news_raw)
                    └──→ Qdrant Cloud (equity_news embeddings) ──→ FastAPI ──────────┘
```

## Databases

**ClickHouse** (Hetzner, accessed via SSH tunnel in dev):

`equity_raw` — ingested data:
- `ohlcv_raw` — daily OHLCV bars from yfinance (2-year history, 5-day incremental)
- `fundamentals` — quarterly/annual financial statements per ticker (revenue, net income, balance sheet, etc.)
- `news_raw` — raw news headlines and body text before embedding

`equity_derived` — computed data:
- `ohlcv_weekly` — weekly bars aggregated from ohlcv_raw (open=first, close=last, adj_close=last, high=max, low=min, volume=sum)
- `ohlcv_monthly` — monthly bars aggregated from ohlcv_raw
- `technical_indicators_daily` — RSI-14, MACD(12/26/9), SMA-20/50, EMA-12/26, BB(20,2) on daily bars (computed from `adj_close`)
- `technical_indicators_weekly` — same indicators computed on weekly bars
- `technical_indicators_monthly` — same indicators computed on monthly bars
- `fundamental_summary` — 15 derived ratios (P/E, EV/EBITDA, margins, YoY growth, etc.) from fundamentals + ohlcv_raw

All tables use `ReplacingMergeTree` for idempotency. FastAPI queries **must** use `SELECT ... FROM table FINAL` for consistent reads (see ADR-001).

**Qdrant Cloud** (managed, free tier):
- `equity_news` collection — news embeddings (all-MiniLM-L6-v2, 384-dim Float32, runs as Python library in Dagster — not Ollama) with ticker/date/headline payload

## API Endpoint Categories

**Report endpoints** (text strings, consumed by LangGraph agent):
- `GET /api/v1/reports/technical/{ticker}` — human-readable technical analysis
- `GET /api/v1/reports/fundamental/{ticker}` — human-readable fundamental summary
- `GET /api/v1/reports/news/{ticker}` — recent news summary with sentiment (top-N headlines + narrative; depends on Phase 4 data)
- `GET /api/v1/reports/summary/{ticker}` — combined text overview for agent "at a glance" tool

**Data endpoints** (JSON, consumed by Next.js frontend):
- `GET /api/v1/ohlcv/{ticker}?timeframe=daily|weekly|monthly` — `{time, open, high, low, close, adj_close, volume}[]` — `time` is ISO date `"YYYY-MM-DD"`. Chart renders `adj_close` as candlestick close to avoid split artifacts.
- `GET /api/v1/indicators/{ticker}?timeframe=daily|weekly|monthly` — `{time, rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, ema_12, ema_26, bb_upper, bb_middle, bb_lower}[]` — `null` during warm-up
- `GET /api/v1/fundamentals/{ticker}` — `{ticker, period_end, period_type, pe_ratio, ev_ebitda, ...}[]` for ratios table
- `GET /api/v1/dashboard/summary` — `[{ticker, price, daily_change_pct, rsi_14, rsi_signal, trend_status}]` all tickers in one call. `price` = latest available close (not adj_close). `rsi_signal`: overbought/neutral/oversold. `trend_status`: bullish/bearish/neutral (close vs SMA-50).

**Search endpoints** (JSON, consumed by frontend and agent):
- `GET /api/v1/search/news?ticker=NVDA&query=earnings` — Qdrant semantic search over news embeddings. Returns top-N relevant headlines with scores. Depends on Phase 4 data; returns empty results if Qdrant is unreachable.

**Utility endpoints**:
- `GET /api/v1/tickers` — list of active tickers from `shared.tickers.TICKERS`
- `GET /api/v1/health` — service health check

**Agent endpoint**:
- `POST /api/v1/agent/chat` — stateless single-analysis; request: `{ticker, message}`, SSE events: `tool_call` → `thinking` → `thesis` → `done`

**Cross-cutting**: All `{ticker}` endpoints validate against `shared.tickers.TICKERS` and return 404 for unknown tickers. No API authentication in initial scope (read-only public market data).

## The Agentic Boundary

The agent ONLY interacts with FastAPI endpoints via tool calls. It never:
- Connects to ClickHouse or Qdrant directly
- Performs arithmetic (no RSI calculations, no percentage changes in agent code)
- Accesses raw data

This boundary is enforced by architecture (no DB credentials in the agent package) and by the system prompt.

## Multi-Timeframe Strategy

Daily bars are the single source of truth. Weekly and monthly bars are derived, not fetched separately:
- Avoids yfinance data divergence across intervals
- Full Dagster lineage: `ohlcv_raw → ohlcv_weekly → technical_indicators_weekly`
- Cross-dependency: `fundamentals` + `ohlcv_raw` → `fundamental_summary` (price-based ratios need close price)
- Replayable: re-run one Dagster asset to rebuild any timeframe

Technical indicators are computed independently on each timeframe's OHLCV table, supporting multi-timeframe analysis (daily RSI for short-term momentum, weekly RSI for trend health, monthly for regime context).

## Ticker Scope

10 US equities defined in `packages/shared/src/shared/tickers.py`. This is the single source of truth — all assets, schedules, and partitions derive from this list.

## Partitioning & Concurrency

All per-ticker Dagster assets use `StaticPartitionsDefinition` over the 10 tickers. Max 3 concurrent partition runs via `TagConcurrencyLimit("ticker", 3)` to stay within ClickHouse connection limits on the CX41.

## LLM Routing

LiteLLM proxy (v1.56.0, pinned) routes model requests:
- **Default**: Ollama Cloud (`https://ollama.com/v1`, OpenAI-compatible) via `OLLAMA_API_KEY` — no local model container needed on Hetzner, saves 6GB RAM
- **Override**: Claude API via `ANTHROPIC_API_KEY` for higher quality analysis
- Config: `litellm_config.yaml` at repo root, model alias `equity-agent/default`
- Agent code references only the alias — never a provider-specific model name or URL

## Infrastructure

- **Dev**: MacBook M4 → SSH tunnel → Hetzner ClickHouse (port 8123); LiteLLM on localhost:4000 (from Phase 5); Next.js on localhost:3001
- **Prod Backend**: Hetzner CX41 (16GB) → Docker Compose (ClickHouse 4GB + Dagster/FastAPI/Caddy/LiteLLM 12GB — no local Ollama, inference via Ollama Cloud)
- **Prod Frontend**: Vercel (Next.js 15, free tier) → calls FastAPI over HTTPS
- **HTTPS**: Caddy service in Docker Compose handles TLS termination (auto HTTPS via Let's Encrypt)
- **CI/CD**: GitHub Actions → backend: SSH → git pull → `make migrate` → docker compose up; frontend: Vercel auto-deploy on push to main
- **Rollback**: `make rollback` — SSHs to Hetzner, checks out `HEAD~1`, rebuilds Docker, verifies health (60s timeout with retries)
- **Health Monitoring**: `scripts/health-monitor.sh` runs every 15 min on Hetzner via cron — checks API `/health` + Docker service status, logs failures to `health-monitor.log`. Session-start hook reads this log and warns on failures. Install: `make monitor-install`. Check: `make monitor-log`.
- **Dagster UI**: Internal only in prod — access via SSH tunnel (`ssh -L 3000:localhost:3000 hetzner`), no auth configured
