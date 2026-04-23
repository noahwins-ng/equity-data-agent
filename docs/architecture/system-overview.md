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
- `fundamental_summary` — 15 derived ratios (P/E, EV/EBITDA, margins, YoY growth, etc.) from fundamentals + ohlcv_raw. Quarterly P/E uses TTM (trailing-four-quarter) net_income; P/E is nulled out (N/M convention) when `|EPS| < $0.10`.

**Data quality**: 25 Dagster `@asset_check`s registered across 10 assets (QNT-68 + QNT-93) — row counts, null/bound checks, RSI 0-100, MACD/signal coherence, P/E and margin domain bounds, news headline/URL integrity, Qdrant vector count/dimension/orphan checks. Checks run inline with the asset and surface in the Dagster UI. Asset checks earned their keep in Phase 4: `news_embeddings_vector_count_matches_source` caught QNT-120 (cross-ticker Qdrant ID collision) within 24h of shipping.

All tables use `ReplacingMergeTree` for idempotency. FastAPI queries **must** use `SELECT ... FROM table FINAL` for consistent reads (see ADR-001).

**Qdrant Cloud** (managed, free tier):
- `equity_news` collection — news embeddings via **Qdrant Cloud Inference** (ADR-009: model `sentence-transformers/all-minilm-l6-v2`, 384-dim, embedded server-side so the Dagster run-worker stays I/O-bound). Point ID = `blake2b(f"{ticker}:{url_id}", digest_size=8)` — namespaced by ticker (QNT-120) to match ClickHouse's `(ticker, url)` composite key so cross-mentioned URLs land as one point per ticker. Payload: `{ticker, published_at, url, headline, source}`; indexed on `ticker` (keyword) and `published_at` (integer). Re-embed window: trailing 7 days of `fetched_at` per ticker on every tick.

## API Endpoint Categories

**Report endpoints** (text strings, consumed by LangGraph agent):
- `GET /api/v1/reports/technical/{ticker}` — human-readable technical analysis
- `GET /api/v1/reports/fundamental/{ticker}` — human-readable fundamental summary
- `GET /api/v1/reports/news/{ticker}` — recent news summary (top-N headlines + narrative)
- `GET /api/v1/reports/summary/{ticker}` — combined text overview for agent "at a glance" tool

**Data endpoints** (JSON, consumed by Next.js frontend):
- `GET /api/v1/ohlcv/{ticker}?timeframe=daily|weekly|monthly` — `{time, open, high, low, close, adj_close, volume}[]` — `time` is ISO date `"YYYY-MM-DD"`. Chart renders `adj_close` as candlestick close to avoid split artifacts.
- `GET /api/v1/indicators/{ticker}?timeframe=daily|weekly|monthly` — `{time, rsi_14, macd, macd_signal, macd_hist, sma_20, sma_50, ema_12, ema_26, bb_upper, bb_middle, bb_lower}[]` — `null` during warm-up
- `GET /api/v1/fundamentals/{ticker}` — `{ticker, period_end, period_type, pe_ratio, ev_ebitda, ...}[]` for ratios table
- `GET /api/v1/dashboard/summary` — `[{ticker, price, daily_change_pct, rsi_14, rsi_signal, trend_status}]` all tickers in one call. `price` = latest available close (not adj_close). `rsi_signal`: overbought/neutral/oversold. `trend_status`: bullish/bearish/neutral (close vs SMA-50).

**Search endpoints** (JSON, consumed by frontend and agent):
- `GET /api/v1/search/news?ticker=NVDA&query=earnings` — Qdrant semantic search over news embeddings. Returns top-N `{headline, source, date, score, url}` ranked by cosine similarity. Query string is embedded server-side via Qdrant Cloud Inference using the same model as `news_embeddings` so query-time and embed-time vectors share one vector space. Returns `[]` (HTTP 200) if Qdrant is unreachable or no matches — frontend renders "no news" the same way as "service down".

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

LiteLLM proxy (v1.81.14-stable, pinned) routes model requests (see ADR-011):
- **Default**: Groq (`https://api.groq.com/openai/v1`, llama-3.3-70b-versatile) via `GROQ_API_KEY` — email-only free tier covers Phase 5 dev + steady-state portfolio demos, ~500 tok/s inference. No local model container on Hetzner.
- **Override**: Google AI Studio Gemini 2.5 Flash via `GEMINI_API_KEY` — free-tier quality override (15 RPM / 1500 RPD, no credit card) for the hero demo thesis and README screenshot. Eval harness (QNT-67) logs a per-provider column so Groq↔Gemini becomes a deliberate eval axis. (Pro was the original pick in ADR-011 but returned `limit: 0` on free tier at first live test — see QNT-123 and ADR-011 §Revision history.)
- Config: `litellm_config.yaml` at repo root, model alias `equity-agent/default`
- Agent code references only the alias — never a provider-specific model name or URL

## Infrastructure

- **Dev**: MacBook M4 → SSH tunnel → Hetzner ClickHouse (port 8123); LiteLLM on localhost:4000 (from Phase 5); Next.js on localhost:3001
- **Prod Backend**: Hetzner CX41 (16GB) → Docker Compose (ClickHouse 4GB + Dagster/FastAPI/Caddy/LiteLLM 12GB — no local model inference; LLM calls go to Groq / Gemini via LiteLLM)
- **Prod Frontend**: Vercel (Next.js 15, free tier) → calls FastAPI over HTTPS
- **HTTPS**: Caddy service in Docker Compose handles TLS termination (auto HTTPS via Let's Encrypt)
- **CI/CD**: GitHub Actions → backend: SSH → git pull → `make migrate` → docker compose up, then two hard gates (QNT-88/89): assert `git rev-parse HEAD` equals the merged commit SHA and assert the Dagster definitions module loads with the expected asset/check/schedule counts. Frontend: Vercel auto-deploy on push to main.
- **Rollback**: `make rollback` — SSHs to Hetzner, checks out `HEAD~1`, rebuilds Docker, verifies health (60s timeout with retries)
- **Health Monitoring**: `scripts/health-monitor.sh` runs every 15 min on Hetzner via cron — checks API `/health` + Docker service status, logs failures to `health-monitor.log`. Session-start hook reads this log and warns on failures. Install: `make monitor-install`. Check: `make monitor-log`.
- **Dagster UI**: Internal only in prod — access via SSH tunnel (`ssh -L 3000:localhost:3000 hetzner`), no auth configured
