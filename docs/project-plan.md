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
- [x] Create ops runbook skeleton with failure-mode catalog — QNT-99
    - **Triggered by**: Apr 19 2026 retro — the Ops & Reliability work has turned specific incidents into permanent detectors, but there's no consolidated document to grep when something breaks at 3am. Runbook is the index into the muscle memory. Small scaffolding ticket; subsequent Ops & Reliability tickets add their own entries.
- [x] Harden docker-compose.yml: HEALTHCHECK + log rotation + resource limits — QNT-100
    - **Triggered by**: Apr 19 2026 retro — raw compose defaults leave three specific gaps we hadn't closed: "sick but still up" (no healthchecks), "disk fills with logs" (no rotation), "one leaky service OOMs the box" (no resource limits). Addressing each directly on the existing stack.
- [x] Alerting pipeline: uptime monitoring + container state notifications — QNT-101
    - **Triggered by**: Same Apr 19 2026 retro — Apr 18 outage surfaced that `/health` failures go into a log file nobody reads. Need real pager (SMS/email) for downtime + Discord notifications for container state changes.
- [ ] Autoheal sidecar + tighten resource limits after observation — QNT-104
    - **Triggered by**: QNT-100 deferrals — plain compose's `restart: unless-stopped` only restarts on container *exit*, not *unhealthy* status; sick-but-still-up is a known gap. `willfarrell/autoheal` sidecar watches healthcheck status and kills unhealthy containers so the restart policy picks them up. Also tightens `mem_limit` / `cpus` (set generously in QNT-100) to peak + ~30% headroom after 1-2 weeks of observed usage.
- [x] Raise dagster-daemon mem_limit 1g → 2g after Apr 20 weekly-fundamentals OOM cascade — QNT-111
    - **Triggered by**: Apr 20 02:00 UTC weekly schedule tick — `fundamentals_weekly_schedule` fired 10 partitions, the daemon's 1g cgroup (set by QNT-100) was blown out by concurrent partition-launcher subprocesses. Kernel OOM-killed 4+ python processes in ~2 min (one hit total-vm 2GB), 7 partitions failed to launch, 2 runs orphaned in CANCELING for ~10.5h. Narrow stopgap so next Sunday's tick doesn't cascade; QNT-104 still owns the comprehensive observed-peak audit + autoheal sidecar.
- [x] Two-layer deploy-window retry protection for sensor + schedule jobs — QNT-110
    - **Triggered by**: Apr 19 same-day shipping cadence — 2 `ohlcv_downstream_job` runs failed with `DagsterUserCodeUnreachableError: gRPC UNAVAILABLE` when dequeued mid-deploy (code-server container was restarting). No op ran, so the ticket's originally-proposed op-level `RetryPolicy` wouldn't have caught it. Shipped **both layers**: op-level `DEPLOY_WINDOW_RETRY` on sensor jobs (catches in-run flakes like yfinance timeouts) + run-level `run_retries` in `dagster.yaml` with per-job `dagster/max_retries` tags on all 4 auto-triggered jobs (catches launch-time failures). `retry_on_asset_or_op_failure: false` ensures real op errors still fail loud — only launch failures retry. Complements QNT-109 (notification suppression during the same deploy window).
- [x] Cap Dagster concurrent runs at 3 with QueuedRunCoordinator to prevent backfill fan-out OOM — QNT-113
    - **Triggered by**: Apr 20 13:22–13:28 UTC manual backfill — 10-partition `fundamentals_weekly_job` launched all 10 subprocess workers simultaneously (DefaultRunCoordinator fires immediately, no queue). Each worker loads the full repo (~150 MB RSS / ~2 GB VM); combined load re-OOM'd the daemon's 2 GB cgroup even after QNT-111's bump. QNT-110 retries re-launched into the same starved cgroup, looping backfill `tevuzzoj` into a 10:31 failure (partition AMZN stuck at "Failed to start"). Fix adds `QueuedRunCoordinator(max_concurrent_runs=3)` to `dagster.yaml` — peak memory now ~1.1 GB within the 2 GB limit (daemon baseline ~260 MB + sensor-tick headroom ~400 MB + 3 × 150 MB). QNT-111 addressed the daemon's baseline; QNT-113 addresses the fan-out; QNT-104 still owns the broader resource-limit audit.
- [x] Raise dagster-daemon mem_limit 2g → 3g after QNT-113 sizing math under-estimated per-worker peak — QNT-115
    - **Triggered by**: Apr 21 2026 12:22 / 12:48 UTC — the same `__ASSET_JOB` backfill (MSFT/GOOGL/AMZN) that motivated QNT-114 also pushed the daemon cgroup to 1.74 GiB / 2 GiB (87%) and the kernel OOM-killed python run-worker subprocesses. QNT-113 had sized the cap assuming 150 MB RSS per worker (reasonable for the Apr 20 `fundamentals_weekly_job` which mostly did repo-load), but observed peak during `__ASSET_JOB` *materialization* is ~360 MB, so real load was 660 + 3 × 360 = 1740 MB — right at the limit with no headroom. Fix: bump `docker-compose.yml` dagster-daemon `mem_limit` 2g → 3g; `max_concurrent_runs` stays at 3 (this is headroom, not more parallelism). Also revised the "Adding a Dagster Asset → Concurrency pre-flight" formula in `docs/patterns.md` from `/150 MB` to `/360 MB` and swept four stale call sites (`dagster.yaml`, `ops-runbook.md`, `schedules.py`, `ops-investigator` symptom card) so the sizing math is consistent everywhere. Theoretical ceiling at 3g is ~6; practical cap stays at 3. Pairs with QNT-114: run_monitoring auto-heals ghosts when OOMs still happen, mem bump reduces the rate at which OOMs happen in the first place.
- [x] Enable run_monitoring + backfill tag concurrency in dagster.yaml — QNT-114
    - **Triggered by**: Apr 21 2026 post-incident backfill — `__ASSET_JOB` (MSFT/GOOGL/AMZN partitions) OOM'd the daemon cgroup; the AMZN run-worker was kernel-killed before emitting `RUN_FAILURE`. Operator "Terminate" flipped it from STARTED → CANCELING; the run never progressed because CANCELING requires the worker to ack and the worker was dead. The ghost slot held 1 of 3 `max_concurrent_runs`, the backfill daemon kept auto-relaunching AMZN producing more ghosts, and the queue wedged ~30 min until the daemon was restarted. Root cause: Dagster ships with `run_monitoring` disabled by default; no daemon was polling the CANCELING rows. Shipped in two PRs plus a post-ship doc clarification: (1) PR #94 enabled `run_monitoring` + `tag_concurrency_limits: dagster/backfill: 2`; (2) post-ship live chaos test on prod (injected a STARTED row) surfaced that `DefaultRunLauncher` returns `supports_check_run_worker_health = False`, so `monitor_started_run`'s worker-health branch is a no-op for us; (3) hotfix PR #95 added `max_runtime_seconds: 1800` as a timeout fallback so STARTED ghosts with no operator Terminate also get recovered (after 30 min). Important correction captured in the runbook: the Apr 21 incident was actually the **CANCELING class**, recovered by `monitor_canceling_run` within `cancel_timeout_seconds` (~3 min) — that path is launcher-independent and was already covered by PR #94. The hotfix closes a separate STARTED-no-Terminate class the incident didn't hit. Three orphan classes total, three code paths, three timeouts — documented as a table in `ops-runbook.md` under "CANCELING ghost after run-worker OOM". A proper launcher switch to `DockerRunLauncher` (which advertises health-check support → STARTED recovery in ~2 min instead of 30 min) is tracked as a separate follow-up ticket. Complements QNT-113 (fan-out cap) and QNT-110 (launch-time retry). Durable lesson captured: "run_monitoring.enabled: true" is necessary but not sufficient — each orphan class (STARTING / CANCELING / STARTED) uses a different code path with different launcher dependencies; validate which class your incident was before declaring recovery proven.
- [x] Bind-mount dagster.yaml so repo edits actually reach the running daemon — QNT-112
    - **Triggered by**: Apr 20 QNT-110 ship session discovery — `dagster_home` named volume held a stale copy of `dagster.yaml` seeded on Apr 16 initial deploy; `git pull` on every deploy updated the host file but never the container. QNT-110's `run_retries` config silently failed to activate despite CD green + all hard gates passing. Same class as Apr 16 SCP-drift and Apr 18 "deploy green ≠ code active" — aggregate green signals hid an invariant. Fix: bind-mount `./dagster.yaml:/dagster_home/dagster.yaml:ro` on both `dagster` and `dagster-daemon`. Repo is now the single source of truth for dagster.yaml; named volume keeps Dagster-managed state (history/storage/schedules) intact. PR also includes a smoke-marker comment in `dagster.yaml` so the deploy self-verifies the delivery path end-to-end.
- [x] Docs: swap uptime monitoring guide from BetterStack to UptimeRobot — QNT-105
    - **Triggered by**: QNT-101 rollout — BetterStack's free tier requires a paid plan for Discord webhooks, while UptimeRobot ships native Discord integration on free. Zero-cost polish to match the free-tier Discord path the project already uses elsewhere.
- [x] API accepts HEAD on /health endpoints for HEAD-only uptime probes — QNT-106
    - **Triggered by**: QNT-105 uptime-monitoring switch — UptimeRobot defaults to HEAD probes; FastAPI's auto-generated GET routes return 405 on HEAD. Explicit HEAD handlers let the uptime probe pass cleanly without needing UptimeRobot's paid advanced-monitor tier.
- [x] Polish docker-events-notify: fix `<no value>` exit-code display + correct restart-policy docs — QNT-107
    - **Triggered by**: QNT-101 runtime testing — Docker kill events don't carry `exitCode`, so Go template `<no value>` sentinel leaked past the bash `${var:-default}` fallback. Also corrected Makefile + uptime-monitoring.md that implied `restart: unless-stopped` auto-restarts after `docker kill` (it doesn't — Docker treats both `stop` and `kill` as "manually stopped" and skips the restart policy).
- [x] Suppress docker-events alerts during CD deploy window — QNT-109
    - **Triggered by**: QNT-101 noise during deploys — every main-branch merge fires ~4 Discord messages ([KILL]+[DIE] on api from GIT_SHA-driven recreate, [DIE] on both dagster services from image rebuild) plus 2-3 "can not get logs" races. Expected churn, not real incidents — but trains users to ignore the channel, eroding QNT-101's pager purpose. Sentinel-file deploy window (`/opt/equity-data-agent/.deploy-in-progress`) suppresses notifier events during CD; fail-open (>10 min) so a crashed deploy can never silently mute real alerts.
- [x] Encrypt .env at rest with SOPS — QNT-102
    - **Triggered by**: Same Apr 19 2026 retro — plaintext `.env` on VPS = all credentials leak on compromise. Replace with SOPS-encrypted file + decrypt-on-deploy. (ClickHouse backup ticket deferred: current data <1GB, re-ingestible from yfinance in 1-2h; revisit after Phase 4 news+embeddings populate.)
- [x] Migrate Dagster to production topology: code-server split + DockerRunLauncher — QNT-116
    - **Triggered by**: Apr 21 2026 21:13–21:26 UTC gRPC-UNAVAILABLE cascade (QNT-115 window) — code-loading subprocess OOM'd inside the `dagster-daemon` cgroup at the 3g ceiling (had just been raised 2g → 3g hours earlier), the code server was unavailable for 180s, and 8 `ohlcv_downstream_job` runs transitioned to FAILURE without launching a step, 5 in lockstep at 21:26 as submission-path retries timed out simultaneously. The incident hit the ceiling of the QNT-100/111/113/115 `mem_limit`-bump ratchet: 3g was already saturated under fan-out so the next bump wasn't going to hold. Root cause: the daemon container was doing jobs Dagster's production deployment docs explicitly carve out into separate services. Shipped Dagster's canonical Docker Compose topology in one PR: (1) split user code into `dagster-code-server` (own `mem_limit: 2g`, gRPC on :4000, healthcheck via `dagster api grpc-health-check`); webserver and daemon reach it via `workspace.yaml`; (2) replaced `DefaultRunLauncher` with `DockerRunLauncher` so each run is an ephemeral container with its own cgroup (per-run OOM no longer touches siblings). Daemon `mem_limit: 3g → 512m`, webserver `2g → 1g`. The DockerRunLauncher also flips `supports_check_run_worker_health = True`, closing QNT-114's chaos-test finding that `monitor_started_run`'s health branch was a no-op on `DefaultRunLauncher`: STARTED-orphan recovery drops from ~30 min (`max_runtime_seconds` fallback) to ~2 min (verified in dev smoke: `docker kill` → FAILURE in 30s via Docker-API `ExitCode: 137` detection — 60× improvement). Three first-boot issues caught + fixed during dev smoke that would have broken prod: workspace.yaml bind-mount path conflict with shared named volume, concurrent-init alembic race (fixed via `depends_on: service_healthy`), run-worker containers couldn't reach SQLite run storage (fixed via `container_kwargs.volumes`). Compose project name pinned to `equity-data-agent` at top-level so DockerRunLauncher network/volume references (which hardcode the prefix) fail loudly at parse time rather than silently at run-launch time if anyone renames the repo dir. ADR-010 captures the decision + alternatives + revisit triggers (SQLite `database is locked`, outgrowing single-VPS, second code-location). Retires the `mem_limit`-bump cycle; QNT-118 (lazy-import sweep) compounds this work but ships independently.

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
- [x] Verify: Run backfill for all 10 tickers, confirm data in ClickHouse, check Dagster lineage graph — verified 2026-04-19 in prod: `ohlcv_raw` has 504 rows/ticker (2024-04-15 → 2026-04-17) for all 10 tickers; `fundamentals` has 9-11 quarters/ticker; derived tables populated (weekly=1040, monthly=240, tech_daily=5040, fund_summary=101); Dagster asset graph loads 8 assets + 17 checks + 2 schedules + 2 sensors

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
- [x] Null out P/E when EPS is near zero (|EPS| < $0.10) to honor N/M convention — QNT-87
    - **Triggered by**: Phase 2 QA — P/E was emitting absurd values (>1000x) during near-zero-EPS quarters, pretending precision where the ratio is meaningless. Now renders as `N/M (near-zero earnings)` in reports.
- [x] Fix P/E to use TTM earnings on quarterly rows in fundamental_summary — QNT-91
    - **Triggered by**: Phase 2 QA — quarterly P/E was dividing price by single-quarter EPS, inflating the ratio by ~4×. Switched to trailing-twelve-month EPS so quarterly and annual P/E are directly comparable.
- [x] Set `default_status=RUNNING` on all sensors and schedules — QNT-92
    - **Triggered by**: Runtime-state drift — Dagster defaults sensors/schedules to STOPPED, requiring a manual UI toggle after every redeploy. Declaring `RUNNING` in code makes prod runtime state fully reproducible from git.
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
- [x] `GET /api/v1/tickers` — returns the ticker list from `shared.tickers.TICKERS` — QNT-78
- [x] `GET /api/v1/health` — health check with ClickHouse + Qdrant connectivity status + deploy identity (git SHA, Dagster asset/check counts) — QNT-51

**Cross-cutting:**
- [x] CORS middleware configured (allow production domain, `*.vercel.app` for preview deploys, and `localhost:3001` for dev) — `packages/api/src/api/main.py:131-137`
- [x] Ticker validation: all `{ticker}` path endpoints AND the `POST /agent/chat` request body validate the ticker against `shared.tickers.TICKERS` and return `404 {"detail": "Ticker not found"}` for unknown tickers — enforced in `packages/api/src/api/routers/data.py` and all report template formatters
- [x] No API authentication in initial scope — the API is read-only and serves public market data
- [x] Verify: Hit all endpoints with VS Code REST Client (`.http` files), confirm chart data arrays are correctly structured, check OpenAPI docs at `/docs` — verified 2026-04-19 via prod `curl` pass: 10 OpenAPI paths, correct row counts (504 daily / 104 weekly / 24 monthly / 10 fundamental / 10 dashboard), all `{ticker}` endpoints 404 on BOGUS, `/docs` 200

---

### Phase 4 — Narrative Data
**Scope**: News ingestion, embedding, and semantic search via Qdrant.

- [x] Ingest news via **RSS + `feedparser`** — QNT-52
    - Per-ticker Yahoo Finance RSS (`https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US`), plus 1–2 broad market feeds (e.g., Reuters markets RSS)
    - No paid news API evaluation — RSS is free, unrate-limited, and deterministic enough for a 10-ticker scope. The news-API comparison rabbit hole is not the portfolio story; RSS + embeddings + semantic search is.
- [x] Implement `news_raw` Dagster asset (RSS feeds → `equity_raw.news_raw` in ClickHouse) — QNT-53
    - Schedule: every 4 hours during market hours, `default_status=RUNNING` (Phase 2 lesson: QNT-92)
    - Dedup key: `id = hash(ticker + url)`
    - Stores: `headline`, `body`, `source`, `url`, `published_at` per ticker
    - Downstream sensor (`news_raw` → `news_embeddings`) must batch all pending events per tick from day one (Phase 2 lesson: QNT-46 rewrite) — **deferred to QNT-54** (target asset `news_embeddings` lives there; reuses the existing `_build_materialization_sensor` factory for batch semantics)
- [x] Create Qdrant `equity_news` collection (384-dim Float32, cosine distance) — auto-create in the Qdrant Dagster resource on first use, or via a setup script (shipped as part of QNT-54 — `QdrantResource.ensure_collection` runs at asset-start)
- [x] Implement `news_embeddings` Dagster asset (`news_raw` → Qdrant Cloud) — QNT-54
    - Embeds `headline` text using `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
    - Sensor-triggered when `news_raw` materializes new rows
    - Stores vector + full payload (headline, source, url, published_at, ticker) in Qdrant
    - **Tests use a fake Qdrant client** (analogous to the `_FakeClient` for ClickHouse in `packages/api/tests/test_data.py`) so CI is deterministic with no Qdrant Cloud connection — Phase 3 lesson
- [ ] `GET /api/v1/search/news?ticker=NVDA&query=earnings` — QNT-55
    - Returns `[{headline, source, url, published_at, score}]` — top-N results ranked by cosine similarity
    - Both `ticker` and `query` are required. Returns `[]` if Qdrant is unreachable or no news data exists.
    - **Tests use a fake Qdrant client** (same `_FakeClient` pattern) covering ranking, payload shape, and the empty-results/unreachable fallback — Phase 3 lesson
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
    - **Ship as one PR using the template-pattern approach from QNT-69**: all 5 tools share the same shape (`httpx.get(url).text → return string`) with only the URL template varying. Build the hardest tool end-to-end first (`search_news` — has extra `query` arg + empty-results fallback), then parameterise for the other 4 in the same PR. Resist splitting into 5 PRs. Phase 3 lesson (PR #53 bundled QNT-69 + 4 report endpoints).
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
- [ ] **Cross-cutting**: Ticker list is sourced from `GET /api/v1/tickers` across every page (dashboard cards, detail-page switcher, chat-page selector) — never hardcoded. Inherits QNT-78's ⏳ PENDING AC; hardcoding the list anywhere defeats the endpoint's purpose. Phase 3 lesson.
- [ ] Verify: Dashboard loads all 10 tickers, chart renders with timeframe toggle, agent chat streams a thesis

---

### Phase 7 — Observability & Polish
**Scope**: Tracing, alerting, and production hardening.

- [ ] Observability stack: Dozzle logs UI + Prometheus/Grafana/cAdvisor metrics — QNT-103 **[moved from Ops & Reliability — Apr 19 2026 retro surfaced the need; full observability stack is Phase 7-shaped planned infra, not narrow reactive hardening]**
    - **Triggered by**: Apr 19 2026 retro — unified logs UI + resource trend visibility are the observability surfaces missing from the current setup. Enables diagnosing slow leaks before they become outages.
- [ ] Integrate Sentry for FastAPI error tracking (`sentry-sdk[fastapi]`, uses `SENTRY_DSN` from `.env`) — QNT-86
- [ ] Add Dagster alerting on asset materialization failures — QNT-62
- [ ] Implement retry logic for flaky external API calls (yfinance, news APIs) — QNT-63
- [ ] Load test FastAPI endpoints (confirm response times under 10 tickers) — QNT-65
- [ ] Write integration tests for critical paths (ingestion → calculation → report → agent) — QNT-64
- [ ] Verify: End-to-end run on all 10 tickers, review Langfuse dashboard, confirm no orphaned errors in Sentry
