# Equity Data Agent — Project Plan

Progress tracking for the phased build-out. Each item maps to one or more Linear issues.
Updated automatically by `/ship` and `/sync-docs`.

---

### Phase 0 — Foundation
**Scope**: Repo scaffolding, infrastructure, and CI/CD.

- [x] Initialize monorepo with uv workspaces (4 packages) — QNT-34
- [x] Set up root `pyproject.toml` with shared dev dependencies (ruff, pyright, pytest)
- [x] Create `shared` package with `Settings`, ticker registry (`TICKERS` list + `TICKER_METADATA` dict with sector/industry per ticker), and base Pydantic schemas — QNT-35
- [x] Write `docker-compose.yml` with dev/prod profiles — QNT-36
- [x] Write `Dockerfile` (multi-stage: base with uv deps → dagster target, api target) — shared by dagster, dagster-daemon, and api services
- [x] Set up ClickHouse with DDL migration scripts (raw + derived databases) — QNT-37
- [x] Configure GitHub Actions for CI (lint + test) and CD (SSH deploy) — QNT-38
- [x] Create `.env.example` with all required environment variables — QNT-39
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
- [x] Widen fundamental_summary P/E asset-check band to symmetric [-10000, 10000] — QNT-119
    - **Triggered by**: Apr 23 2026 — `fundamental_summary_pe_in_band` WARN on AMZN 2022-12-31 annual (P/E = -1008.89, EPS = -0.253 × latest close $255.36). Value is arithmetically correct — today's price × AMZN's 2022 Rivian-writedown loss year — not corruption. Root cause: the check band (-1000, 10000) was asymmetric but the N/M floor `_EPS_NM_THRESHOLD = 0.10` in `fundamental_summary.py` was calibrated to cap |P/E| at ~10k on both signs, so the tight -1000 lower bound flagged legitimate negative-earnings years the N/M floor already intentionally let through. Fix: widen `_PE_MIN` to -10_000.0 for symmetry, update docstring + inline comment so the two calibrations don't drift silently. True arithmetic corruption still trips at |P/E| > 10k. Reinforces `feedback_asset_checks_catch_real_bugs.md` — the asset check surfaced a real calibration inconsistency (not a data bug), which is a valid catch.
- [ ] Resource hygiene: reuse HTTP client across calls, narrow retry-on-Exception — QNT-117
    - **Triggered by**: QNT-54 code review (2026-04-22) — `ClickHouseResource` and `QdrantResource` both build a fresh client on every method call (keepalive wasted, retry loops spawn 3 fresh TCP connections) and both retry on bare `except Exception` (burns the 30s×3 budget on non-transient errors like wrong API key or 4xx that should fail loud). Both fixes travel together: cache client on resource instance via `setup_for_execution`, narrow retry to transport/timeout/5xx.
- [x] Audit asset checks for composite-key aggregation correctness — QNT-122
    - **Triggered by**: Phase 4 retro (2026-04-23) — QNT-120 surfaced that QNT-93's `news_embeddings_vector_count_matches_source` compared Qdrant per-ticker point count against `count() FROM news_raw`, but the asset keys Qdrant on `(ticker, url_id)` so the correct aggregation was `uniqExact(id)`. The check was silently off-by-dedup for a week until QNT-120 exposed it (9/10 tickers showed false drift from re-published articles). `feedback_fix_pattern_not_example.md` says sweep for every instance; this ticket is that sweep across indicator / fundamental / news asset checks. Output: audit note per check in `docs/retros/phase-4-asset-check-audit.md`, follow-up fix tickets for any mismatches found.
- [x] Migrate Dagster to production topology: code-server split + DockerRunLauncher — QNT-116
    - **Triggered by**: Apr 21 2026 21:13–21:26 UTC gRPC-UNAVAILABLE cascade (QNT-115 window) — code-loading subprocess OOM'd inside the `dagster-daemon` cgroup at the 3g ceiling (had just been raised 2g → 3g hours earlier), the code server was unavailable for 180s, and 8 `ohlcv_downstream_job` runs transitioned to FAILURE without launching a step, 5 in lockstep at 21:26 as submission-path retries timed out simultaneously. The incident hit the ceiling of the QNT-100/111/113/115 `mem_limit`-bump ratchet: 3g was already saturated under fan-out so the next bump wasn't going to hold. Root cause: the daemon container was doing jobs Dagster's production deployment docs explicitly carve out into separate services. Shipped Dagster's canonical Docker Compose topology in one PR: (1) split user code into `dagster-code-server` (own `mem_limit: 2g`, gRPC on :4000, healthcheck via `dagster api grpc-health-check`); webserver and daemon reach it via `workspace.yaml`; (2) replaced `DefaultRunLauncher` with `DockerRunLauncher` so each run is an ephemeral container with its own cgroup (per-run OOM no longer touches siblings). Daemon `mem_limit: 3g → 512m`, webserver `2g → 1g`. The DockerRunLauncher also flips `supports_check_run_worker_health = True`, closing QNT-114's chaos-test finding that `monitor_started_run`'s health branch was a no-op on `DefaultRunLauncher`: STARTED-orphan recovery drops from ~30 min (`max_runtime_seconds` fallback) to ~2 min (verified in dev smoke: `docker kill` → FAILURE in 30s via Docker-API `ExitCode: 137` detection — 60× improvement). Three first-boot issues caught + fixed during dev smoke that would have broken prod: workspace.yaml bind-mount path conflict with shared named volume, concurrent-init alembic race (fixed via `depends_on: service_healthy`), run-worker containers couldn't reach SQLite run storage (fixed via `container_kwargs.volumes`). Compose project name pinned to `equity-data-agent` at top-level so DockerRunLauncher network/volume references (which hardcode the prefix) fail loudly at parse time rather than silently at run-launch time if anyone renames the repo dir. ADR-010 captures the decision + alternatives + revisit triggers (SQLite `database is locked`, outgrowing single-VPS, second code-location). Retires the `mem_limit`-bump cycle; QNT-118 (lazy-import sweep) compounds this work but ships independently.
- [ ] Lazy-import heavy deps in asset modules to shrink per-subprocess RSS — QNT-118
    - **Triggered by**: QNT-116 follow-up (2026-04-22) — every Dagster subprocess (code server, run workers, sensor/schedule evaluators) pays top-level `import pandas / numpy / qdrant-client / yfinance / clickhouse-connect` at startup, even when the subprocess never touches the library. QNT-115's revised per-subprocess peak was ~360 MB; hypothesis is the majority is heavy library imports, and deferring them into function bodies should drop per-subprocess RSS to ~120–150 MB. Scope: `from __future__ import annotations` + function-local imports across every module in `packages/dagster-pipelines/src/dagster_pipelines/`; `TYPE_CHECKING` guards for any Pydantic resource fields that annotate heavy external types. Compounds QNT-116's topology gains (more subprocesses to amortize across) but ships independently so rollback is orthogonal. Measurement AC compares pre/post `docker stats` on `dagster-code-server` under a sensor-tick storm.
- [x] CD: restart services whose bind-mounted config changed — QNT-124
    - **Triggered by**: QNT-123 post-deploy verification (2026-04-23) — first POST to `equity-agent/gemini` after the Pro → Flash config change still routed to Pro and returned HTTP 429. Prod SHA matched the merge commit, hard gates green, but `docker compose ps` showed `litellm` Up 21 min — predating the deploy. Root cause: `docker compose up -d` only recreates a container when the *compose service definition* changes (image, command, env_file, environment), NOT when a bind-mounted file changes on disk; `litellm_config.yaml` is bind-mounted and read once at startup with no hot-reload, so the file on disk was new but the running process was stale. Same class as the Apr 16 SHA-drift outage and QNT-112 named-volume-shadow — aggregate-green signals hiding a runtime invariant. Fix: post-`docker compose up -d` step in `.github/workflows/deploy.yml` detects changed bind-mounted config files (`litellm_config.yaml`, `dagster.yaml`, `workspace.yaml`, `Caddyfile`) in the merged diff and `docker compose restart`s affected services before QNT-89's Dagster-graph hard gate runs, so the gate exercises the reloaded config.
- [x] Guard dagster.yaml env_vars against Settings drift — QNT-125
    - **Triggered by**: 2026-04-24 outage — 200/200 Dagster runs failed at dequeue with `Tried to load environment variable OLLAMA_API_KEY, but it was not set` for ~20h before user detection ("why so many failures in Dagster wtf"). Root cause: QNT-59 (Phase 5 LiteLLM swap, PR #110) removed `OLLAMA_API_KEY` + `ANTHROPIC_API_KEY` from `shared.config.Settings` and `.env` but left both keys in `dagster.yaml::run_launcher.config.env_vars`. `DockerRunLauncher` treats the env_vars list as a passthrough contract and drops every run at dequeue when a listed key isn't in the daemon env — the daemon container stays healthy (so uptime probe + docker-events-notify see nothing), `/health` keeps returning 200, and none of the three alerting channels fired. Same "aggregate-green signals hiding a runtime invariant" class as Apr 16 SHA drift, QNT-112 named-volume shadow, and QNT-124 stale bind-mounted config — but on an orthogonal axis: QNT-124 catches "running process vs file on disk"; QNT-125 catches "file on disk vs Settings shape". The previous guard was an inline `# audited as of 2026-04-22` comment, which drifted silently on the very next PR after it was written; this ticket replaces it with a CI test (`tests/dagster/test_run_launcher_env_vars.py`) that asserts `set(env_vars) ⊆ set(Settings.model_fields) ∪ {DAGSTER_HOME}` on every push and fails loud on re-drift (verified via inject-and-restore: bogus key → AssertionError with remediation text, restore → green). Queue-drop alerting (the sibling gap that let this run ~20h undetected) is scoped separately in QNT-62. Calibration note: the Phase-5 LLM swap PR should have been the trigger to run the Settings-diff audit, not a comment promising it was already done — the memory rule "comments are not contracts" is now enforced by CI.
- [x] Migrate orphaned `packages/*/tests/` into `tests/` so pytest actually runs them — QNT-127
    - **Triggered by**: 2026-04-24 QNT-61 adversarial review — a new agent test file landed in `packages/agent/tests/` and was never collected. Root cause: `pyproject.toml` pins `[tool.pytest.ini_options] testpaths = ["tests"]`, so pytest only walks the repo-root `tests/` tree; any `test_*.py` under `packages/<pkg>/tests/` is silently skipped by `uv run pytest` and CI. The drift was months old — ~1,340 lines of API tests + one dagster test had never executed in CI, including `tests/api/test_health.py`'s QNT-51 `/health` deploy-identity contract (a regression there would have shipped uncaught). Fix: (1) `git mv` 7 files into `tests/api/` + `tests/dagster/` — collection 76 → 152 tests, full suite green on first run (no bit-rot to repair); (2) add `tests/test_no_orphan_tests.py`, a CI guard symmetric to QNT-125's env_vars guard that fails loud if any `test_*.py` / `*_test.py` reappears under `packages/*/tests/**` at any depth, with a remediation hint pointing at this ticket. Same durable pattern as QNT-125: replace reviewer vigilance with a machine-enforced contract. Post-review the glob was deepened from shallow `*/tests/test_*.py` to cover nested subdirs + pytest's alternate `*_test.py` discovery convention; both variants negative-tested.

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
- [x] `GET /api/v1/search/news?ticker=NVDA&query=earnings` — QNT-55
    - Returns `[{headline, source, url, published_at, score}]` — top-N results ranked by cosine similarity
    - Both `ticker` and `query` are required. Returns `[]` if Qdrant is unreachable or no news data exists.
    - **Tests use a fake Qdrant client** (same `_FakeClient` pattern) covering ranking, payload shape, and the empty-results/unreachable fallback — Phase 3 lesson
- [x] Add Dagster asset checks for `news_raw` and `news_embeddings` data quality — QNT-93
    - `news_raw`: no empty headlines, valid URLs, no future `published_at` dates, row count per ticker
    - `news_embeddings`: vector count matches source row count, no orphaned vectors
    - Phase 2 lesson: QNT-68 asset checks caught 2 real formula bugs — apply same pattern to news assets
- [x] Namespace Qdrant point IDs by ticker — QNT-120
    - **Triggered by**: QNT-93 `news_embeddings_vector_count_matches_source` flagged every ticker's `|qdrant − clickhouse| > tolerance` on 2026-04-23. Root cause: `news_embeddings` keyed Qdrant points by `blake2b(url)`, but `news_raw` keys rows by `(ticker, url)` — so cross-mentioned URLs had the last ticker's upsert win and silently disappeared from per-ticker search.
    - Fix: `point_id(ticker, url_id) = blake2b(f"{ticker}:{url_id}")` restores 1:1 mapping to ClickHouse's composite key. Orphan asset check updated to compute expected namespaced IDs in Python.
    - Migration: drop `equity_news` collection post-deploy (not backwards-compatible); next sensor tick re-embeds from the 7-day window. Script: `scripts/drop_qdrant_news_collection.py --yes`.
    - Reinforces `feedback_asset_checks_catch_real_bugs.md` + `feedback_dont_explain_away_first_warn.md` — investigated the first prod WARN rather than writing it off as backlog.
- [ ] Verify: Search for recent news about a ticker, confirm relevance ranking

---

### Phase 5 — Agent Layer
**Scope**: LangGraph agent with tools that call FastAPI endpoints.

- [x] Configure LiteLLM proxy via `litellm_config.yaml` — QNT-59
    - **Default**: routes to Groq (`https://api.groq.com/openai/v1`, llama-3.3-70b-versatile) via `GROQ_API_KEY`. Email-only free tier (30 RPM / 6K TPM / up to 14.4K RPD) covers Phase 5 dev + steady-state portfolio demos. ~500 tok/s inference keeps prompt-iteration fast.
    - **Override**: routes to Google AI Studio Gemini 2.5 Flash via `GEMINI_API_KEY` — free-tier quality override (15 RPM / 1500 RPD, no credit card) for the hero demo thesis (QNT-94), README screenshot (QNT-66), and as the per-provider axis in the QNT-67 eval harness.
    - Model alias: `equity-agent/default` — zero agent code changes to switch backends.
    - See ADR-011 for provider selection rationale (why Groq over Ollama Cloud / Gemini / OpenAI / self-hosted).
- [x] Demote Gemini override from Pro to Flash — QNT-123
    - Shipped right after QNT-59. First live test of the `equity-agent/gemini` alias returned HTTP 429 with `limit: 0` — Gemini 2.5 Pro is no longer on Google AI Studio's free tier. Swapped to Gemini 2.5 Flash (free-tier-reachable) to preserve ADR-011's "free to clone" invariant. Pro stays available via one-line YAML edit if a paid plan is ever added. See ADR-011 §Revision history (2026-04-23 Pro → Flash entry) for the full story.
- [x] Integrate Langfuse tracing — QNT-61 **[day-one of Phase 5, moved from Phase 7]**
    - `LangfuseResource` in the agent package; `@observe` decorator on every tool and graph node from the first commit of agent code — traces are needed *while* iterating on the prompt, not bolted on after shipping.
    - Portfolio artifact: one Langfuse trace screenshot is embedded in the README (QNT-66).
- [x] Define LangGraph state schema (ticker under analysis, gathered reports, thesis draft) — QNT-56
- [x] Implement tools — QNT-57
    - `get_summary_report` → calls `/reports/summary/{ticker}` (agent calls this first)
    - `get_technical_report` → calls `/reports/technical/{ticker}`
    - `get_fundamental_report` → calls `/reports/fundamental/{ticker}`
    - `get_news_report` → calls `/reports/news/{ticker}`
    - `search_news` → calls `/search/news`
    - **Ship as one PR using the template-pattern approach from QNT-69**: all 5 tools share the same shape (`httpx.get(url).text → return string`) with only the URL template varying. Build the hardest tool end-to-end first (`search_news` — has extra `query` arg + empty-results fallback), then parameterise for the other 4 in the same PR. Resist splitting into 5 PRs. Phase 3 lesson (PR #53 bundled QNT-69 + 4 report endpoints).
    - **Pre-implementation tool-contract block (required in PR body)** — for each tool, a three-line mini-spec before any code:
        - **Input**: named args + types (`ticker: str`, `query: str`, etc.)
        - **Upstream**: exact FastAPI method + path (`GET /api/v1/reports/technical/{ticker}`)
        - **Return**: concrete shape (e.g., "plain-text report — numbers appear verbatim; consumed by LLM as-is") and the degraded-case return (`""`, `"No news data available."`, etc.)
      Forces the API-response ↔ graph-state identity to surface at design time. Phase 4 retro lesson: QNT-54 shipped a Qdrant point-ID scheme that silently collided with ClickHouse's composite key because the cross-store identity was never written down (cost: QNT-120 fix + follow-up). Apply the same identity-first discipline to agent tools. Reinforces ADR-003 by naming *where* every number in the thesis originates.
- [x] Build agent graph — **3 nodes: plan → gather → synthesize** (per ADR-007) — QNT-56
    - No critique / reflect / retry loop until the baseline has failed in specific, observed ways. Adding loops prematurely is indistinguishable from the baseline working.
- [x] System prompt enforcing the "interpret, don't calculate" mandate — QNT-58
    - **Triggered by**: Phase 5 Agent Layer architectural-boundary work — ADR-003's "LLM never does math, only interprets reports" had been paraphrased into a one-liner buried in a graph-node closure, which drifted each time the prompt changed. This ticket promotes the four issue-body rules (no arithmetic / cite source / structured thesis / confidence anchored to data completeness) to a named `SYSTEM_PROMPT` in `packages/agent/src/agent/prompts/` and wires them into the synthesize node. Round-1 review caught three issues that would have meaningfully degraded the contract: (1) the prompt was being delivered as a flat user-turn string via `langchain.ChatModel.invoke(str)` rather than a `SystemMessage`, dropping ADR-003's authority on most providers; (2) the `=== <name> report ===` fences for report bodies were vulnerable to fence-collision injection from FastAPI report content (news headlines etc.); (3) `REPORT_SOURCES` duplicated the graph's `REPORT_TOOLS` tuple with only a runtime-test guard against drift. Round-2 fixes: `build_synthesis_prompt` returns `[SystemMessage(SYSTEM_PROMPT), HumanMessage(...)]`, `traced_invoke` accepts `str | list[BaseMessage]`, `_sanitize_report_body` neutralises `===` runs in untrusted bodies (replace with `==·==`, with parametrized tests pinning the long-run invariant so a future "simplify" refactor can't reintroduce the gap), and `REPORT_TOOLS` lives canonically in `agent.prompts.system` with `is`-identity tested. Empirical "model obeys the rules in actual output" verification is QNT-67's defining job (hallucination eval) — Phase 5 splits "set the rule" (this ticket) from "measure compliance" (next ticket) by design.
- [x] Agent CLI: `python -m agent analyze NVDA` — run single-ticker analysis from terminal — QNT-60
    - **Built before the SSE endpoint** — ~50× faster prompt iteration without a frontend round-trip. Pairs with the eval harness below.
- [x] Agent evaluation framework — QNT-67 **[highest-priority Phase 5 item — the single biggest AI-Engineer hiring signal]**
    - Lives under `packages/agent/evals/`. Three eval types — all required, not optional:
    - **(a) Numeric-claim hallucination detector** (`evals/hallucination.py`): regex every number out of the agent's thesis; assert each appears verbatim in one of the report strings the agent received as tool output. Any mismatch = test failure. Operationalises the ADR-003 contract.
    - **(b) Golden set** (`evals/golden_set.py` + `evals/goldens/questions.yaml`): 15–20 curated `(ticker, question, reference_thesis, expected_tools)` pairs. **Coverage invariant: at least one question per ticker in `shared.tickers.TICKERS`** — thin, heavy, volatile, and quiet tickers all exercise the pipeline. Phase 2 retro lesson (`feedback_sample_ac_broadly.md`): a golden set that skews to NVDA/AAPL leaves bugs in UNH/V/JPM invisible to regression. Per run, track LLM-as-judge score + cosine similarity of generated thesis vs reference thesis. Commit `evals/history.csv` so prompt-version quality is visible in `git log -p`.
    - **(c) Tool-call correctness** (`evals/tool_calls.py`): for each golden-set question, assert the expected tool was called — e.g., valuation questions MUST call `get_fundamental_report`, technical questions MUST call `get_technical_report`.
    - Design goal: harness is reusable enough to extract as a standalone repo later.
- [x] Investigate + fix the 3 hallucination findings the QNT-67 baseline flagged — QNT-128
    - **Triggered by**: QNT-67 baseline (commit 1b66e7b, run 20260425T092008Z) flagged 3 records emitting numbers absent from any tool-output report — `amzn-fundamental` (16.09), `unh-fundamental` (89.02 + 99.82), `unh-news` (same UNH numbers). The framework was doing exactly what it should; per the issue body the next step was to classify each finding as (a) rounding / (b) cross-pollination / (c) pretraining leakage / (d) regex false-positive, then apply the matching lever. Replay of all 3 records showed every "hallucinated" number was actually present in the reports as the negative form (`-16.09`, `-89.02`, `-99.82`) — the reports template YoY changes with explicit signs while the model naturally moves the sign into English verbs ("free cash flow declined 16.09%"). Class (d): regex false-positive. Fix: support comparison strips the leading sign before lookup; canonical form (and the `unsupported` listing for `--explain`) keeps it for explainability. Trade-off documented in `hallucination.py`'s "Sign-magnitude support" section and pinned by an `xfail(strict=True)` tripwire (`test_inverted_sign_thesis_should_be_flagged_but_is_not`) — if a future change re-introduces asymmetric sign comparison without a deliberate decision to remove the trade-off, the suite fails loud. Verifying live sweep (run 20260425T142759Z): 16/16 hallucination_ok, 16/16 tool_call_ok, exit 0. Side effect: `litellm_config.yaml` adds Qwen3-32B as auto-fallback for the default Llama-3.3-70B alias (Groq's free-tier daily TPD on Llama is 100K, ~2 sweeps' worth; Qwen-fallback adds 5× more headroom). Llama stays default — Qwen-as-default was tested first and regressed 15/16 → 11/16 hallucination_ok because Qwen rounds numbers and leaks `<think>` reasoning into the thesis content.
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

- [ ] ADR-012: Next.js rendering mode per page (SSG / SSR / CSR) + cache strategy — QNT-121 **[written BEFORE any page code]**
    - Phase 4 retro carry-over: the Dagster quickstart-topology arc (QNT-100 → QNT-116, 17h across 4 incidents) and Qdrant point-ID arc (QNT-54 → QNT-120) both burned hours because the prod-vs-tutorial gap surfaced at deploy, not design. Next.js app-router has the same shape — SSR-by-default + RSC boundaries + SSE streaming don't all compose.
    - Each page names: rendering mode, data-fetch location (RSC vs client vs API route), cache / revalidate strategy, and failure-mode rendering. Dashboard probably ISR; ticker detail SSR + client-side toggle; chat CSR + `fetch`/`ReadableStream`.
    - Output: `docs/decisions/012-nextjs-rendering-mode-per-page.md` — every subsequent Phase 6 ticket references it by section.
- [ ] Initialize Next.js app in `frontend/` with Tailwind CSS — QNT-71
- [ ] Dashboard page (`/`) — ticker cards showing price, daily change, RSI signal, trend status — QNT-72
    - Calls `GET /api/v1/dashboard/summary` (single request for all tickers — no N+1)
- [ ] Ticker detail page (`/ticker/[symbol]`) — full analysis view — QNT-73
    - TradingView Lightweight Charts: candlestick + volume (`GET /api/v1/ohlcv/{ticker}`). **Chart renders `adj_close` as the candlestick close value** to avoid split discontinuities
    - Timeframe toggle: daily / weekly / monthly (swaps chart + indicator data)
    - Technical indicator overlays: SMA, EMA, Bollinger Bands on chart; RSI, MACD as separate panes
    - Fundamental ratios table: 15 ratios in 5 categories (`GET /api/v1/fundamentals/{ticker}`)
    - Recent news sidebar (`GET /api/v1/search/news?ticker={ticker}`) — gracefully degrades if Phase 4 not deployed
- [ ] Agent chat page (`/chat`) — conversational interface — QNT-74
    - Calls `POST /api/v1/agent/chat` with SSE streaming
    - **No Vercel AI SDK** — use native `fetch` + `ReadableStream`. Optionally add `eventsource-parser` (~2KB) for SSE line parsing.
    - Displays agent thesis with markdown rendering
    - Shows which tools the agent called (transparency)
- [ ] Generate TypeScript types from FastAPI's `/openapi.json` via `openapi-typescript` (`make types`) — do not handwrite types in `lib/api.ts`
- [ ] Deploy to Vercel, set `NEXT_PUBLIC_API_URL` in Vercel dashboard — QNT-75
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
