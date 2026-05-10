# Equity Data Agent

[![Live demo](https://img.shields.io/badge/live%20demo-equity--data--agent--ynr2.vercel.app-success?style=for-the-badge)](https://equity-data-agent-ynr2.vercel.app)

![Equity Data Agent — live terminal](docs/screenshots/terminal-live.png)

> Production AI research tool for US equities, deployed live at **[equity-data-agent-ynr2.vercel.app](https://equity-data-agent-ynr2.vercel.app)**. Solo portfolio build — **260+ merged PRs · 19 ADRs · 700+ tests · 7 shipped phases** — designed to demonstrate end-to-end ownership of a non-trivial AI system: data engineering, LLM safety, agent design, frontend, and production ops.
>
> **Product claim: the LLM never does math.** Every number in every thesis is pre-computed by Dagster, served as a human-readable report by FastAPI, and only *interpreted* by the LangGraph agent — so hallucinated financials are architecturally impossible. [→ how](#hallucination-resistance)

![Phases](https://img.shields.io/badge/phases-7%2F7%20complete-2ea44f)
![Tests](https://img.shields.io/badge/tests-716%20passing-2ea44f)
![ADRs](https://img.shields.io/badge/ADRs-19-1f6feb)
![Hallucination](https://img.shields.io/badge/hallucination__ok-22%2F22-2ea44f)
![Prod](https://img.shields.io/badge/prod-live-success)

![pip-audit](https://img.shields.io/badge/pip--audit-clean-2ea44f)
![bandit](https://img.shields.io/badge/bandit-clean-2ea44f)
![gitleaks](https://img.shields.io/badge/gitleaks-clean-2ea44f)
![npm audit](https://img.shields.io/badge/npm%20audit-clean-2ea44f)
![Trivy weekly](https://img.shields.io/badge/Trivy-weekly-1f6feb)

---

## What's demonstrated

| Area | Concrete proof |
|---|---|
| **Full-stack** | Next.js 16 (App Router, SSG + Vercel Deploy Hook, SSE chat) · FastAPI (async, OpenAPI, per-IP rate-limit + token budget + fail-closed circuit breaker) · TypeScript types auto-generated from the OpenAPI schema |
| **LLM engineering** | Intent-routed LangGraph (`classify → plan → gather → synthesize`) supporting 7 response shapes (thesis / quick-fact / comparison / conversational + focused-analysis: fundamental / technical / news_sentiment) · LiteLLM routing across Groq + Gemini with a free-tier-first policy · Hallucination eval harness (regex-verified, **22/22 pass** on the 22-question golden set) · Cross-model bench with `git`-tracked history |
| **Data engineering** | Dagster asset graph (**10 assets · 30 domain-bounded asset checks · 6 schedules**) · ClickHouse + Qdrant Cloud · Idempotent migrations re-applied every deploy · Multi-timeframe aggregation (daily → weekly → monthly) · Per-ticker news relevance filter at ingest |
| **System design** | **19 ADRs** documenting every non-obvious choice (storage, agent shape, LLM routing, deploy ingress, public-chat threat model) — written at decision time, not retrofitted |
| **Production ops** | Bespoke Docker Compose on a Hetzner VPS · 7 phase retros + a living failure-mode runbook · Multi-layer observability (Sentry + Langfuse + Prometheus + Grafana + cAdvisor + node_exporter + Dozzle) · Discord alerting on Dagster materialization failures + Docker container events ≤30s · UptimeRobot probe on `/api/v1/health` |
| **CI/CD + security** | SOPS-encrypted secrets · SHA-identity gate + Dagster-load gate on every deploy · Layered scanner suite (pip-audit + bandit + gitleaks + npm audit + weekly Trivy image CVE scan) · Dependabot with grouped bumps + a waivers file with rationale |
| **Testing** | **716 pytest tests** (unit + real-ClickHouse integration) · Endpoint p50/p95/p99 latency baseline with error-rate gate · Asset-check domain bounds that have caught real arithmetic bugs that passed code review |

The 10-ticker universe (NVDA, AAPL, MSFT, GOOGL, AMZN, META, TSLA, JPM, V, UNH) ingests daily at 17:00 ET. All 7 planned phases are complete; remaining work lives in a perpetual **Ops & Reliability** queue.

---

## Why this exists

Off-the-shelf LLMs hallucinate financial numbers — fabricated RSI values, invented YoY growth, miscomputed P/E ratios. One hallucinated number poisons the rest of the thesis.

**This project enforces a hard architectural boundary** ([ADR-003](docs/decisions/003-intelligence-vs-math.md)):

- **Dagster** computes 100% of the math (technical indicators, fundamental ratios, multi-timeframe aggregation, news embeddings) and writes idempotently to ClickHouse + Qdrant.
- **FastAPI** turns database rows into pre-formatted, human-readable report strings — `"RSI 72.3 — above the 70 overbought threshold, momentum extended"`.
- **LangGraph agent** receives those strings as tool output and reasons over them. It never sees a raw row, never connects to the database, and never performs arithmetic.

A regression suite regexes every numeric literal out of the agent's thesis and asserts it appears verbatim in one of the report strings the agent received. Mismatches fail CI — see [Hallucination resistance](#hallucination-resistance) below.

---

## Hallucination resistance

This is the product's main claim. The contract:

> **The agent's thesis cannot contain a number that was not pre-computed by Dagster and printed verbatim into a FastAPI report string.**

Three independent enforcement layers:

1. **Architecture** ([ADR-003](docs/decisions/003-intelligence-vs-math.md)) — the agent has no database client, no calculator tool, no arithmetic primitives. It physically cannot compute a number; the only numbers it sees are the ones FastAPI already chose to print.
2. **System prompt** — `SYSTEM_PROMPT` in [`packages/agent/src/agent/prompts/`](packages/agent/src/agent/prompts/) ratifies the rule: "every numeric claim must cite the report it came from; never derive a new number".
3. **Eval harness** — [`packages/agent/src/agent/evals/hallucination.py`](packages/agent/src/agent/evals/hallucination.py) regexes every numeric literal from a generated thesis and asserts each appears verbatim in one of the report strings the agent received as tool output. Run on a 22-question golden set covering all 10 portfolio tickers across every supported intent shape (thesis · quick-fact · comparison · conversational · fundamental · technical · news-sentiment); results land in [`packages/agent/src/agent/evals/history.csv`](packages/agent/src/agent/evals/history.csv) so prompt-version quality is `git log -p`-visible.

Most recent benches (May 9, 2026, full 22-record set):

| Model | hallucination_ok | tool_call_ok | judge | cos | avg latency | role |
|---|---|---|---|---|---|---|
| **Llama-3.3-70B** | **22/22** | **22/22** | **7.91** | 0.417 | 14.6s | production default |
| **Llama-4-Scout-17B** | **22/22** | **22/22** | 7.14 | 0.411 | **3.8s** | calibrated fallback (4× faster) |
| GPT-OSS-120B | 21/22 ✗ | 22/22 | 5.14 | 0.438 | 21.7s | disqualified (hallucinated on `tsla-news`) |
| GPT-OSS-20B | 22/22 | 22/22 | 4.09 | 0.398 | 27.4s | clean but lowest quality + slowest |

The 120B finding is exactly why the eval gate exists: GPT-OSS-120B was 16/16 clean on the April 16-record bench, so on the easier surface it looked qualified. Adding 6 news-sentiment / focused-analysis questions in May surfaced one fabricated number — the model would have shipped to prod with no signal that the contract was broken. This is the failure mode ADR-003 prevents architecturally and the eval catches behaviorally; both layers paid for themselves on this run.

The April 2026 cross-model comparison ([`docs/model-bench-2026-04.md`](docs/model-bench-2026-04.md)) is preserved as a frozen-set artifact against the pre-expansion 16 records (also covers Qwen3-32B, Gemma3-27B, Gemini-2.5-Flash-Lite — Gemma3-27B has since been deprecated by Google).

[ADR-012](docs/decisions/012-domain-conventions-in-reports-not-prompts.md) extends the contract: *canonical thresholds* (RSI 70/30, P/E rich/cheap bands) live in the **report templates**, never in the prompt — so the model can quote them without "leaking" prior knowledge.

---

## Architecture

```mermaid
graph LR
    subgraph Sources
        YF[yfinance]
        NEWS[Finnhub /company-news]
    end

    subgraph "Dagster — Calculation"
        OHLCV[ohlcv_raw]
        FUND[fundamentals]
        AGG[weekly / monthly]
        TECH[technical_indicators<br/>daily / weekly / monthly]
        FSUM[fundamental_summary]
        NRAW[news_raw]
        EMB[news_embeddings]
    end

    subgraph Storage
        CH[(ClickHouse)]
        QD[(Qdrant Cloud)]
    end

    subgraph "FastAPI — Interpretation"
        REP[Report endpoints<br/>/reports/...]
        DAT[Data endpoints<br/>/ohlcv, /indicators, /quote]
        SSE[Agent SSE<br/>/agent/chat]
    end

    subgraph "LangGraph — Reasoning"
        AGENT[classify → plan → gather → synthesize]
        TOOLS[get_*_report tools]
    end

    subgraph "Next.js — Presentation"
        FE[Watchlist · Detail · Chat]
    end

    YF --> OHLCV --> CH
    YF --> FUND --> CH
    NEWS --> NRAW --> CH
    NRAW --> EMB --> QD
    CH --> AGG --> CH
    CH --> TECH --> CH
    OHLCV --> FSUM
    FUND --> FSUM
    FSUM --> CH
    CH --> REP
    CH --> DAT
    QD --> REP
    REP --> TOOLS --> AGENT
    AGENT --> SSE --> FE
    DAT --> FE
```

Three roles, no overlap:

| Role | Layer | What it does | Where it lives |
|---|---|---|---|
| **Worker** | Dagster | Fetches and transforms data; owns 100% of arithmetic | `packages/dagster-pipelines/` |
| **Interpreter** | FastAPI | Turns DB rows into pre-formatted reports + chart-ready JSON | `packages/api/` |
| **Executive** | LangGraph | Reasons over reports, synthesizes a structured thesis | `packages/agent/` |

See [`docs/architecture/system-overview.md`](docs/architecture/system-overview.md) for the full data-flow + boundary documentation. Production topology is [ADR-010](docs/decisions/010-dagster-production-topology.md) (split code-server / daemon / webserver, `DockerRunLauncher` with per-run ephemeral containers).

---

## Screenshots

**Live terminal** — deployed at [equity-data-agent-ynr2.vercel.app](https://equity-data-agent-ynr2.vercel.app). Watchlist on the left, ticker detail (chart + technicals + fundamentals + news) center, agent chat panel right. Server-rendered with SSG; chat streams over SSE.

![Live terminal](docs/screenshots/terminal-live.png)

**CLI thesis** — `uv run python -m agent analyze NVDA` produces a structured Setup / Bull Case / Bear Case / Verdict report. Every number in the output is traceable back to a Dagster-computed report string.

![CLI thesis](docs/screenshots/cli-thesis.png)

**Langfuse trace** — a full `classify → plan → gather → synthesize` agent run on prod, tagged with `intent:thesis` + `model:groq/llama-3.3-70b-versatile` and scored inline (`hallucination_ok`, `plan_adherence`). Per-node spans nest the per-LLM `ChatOpenAI` generation, which carries model name, token usage (`3,394 → 334`), and latency. `session_id` and a hashed-IP `user_id` make every prod chat filterable in the trace list.

![Langfuse trace](docs/screenshots/langfuse-trace.png)

**Dagster asset graph** — `ohlcv_raw → ohlcv_weekly / technical_indicators / fundamental_summary` lineage with run-status decorations.

![Dagster lineage](docs/screenshots/dagster-lineage.svg)

---

## Stack

| Tier | Technology | Why |
|---|---|---|
| Storage | **ClickHouse** + **Qdrant Cloud** | Columnar OLAP for indicators / ratios; managed vector store for news embeddings ([ADR-001](docs/decisions/001-clickhouse-over-postgres.md), [ADR-009](docs/decisions/009-embedding-via-qdrant-cloud-inference.md)) |
| Orchestration | **Dagster** | Asset-based lineage, sensors trigger downstream recompute, asset checks with real domain bounds catch bugs that pass code review |
| API | **FastAPI** | Async, Pydantic-native, auto-generated OpenAPI; rate-limit + per-IP token budget + fail-closed breaker for the public chat endpoint ([ADR-017](docs/decisions/017-public-chat-truly-public-no-auth.md)) |
| Agent | **LangGraph** | Intent-routed 4-node graph (`classify` → `plan` → `gather` → `synthesize`) — per [ADR-007](docs/decisions/007-minimal-agent-graph-first.md)'s minimal-loop principle, with the `classify` router added later for response-shape routing across 7 intents |
| LLM routing | **LiteLLM** + **Groq** (default) + **Gemini 2.5 Flash** (override) | One model alias `equity-agent/default`; switch backends via env var ([ADR-011](docs/decisions/011-llm-routing-groq-default-gemini-override.md)) |
| Observability | **Langfuse** (agent traces) + **Sentry** (FastAPI errors) + Prometheus / Grafana / cAdvisor / node_exporter / Dozzle (host + container metrics + logs) | |
| Frontend | **Next.js 16** on **Vercel** | [ADR-005](docs/decisions/005-nextjs-vercel-over-python-native-frontend.md), [ADR-008](docs/decisions/008-no-vercel-ai-sdk.md) (no Vercel AI SDK — native fetch + ReadableStream for SSE), [ADR-014](docs/decisions/014-nextjs-rendering-mode-per-page.md) (rendering mode per page) |
| Ingress | **Cloudflare named tunnel** | HTTPS at `api.<your-domain>`, FastAPI port :8000 closed to public internet, stable across reboots ([ADR-018](docs/decisions/018-cloudflare-quick-tunnel-for-https-ingress.md)) |
| Packaging | **uv workspaces** | 4 packages under `packages/` ([ADR-002](docs/decisions/002-monorepo-uv-workspaces.md)) |

---

## Production ops

Deployment isn't `git push` and pray. Each merge to `main` runs a series of explicit gates designed by every prior outage:

1. **SOPS decrypt** — age-encrypted `.env.sops` decrypted in CI, never written to prod disk
2. **SHA gate** — `git rev-parse HEAD` on prod must match the merged commit. Catches the "deploy succeeded but code is N commits behind" outage class. ([retro](docs/retros/phase-3-api-layer.md))
3. **Dagster load gate** — definitions module imports cleanly with `≥10 assets`, `≥25 checks`, `≥4 schedules`. Catches the silent "code-server up but graph broken" class. ([retro](docs/retros/phase-3-api-layer.md))
4. **Health-check loop** — 60s timeout retries on `/health`; deploy fails if API doesn't come up.
5. **Idempotent ClickHouse migrations** — re-applied every deploy. ([retro](docs/retros/phase-2-calculation-layer.md))
6. **Bind-mount config detection** — changes to `litellm_config.yaml` / `dagster.yaml` / `workspace.yaml` trigger explicit `docker compose restart` (catches stale-config-on-disk class). ([retro](docs/retros/phase-3-api-layer.md))
7. **`obs-smoke` pre-prod gate** — asserts every Prometheus target up, every Grafana dashboard panel non-empty, every alert rule state≠unknown — closes the "shipped infra to prod, every signal still green, none of it actually working" class.

Ingress topology ([ADR-018](docs/decisions/018-cloudflare-quick-tunnel-for-https-ingress.md)):

```
Browser → https://equity-data-agent-ynr2.vercel.app   (Vercel CDN, frontend)
        → https://api.<your-domain>                   (Cloudflare edge, free WAF + DDoS)
        → cloudflared (outbound from Hetzner — no public ingress port)
        → api:8000                                    (FastAPI, loopback-bound)
```

End-to-end HTTPS via a Cloudflare named tunnel. The hostname is stable across reboots and image bumps — `NEXT_PUBLIC_API_URL` is set once in Vercel and never rotates. Setup runbook in [`docs/guides/vercel-deploy.md`](docs/guides/vercel-deploy.md).

**Why not a PaaS** ([ADR-013](docs/decisions/013-stay-on-bespoke-compose-not-coolify.md)) — every PaaS default is now a documented decision in this repo: HEALTHCHECK + log rotation + `mem_limit`, `restart: unless-stopped`, `autoheal` for sick-but-still-up containers. Each one paid for in incident-debrief, not assumed.

The full failure-mode catalog (symptoms → diagnosis → response → prevention) lives in [`docs/guides/ops-runbook.md`](docs/guides/ops-runbook.md).

**Alerting** — UptimeRobot probe on `/api/v1/health` (Discord) + a `docker-events-notify` systemd unit streaming `die`/`kill`/`oom`/`restart` events to Discord ≤30s. Health monitor cron every 15 min surfaces failures via `make monitor-log`. Dagster run-failure sensor wired to the same Discord webhook so asset materialization failures alert in the ops channel.

---

## Security posture

Supply-chain hygiene is enforced by a layered scanner suite that runs on every PR plus a weekly Trivy cron for image-level CVEs. Every gate is set to **HIGH** severity — false-positive flap on mediums is documented in [`.security/waivers.md`](.security/waivers.md), not silenced by ad-hoc `# nosec` comments.

| Scanner | What it catches | Where |
|---|---|---|
| `pip-audit` | Python dep CVEs against the PyUp DB | PR gate (CI) + `make security-scan` |
| `bandit -lll` | Python static analysis (eval/exec, shell injection, weak crypto, hardcoded passwords) | PR gate (CI) + `make security-scan` |
| `gitleaks` | Secrets in commits (API keys, dotenv values, service-account JSON) | PR gate (CI) + `make security-scan` |
| `npm audit --audit-level=high` | Frontend dep CVEs | PR gate (CI) + `make security-scan` |
| `trivy image` | OS-package and lib CVEs in every prod image (ClickHouse, LiteLLM, Grafana, ...) | Weekly cron, posts severity summary to Discord ops channel |
| Dependabot | Grouped weekly minor/patch bumps + immediate security PRs | `.github/dependabot.yml` (Python, npm, GHA, Docker) |

`make security-scan` runs the full PR-gate suite locally — same flags, same gate level. Active waivers and the rationale for each (false positive vs. tracked deferral) live in [`.security/waivers.md`](.security/waivers.md); reviewers cross-check that file when a previously-failing finding disappears.

---

## Quick start

**Prerequisites**: macOS or Linux, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker, an SSH key on the Hetzner host (or your own ClickHouse), free-tier API keys for [Groq](https://console.groq.com), [Google AI Studio](https://aistudio.google.com/apikey), and (optional) [Qdrant Cloud](https://cloud.qdrant.io) + [Langfuse](https://us.cloud.langfuse.com).

```bash
git clone https://github.com/noahwins-ng/equity-data-agent.git
cd equity-data-agent
make setup                          # installs git hooks, syncs uv workspaces, copies .env.example → .env
$EDITOR .env                        # paste GROQ_API_KEY, GEMINI_API_KEY, CLICKHOUSE_*, etc.
make tunnel                         # SSH tunnel to Hetzner ClickHouse on :8123 (terminal 1)
make migrate                        # apply ClickHouse DDL — only needed on a fresh DB
make dev-litellm                    # LiteLLM proxy on :4000  (terminal 2)
make dev-api                        # FastAPI on :8000        (terminal 3)
make dev-dagster                    # Dagster UI on :3000     (terminal 4, optional)

# now you can run the agent against live data
uv run python -m agent analyze NVDA
```

`make help` lists every target with a one-line description.

---

<details>
<summary><strong>Development workflow</strong> — for engineers reviewing the repo (click to expand)</summary>

&nbsp;

The repo is built around a tight inner loop: every change gets its own branch, its own PR, and squash-merges to `main`.

| Surface | Command | URL |
|---|---|---|
| Dagster UI (asset graph, schedules, sensors) | `make dev-dagster` | http://localhost:3000 |
| FastAPI (auto-generated OpenAPI docs) | `make dev-api` | http://localhost:8000/docs |
| LiteLLM proxy (model routing) | `make dev-litellm` | http://localhost:4000 |
| Next.js frontend | `make dev-frontend` | http://localhost:3001 |
| ClickHouse Play (SQL editor) | `make tunnel` | http://localhost:8123/play |

```bash
# day-to-day
make lint                           # ruff check + pyright
make format                         # ruff format
make test                           # pytest (unit, no infra)
make test-integration               # pytest (needs: make tunnel)

# eval harness — runs the 22-question golden set against any LiteLLM-routed model
uv run python -m agent.evals
uv run python -m agent.evals --model equity-agent/bench-llama4scout

# local prod-image build (used before pushing infra changes)
make build
```

Project conventions (commit format, branching, MCP server setup, hooks) live in [`CLAUDE.md`](CLAUDE.md). Failure-mode catalog and ops procedures live in [`docs/guides/ops-runbook.md`](docs/guides/ops-runbook.md).

</details>

---

## Documentation

The repo's "shared brain" is under [`docs/`](docs/INDEX.md):

- [`docs/INDEX.md`](docs/INDEX.md) — entry point
- [`docs/architecture/system-overview.md`](docs/architecture/system-overview.md) — how the system works (data flow, package boundaries, prod infra)
- [`docs/decisions/`](docs/decisions/) — **19 ADRs** documenting every non-obvious choice (storage, orchestration, agent shape, LLM routing, deploy ingress)
- [`docs/retros/`](docs/retros/) — phase retrospectives ([Phase 0](docs/retros/phase-0-foundation.md) · [Phase 1](docs/retros/phase-1-data-ingestion.md) · [Phase 2](docs/retros/phase-2-calculation-layer.md) · [Phase 3](docs/retros/phase-3-api-layer.md) · [Phase 4](docs/retros/phase-4-narrative-data.md) · [Phase 5](docs/retros/phase-5-agent-layer.md) · [Phase 6](docs/retros/phase-6-frontend.md) · [Phase 7](docs/retros/phase-7-observability-polish.md))
- [`docs/patterns.md`](docs/patterns.md) — established code recipes
- [`docs/guides/ops-runbook.md`](docs/guides/ops-runbook.md) — failure-mode catalog (symptoms → diagnosis → response → prevention)
- [`docs/guides/vercel-deploy.md`](docs/guides/vercel-deploy.md) — frontend deploy + rotation runbook
