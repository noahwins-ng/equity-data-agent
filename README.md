# Equity Data Agent

> Production-style AI/data engineering portfolio project for US equities. The
> agent can write an investment thesis, but it is not allowed to invent or
> calculate financial numbers: Dagster computes them, FastAPI prints them into
> report strings, and LangGraph reasons over those reports. A regression eval
> checks every numeric literal in the answer against the retrieved reports.

[![Live demo](https://img.shields.io/badge/live%20demo-equity--data--agent--ynr2.vercel.app-success?style=for-the-badge)](https://equity-data-agent-ynr2.vercel.app)

![Equity Data Agent live terminal](docs/screenshots/terminal-live.png)

## Try It

Live app: **[equity-data-agent-ynr2.vercel.app](https://equity-data-agent-ynr2.vercel.app)**

Good demo prompts:

- `Give me a balanced thesis on NVDA`
- `Compare MSFT and GOOGL`
- `What's the news sentiment on TSLA?`

The current universe is 10 US equities — a deliberately semis/tech-concentrated
set: NVDA, AAPL, MSFT, GOOGL, AMZN, META, TSLA, MU, AMD, and INTC. The
concentration is a scope choice, not a limitation: it keeps every ticker in
sectors the agent can reason about with shared context (AI/data-center demand,
the semiconductor cycle) rather than spreading thin across unrelated industries.
Data ingests daily after market close.

## Why This Exists

LLMs are useful at synthesis, but unreliable at financial arithmetic. A model
that fabricates RSI, miscomputes YoY growth, or invents a P/E ratio can poison
an otherwise plausible thesis.

This project treats that as an architecture problem:

| Role | Layer | Responsibility |
|---|---|---|
| Worker | Dagster | Fetch data and compute indicators, ratios, aggregations, and embeddings |
| Interpreter | FastAPI | Query ClickHouse/Qdrant and format human-readable report strings |
| Executive | LangGraph | Read reports, choose a response shape, and synthesize the answer |

The agent has no database client, no calculator tool, and no access to raw
tables. It only sees the report text FastAPI gives it.

## What This Demonstrates

| Area | Proof |
|---|---|
| AI engineering | LangGraph agent that classifies a question into one of 9 response shapes, plans tools, and synthesizes an answer; semantic news search over Qdrant with deterministic targeted-event routing; multi-turn conversation continuity; LiteLLM provider routing with fallback chains; prompt-version hashing on every Langfuse trace; a four-eval suite (numeric grounding, golden-set regression, tool-call correctness, dialogue quality) |
| Data engineering | Medallion-layered ClickHouse warehouse (`equity_raw` -> `equity_derived`) across 27 idempotent migrations; Dagster asset graph computing 17 technical-indicator columns over three timeframes and 20+ fundamental ratios; 32 domain-bounded asset checks (the dbt-test equivalent) plus z-score anomaly detection; a rejected-row sink; Qdrant news embeddings |
| Product engineering | Next.js 16 app with watchlist, ticker detail, charting, fundamentals, news, and persistent chat panel |
| Production ops | Hetzner Docker Compose backend, Vercel frontend, Cloudflare named tunnel, health checks, autoheal, observability-smoke deploy gate with auto-rollback, alerts, runbooks |
| Engineering process | 22 ADRs, 7 phase retros, 396 merged PRs, 1,200+ tests, security scanners, deploy gates, model bench history |

Badges and counts are supporting evidence, not the point:

![Phases](https://img.shields.io/badge/phases-7%2F7%20complete-2ea44f)
![Tests](https://img.shields.io/badge/tests-1200%2B%20passing-2ea44f)
![ADRs](https://img.shields.io/badge/ADRs-22-1f6feb)
![Golden set](https://img.shields.io/badge/golden__set-41%20questions-1f6feb)
![Merged PRs](https://img.shields.io/badge/merged%20PRs-396-1f6feb)
![Prod](https://img.shields.io/badge/prod-live-success)

## Hallucination Resistance

The precise guarantee is:

> The agent should not introduce numeric financial claims that are absent from
> the report strings it retrieved.

That is narrower, and more useful, than claiming the model can never be wrong.
The project separates two concerns:

| Layer | Guarantee |
|---|---|
| Provenance | Numeric claims must be copied from retrieved reports, not calculated by the LLM |
| Correctness | Evals and judge scores track whether the cited numbers answer the actual question |

Enforcement:

- **Architecture**: [ADR-003](docs/decisions/003-intelligence-vs-math.md) keeps arithmetic in Dagster and report templates, not the agent.
- **Prompt contract**: the system prompt requires every numeric claim to cite its report source.
- **Eval suite**: four evals guard the agent, runnable in CI —
  numeric grounding ([`hallucination.py`](packages/agent/src/agent/evals/hallucination.py) extracts every numeric literal and checks it against the retrieved reports),
  golden-set regression ([`golden_set.py`](packages/agent/src/agent/evals/golden_set.py): 41 curated questions across all 10 tickers plus a cross-ticker case),
  tool-call correctness ([`tool_calls.py`](packages/agent/src/agent/evals/tool_calls.py)),
  and dialogue quality ([`dialogue_eval.py`](packages/agent/src/agent/evals/dialogue_eval.py): a 5-axis LLM-as-judge over multi-turn fixtures).
  A separate RAG eval ([`news_search_eval.py`](packages/agent/src/agent/evals/news_search_eval.py)) checks that semantic news search fires only on the questions that warrant it.

Most recent clean-window run of the 41-question golden set, production model (`groq/llama-3.3-70b-versatile`):

| Metric | Result |
|---|---|
| tool_call_ok | 40/40 |
| hallucination_ok | 38/40 |
| cosine vs. reference | 0.41 |

Both hallucination flags were `*-news-sentiment` questions, and both turned out to
be **false positives in the scorer** — it was splitting glued magnitude units
(e.g. `$2.5T`, `$14B`) and treating the fragment as an unsupported number. The
fix sharpened the scorer rather than loosening the contract
([#411](https://github.com/noahwins-ng/equity-data-agent/pull/411)). The point is
that the eval itself stays under test, not just the agent. An LLM-as-judge scores
answers on faithfulness, structure, correctness, and analyst logic as a softer
quality signal; cosine similarity against the reference responses is the
harder-to-game cross-check.

The eval earns its place by disqualifying production-candidate models: Qwen3-32B
(fabricated numbers and leaked `<think>` blocks), Gemini-2.5-Flash-Lite (grounding
and tool-call regressions, plus a 20-request/day free-tier ceiling), and
GPT-OSS-120B — which passed the smaller bench, then fabricated a number once the
golden set expanded to cover more news-sentiment questions. The full candidate
comparison and the quality-before-capacity selection rationale live in
[`docs/model-bench-2026-04.md`](docs/model-bench-2026-04.md).

## Architecture

```mermaid
graph LR
    subgraph Sources
        YF[yfinance]
        NEWS[Finnhub /company-news]
    end

    subgraph Dagster
        OHLCV[ohlcv_raw]
        FUND[fundamentals]
        AGG[weekly/monthly bars]
        TECH[technical indicators]
        FSUM[fundamental summary]
        NRAW[news_raw]
        EMB[news embeddings]
    end

    subgraph Storage
        CH[(ClickHouse)]
        QD[(Qdrant Cloud)]
    end

    subgraph FastAPI
        REP[report endpoints]
        DATA[data endpoints]
        SSE[agent chat SSE]
    end

    subgraph Agent
        GRAPH[classify -> plan -> gather -> synthesize]
        TOOLS[HTTP report tools]
    end

    subgraph Frontend
        UI[Next.js terminal UI]
    end

    YF --> OHLCV --> CH
    YF --> FUND --> CH
    NEWS --> NRAW --> CH
    NRAW --> EMB --> QD
    OHLCV --> AGG --> CH
    CH --> TECH --> CH
    OHLCV --> FSUM
    FUND --> FSUM
    FSUM --> CH
    CH --> REP
    CH --> DATA
    QD --> REP
    REP --> TOOLS --> GRAPH
    GRAPH --> SSE --> UI
    DATA --> UI
```

The `classify -> plan -> gather -> synthesize` flow above is the spine; the agent
does more than the four boxes show:

- **Intent routing.** `classify` sorts each question into one of 9 response
  shapes (thesis, quick-fact, comparison, fundamental, technical, news,
  conversational, follow-up, exploration) with a heuristic-first, LLM-fallback
  classifier. Ambiguous asks (missing ticker, missing second ticker for a
  comparison, a follow-up with no prior turn) route to a deterministic clarify
  step instead of guessing.
- **Semantic news search.** A deterministic keyword gate fires RAG over Qdrant
  only for targeted events (litigation, executive changes, buybacks, recalls,
  antitrust, M&A, layoffs, SEC actions); generic "what's the news?" asks stay on
  the cheaper canned digest. Retrieved sources stream back to the UI as
  provenance.
- **Multi-turn continuity.** A checkpointer carries a compact transcript across
  turns with per-intent history budgets, so follow-ups reuse prior reports
  without re-fetching.
- **Provider routing and tracing.** LiteLLM abstracts every provider (one-line
  model swap, fallback chains, smaller models for classify/plan); a 10-char hash
  over all system prompts and the tool registry is stamped on each Langfuse
  trace, so prompt-quality trends are reviewable over time.

Read the fuller system description in
[`docs/architecture/system-overview.md`](docs/architecture/system-overview.md).

## Data Engineering

The warehouse follows the standard data-engineering patterns under
Dagster-native names:

- **Medallion layering.** Two ClickHouse databases mirror a bronze ->
  silver/gold split: `equity_raw` holds ingested source data (OHLCV,
  fundamentals, news), `equity_derived` holds everything computed from it —
  multi-timeframe bars, 17 technical-indicator columns across daily/weekly/monthly
  (RSI, MACD, SMA/EMA, Bollinger Bands, ADX, ATR, OBV), and 20+ fundamental
  ratios. Every derived table is rebuildable from raw, so the raw layer is the
  only thing that must be durable. (Strict bronze would persist unparsed API
  payloads; skipped deliberately at this volume.)
- **Tests on the data (dbt-test equivalent).** 32 domain-bounded
  [asset checks](packages/dagster-pipelines/src/dagster_pipelines/asset_checks)
  — the Dagster-native analogue of dbt tests — assert real financial bounds, not
  just non-null, and add z-score volume-spike and price-gap anomaly detection on
  top. They earn their keep: a `pe_in_band` check caught two distinct P/E formula
  bugs that both passed human code review (a near-zero-EPS blowup to a P/E of
  28,545, and a quarterly ratio dividing full market cap by single-quarter income
  instead of TTM). Declining dbt at this scale was a deliberate call
  ([ADR-022](docs/decisions/022-decline-dbt-adoption-at-current-scale.md)).
- **Data observability.** Freshness, volume/distribution, and lineage are
  first-class: per-ticker freshness/staleness checks on OHLCV and news, volume
  and distribution trends on a Grafana data-health dashboard
  ([`observability/grafana/dashboards/data-health.json`](observability/grafana/dashboards/data-health.json)),
  and the Dagster asset graph as lineage. Import-time registry asserts keep the
  ticker universe, metadata, and news-relevance config from drifting. Dropped
  source rows land in an `equity_raw.ingest_rejects` sink (reason, detail,
  payload; 90-day TTL) so a bad URL or NaN period is auditable rather than silent.
- **Idempotency.** All tables are `ReplacingMergeTree` with `FINAL`/`argMax`
  read paths and append-only, re-runnable migrations (27 of them), so re-ingesting
  a day or restating a quarter overwrites cleanly rather than duplicating. A daily
  incremental OHLCV pull is paired with a monthly full 2-year re-fetch that heals
  split/dividend history splices through the same dedup path.

### Where this design breaks at scale

The system is deliberately scoped to 10 tickers. Several choices are right at
this size but would have to change with volume — naming them is the point:

- **Partition cardinality.** Tables `PARTITION BY ticker`, which is ideal at
  10-15 values (ticker churn is an instant `DROP PARTITION` metadata op) but
  degrades past ~100 partitions; a larger universe would switch to a time-based
  scheme like `toYYYYMM(date)`.
- **Market-data vendor.** yfinance is fine for a portfolio-scale daily pull but
  carries no SLA; production scale means a paid feed (Polygon, databento, or a
  direct exchange feed).
- **Incremental / streaming.** Transforms full-rebuild today. Higher volume
  would call for incremental models, and intraday data would need a streaming
  ingest path rather than a daily batch.
- **Single-node ClickHouse.** One node serves this comfortably; horizontal
  growth means a multi-node, sharded ClickHouse cluster.

## Screenshots

**Live terminal** - watchlist, ticker detail, charting, fundamentals, news, and
chat in one persistent workspace.

![Live terminal](docs/screenshots/terminal-live.png)

**CLI thesis** - the same agent can produce a structured thesis from the
terminal.

![CLI thesis](docs/screenshots/cli-thesis.png)

**Langfuse trace** - request-level trace with LangGraph spans, model metadata,
token usage, and eval scores.

![Langfuse trace](docs/screenshots/langfuse-trace.png)

**Dagster asset graph** - asset lineage from raw OHLCV to derived indicators.

![Dagster lineage](docs/screenshots/dagster-lineage.svg)

## Stack

| Tier | Technology |
|---|---|
| Frontend | Next.js 16, React 19, Tailwind, TradingView Lightweight Charts, Vercel |
| API | FastAPI, SSE, Pydantic settings, SlowAPI rate limits, Sentry |
| Agent | LangGraph, LangChain, LiteLLM, Groq default, Gemini override, Langfuse |
| Data | Dagster, ClickHouse, Qdrant Cloud, yfinance, Finnhub |
| Infra | uv workspaces, Docker Compose, Hetzner CX41, Cloudflare named tunnel |
| Quality | pytest, Ruff, Pyright, npm lint/typecheck, pip-audit, bandit, gitleaks, Trivy |

## Production Notes

The backend runs on a Hetzner VPS with Docker Compose. The frontend runs on
Vercel. FastAPI is exposed through a Cloudflare named tunnel at a stable
`api.<domain>` hostname; port 8000 is not open to the public internet.

Production hardening includes:

- SOPS-encrypted secrets and deploy-time decryption.
- SHA, Dagster-load, and observability-smoke deploy gates, with auto-rollback to the previous SHA if the smoke check fails.
- Idempotent ClickHouse migrations on deploy.
- Health checks for API, ClickHouse, Qdrant, and service identity, plus an autoheal container that restarts services that go unhealthy without exiting.
- UptimeRobot, Sentry, Langfuse, Prometheus, Grafana, cAdvisor, node_exporter, and Dozzle.
- Discord alerts for Dagster failures, container events, and infrastructure alerts.
- Failure-mode runbook in [`docs/guides/ops-runbook.md`](docs/guides/ops-runbook.md).

The detailed tradeoffs live in [`docs/decisions/`](docs/decisions/), especially:

- [ADR-003: Intelligence vs. Math](docs/decisions/003-intelligence-vs-math.md)
- [ADR-007: Minimal Agent Graph First](docs/decisions/007-minimal-agent-graph-first.md)
- [ADR-011: LLM Routing](docs/decisions/011-llm-routing-groq-default-gemini-override.md)
- [ADR-017: Public Chat, No Auth](docs/decisions/017-public-chat-truly-public-no-auth.md)
- [ADR-018: Cloudflare Tunnel Ingress](docs/decisions/018-cloudflare-quick-tunnel-for-https-ingress.md)

## Quick Start

Prerequisites: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker, Node,
and API keys for the providers you want to use.

```bash
git clone https://github.com/noahwins-ng/equity-data-agent.git
cd equity-data-agent
make setup
$EDITOR .env

# terminals
make dev-litellm
make dev-api
make dev-dagster
make dev-frontend

# run a local thesis against available data
uv run python -m agent analyze NVDA
```

Useful checks:

```bash
make lint
make test
npm --prefix frontend run lint
npm --prefix frontend run typecheck
uv run python -m agent.evals
```

## Documentation

Start here:

- [`docs/INDEX.md`](docs/INDEX.md) - documentation map.
- [`docs/project-requirement.md`](docs/project-requirement.md) - current requirements and architecture spec.
- [`docs/architecture/system-overview.md`](docs/architecture/system-overview.md) - system boundaries and data flow.
- [`docs/decisions/`](docs/decisions/) - ADRs.
- [`docs/retros/`](docs/retros/) - phase retrospectives.
- [`docs/guides/ops-runbook.md`](docs/guides/ops-runbook.md) - production failure-mode catalog.
