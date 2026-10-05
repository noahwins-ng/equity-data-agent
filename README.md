# Equity Data Agent

An equity research platform for 10 US tech stocks: a daily data pipeline, a research terminal (charts, technicals, fundamentals, news), and an AI analyst that writes investment theses **without being allowed to invent or calculate a single number**. The pipeline does all the math; the LLM only reasons over pre-computed reports, and an eval checks every number it outputs.

[![Live demo](https://img.shields.io/badge/live%20demo-terminal.noahng.dev-success?style=for-the-badge)](https://terminal.noahng.dev)
![Prod](https://img.shields.io/badge/prod-live-success)

![Equity Data Agent live terminal](docs/screenshots/terminal-live.png)

## Features

| | |
|---|---|
| **Watchlist** | All 10 tickers with latest price, daily change, sparkline, and next data refresh |
| **Price charts** | Daily / weekly / monthly candles with SMA 20/50/200, Bollinger bands, RSI, MACD |
| **Technicals & fundamentals** | 17 indicators and 20+ ratios (P/E, margins, growth, balance-sheet health) per ticker |
| **News & filings** | Latest company news with sentiment; SEC 8-K earnings releases searchable from chat |
| **AI analyst chat** | Theses, comparisons, quick facts, event lookups, and open-ended "what's interesting?", with follow-ups |
| **Provenance** | Every chat answer shows which reports and sources it used; any number not found in them is marked † |

## The Idea

LLMs write well but are unreliable at arithmetic: one invented P/E ruins an otherwise plausible thesis. So the system is split into three roles that never overlap:

| Role | Layer | Does |
|---|---|---|
| Worker | Dagster | Fetches data, computes every indicator, ratio, and embedding |
| Interpreter | FastAPI | Turns database rows into plain-text reports |
| Executive | LangGraph | Reads the reports and writes the answer |

The agent has no database access and no calculator. It only sees report text.

**Does it matter?** Running the same model on the same 44 questions without the reports, it invents **87% of its numbers**. With them: **0%** (0 of 619). Reproduce with `uv run python -m agent.evals.baseline_eval`.

```mermaid
graph LR
    SRC[yfinance · Finnhub · SEC 8-K] --> DG[Dagster<br/>compute]
    DG --> CH[(ClickHouse)]
    DG --> QD[(Qdrant)]
    CH --> API[FastAPI<br/>reports]
    QD --> API
    API --> AG[LangGraph<br/>agent]
    AG --> UI[Next.js terminal]
    API --> UI
```

Full data flow: [`docs/architecture/system-overview.md`](docs/architecture/system-overview.md).

## AI Engineering

The agent is a LangGraph state machine, not one big prompt. Each question takes the cheapest path that can answer it:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> classify
    classify --> clarify: ambiguous<br/>(no ticker)
    classify --> synthesize: greeting /<br/>follow-up
    classify --> explore: "what's interesting?"
    classify --> plan: thesis, comparison,<br/>quick fact, focused
    plan --> gather: pick reports
    gather --> synthesize: call report +<br/>search tools
    explore --> synthesize
    clarify --> narrate
    synthesize --> narrate: structured card
    narrate --> [*]: streamed to UI
```

- **Routing.** `classify` sorts each question into 9 answer types. Ambiguous asks get a **clarifying question instead of a guess**; greetings and follow-ups **skip data fetching entirely**.
- **RAG over news + SEC filings.** Hybrid search (vector + keyword) with reranking, triggered only for event questions (lawsuits, buybacks, M&A); **retrieval MRR 0.94**.
- **Streaming.** The answer card fills in field by field as it's written, so **content appears ~2 seconds after the data is in**, not after the whole answer.
- **Memory.** A checkpointer keeps the conversation, so **follow-ups reuse earlier reports** instead of re-fetching.
- **Evals in CI.** **Every number traced back to a report**, a 44-question regression set, retrieval quality metrics, and LLM-judged answer quality.
- **Model routing + tracing.** LiteLLM routes to DeepSeek with prompt caching, a fallback chain, and a small model for routing steps. An hourly canary alerts if requests quietly fall back. Every request is traced in Langfuse. **About $0.002 per thesis.**

## Data Engineering

A Dagster asset graph runs daily after market close. Raw data lands in `equity_raw`, everything computed lands in `equity_derived`, and text is embedded for search:

```mermaid
graph LR
    subgraph Sources
        YF[yfinance]
        FH[Finnhub]
        SEC[SEC EDGAR]
    end
    subgraph "equity_raw"
        OHLCV[ohlcv_raw]
        FUND[fundamentals]
        CAL[earnings_calendar]
        NEWS[news_raw]
        ER[earnings_releases_raw]
    end
    subgraph "equity_derived"
        AGG[weekly / monthly bars]
        TECH[technical_indicators<br/>daily · weekly · monthly]
        FS[fundamental_summary]
    end
    subgraph "Qdrant"
        NE[news_embeddings]
        EE[earnings_embeddings]
    end
    YF --> OHLCV & FUND & CAL
    FH --> NEWS
    SEC --> ER
    OHLCV --> AGG --> TECH
    OHLCV --> TECH
    OHLCV & FUND --> FS
    NEWS --> NE
    ER --> EE
```

- **Layered warehouse.** Everything in `equity_derived` can be rebuilt from `equity_raw`, so only the raw layer has to be durable.
- **Tests on the data.** 38 asset checks with real financial bounds (RSI 0-100, P/E band, MACD coherence), not just "not null". They caught two P/E formula bugs that passed code review, including a P/E of 28,545.
- **Input contracts.** Each source is schema-validated (Pandera) before writing; bad rows go to an auditable reject table instead of disappearing.
- **Idempotent by design.** Every table and migration is safe to re-run; a monthly full re-fetch heals stock-split and dividend adjustments through the same dedup path.
- **Data observability.** Per-ticker freshness checks and a Grafana data-health dashboard.

## Production

- Hetzner (Docker Compose) backend + Vercel frontend, behind a Cloudflare tunnel.
- Deploys verify the running commit and auto-rollback on failure; unhealthy services restart themselves.
- Monitoring with Sentry, Grafana, Langfuse and Discord alerts, plus a [failure runbook](docs/guides/ops-runbook.md).

## Results

| Check | Result |
|---|---|
| Invented numbers (grounded vs. ungrounded) | 0% vs. 87% |
| Golden-set regression (correct tools / grounded answer) | 44 of 44 |
| Retrieval: right source ranked first (MRR) | 0.94 |

Latest model benchmark: [`docs/model-bench-2026-07.md`](docs/model-bench-2026-07.md) (earlier: [2026-04](docs/model-bench-2026-04.md)).

## Problems I Hit

- **Green checks, wrong model.** A hidden LangChain parameter made OpenRouter filter out every provider, so LiteLLM silently served the fallback model with a 200. Fixed the parameter, and now every fallback fire raises a Sentry alert plus an hourly canary.
- **The "hallucination" was the scorer's.** The eval flagged the agent for inventing numbers on news questions. The real cause: the scorer couldn't read `$2.5T` in the report, so the agent's correct "$2.5 trillion" looked unsupported. Fixed the scorer, not the prompt.
- **A deploy that never ran.** A GitHub outage dropped the merge's push event: zero deploy runs, no red signal, prod one commit behind. Caught only because shipping asserts the running commit SHA on the server.

## Known Limits

- **Small universe.** 10 tickers; past ~100 the warehouse partitioning would need to change.
- **Free data source.** yfinance has no SLA; real use needs a paid feed.
- **Batch only.** Transforms rebuild daily; intraday data would need incremental and streaming paths.
- **Small benchmark.** 44 questions is a directional signal, not a leaderboard.
- **No fine-tuning.** Behaviour comes from prompts and routing.

## Try It

At **[terminal.noahng.dev](https://terminal.noahng.dev)**, ask things like:

- `Give me a balanced thesis on NVDA`
- `Compare MSFT and GOOGL`
- `Is MU overbought?`
- `Anything on the INTC lawsuit?`
- `What looks interesting right now?`

Tickers: NVDA, AAPL, MSFT, GOOGL, AMZN, META, TSLA, MU, AMD, INTC. Data updates daily after market close.

## Run Locally

Needs Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker, Node, and an `OPENROUTER_API_KEY` (news search also needs Qdrant Cloud and Cohere keys).

```bash
git clone https://github.com/noahwins-ng/equity-data-agent.git
cd equity-data-agent
make setup && $EDITOR .env

# local ClickHouse + sample data (30 days, 3 tickers)
docker run -d -p 8123:8123 clickhouse/clickhouse-server:24-alpine
make migrate && make seed

# one terminal each
make dev-litellm
make dev-api
make dev-dagster
make dev-frontend

uv run python -m agent analyze NVDA
```

Checks: `make lint` · `make test` · `uv run python -m agent.evals`

## Where to Look in the Code

| What | Where |
|---|---|
| Agent graph | [`packages/agent/src/agent/graph.py`](packages/agent/src/agent/graph.py) |
| Intent router + clarify step | [`packages/agent/src/agent/intent.py`](packages/agent/src/agent/intent.py) |
| Number-grounding check | [`packages/agent/src/agent/evals/hallucination.py`](packages/agent/src/agent/evals/hallucination.py) |
| Retrieval (hybrid search + rerank) | [`packages/shared/src/shared/retrieval.py`](packages/shared/src/shared/retrieval.py) |
| Data asset checks | [`packages/dagster-pipelines/.../asset_checks/`](packages/dagster-pipelines/src/dagster_pipelines/asset_checks) |
| Source contracts | [`packages/shared/src/shared/contracts.py`](packages/shared/src/shared/contracts.py) |

## Screenshots

| RAG answer with sources | Langfuse trace |
|---|---|
| <img src="docs/screenshots/rag-provenance.png" alt="RAG provenance" width="380"> | <img src="docs/screenshots/langfuse-trace.png" alt="Langfuse trace" width="380"> |
| **Dagster lineage** | **Data checks** |
| <img src="docs/screenshots/dagster-lineage.svg" alt="Dagster lineage" width="380"> | <img src="docs/screenshots/dagster-asset-checks.png" alt="Dagster asset checks" width="380"> |

## Stack

Next.js · FastAPI · LangGraph · LiteLLM · Dagster · ClickHouse · Qdrant · Langfuse · Docker Compose · Hetzner · Vercel

## Docs

- [`docs/INDEX.md`](docs/INDEX.md): map of all docs
- [`docs/decisions/`](docs/decisions/): why X over Y (start with [ADR-003](docs/decisions/003-intelligence-vs-math.md))
- [`docs/retros/`](docs/retros/): phase retrospectives

---

Built by Noah Ng. [MIT](LICENSE).
