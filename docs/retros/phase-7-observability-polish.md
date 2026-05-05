# Retrospective: Phase 7 — Observability & Polish

**Timeline:** 2026-04-12 → 2026-05-05 (backlog queued Apr 12; 4-day burndown May 2 → May 5 immediately after Phase 6 closed)
**Shipped:** 6 issues, 13 PRs merged, 0 rollovers, 2 administrative cancellations (QNT-164/165 absorbed into QNT-103 follow-ups)
**Velocity:** ~1.5 tickets/day during the burndown — above the 0.5–1.0/day Phase 6 baseline

## Six tickets shipped

| Ticket | Title | PRs | Notes |
|---|---|---|---|
| QNT-86 | Integrate Sentry for FastAPI error tracking | #204 (+ #205 .env.sops) | Reviewer caught raw `os.environ` violating "all config via Settings" rule |
| QNT-103 | Observability stack: Dozzle + Prometheus + Grafana + cAdvisor + node_exporter | #194, 195, 196, 197, 198, 199, 200, 201 | 7-PR rework arc — see "harder than expected" below |
| QNT-62 | Dagster alerting on materialization failures | #222 | Reviewer flagged (asset, partition) vs (job, partition) scope drift; bumped query limit from 10 → 100 |
| QNT-63 | Retry logic for external API calls | #223 | RFC 9110 §10.2.3 Retry-After parser + intra-attempt finnhub loop + jittered RetryPolicy on 3 assets |
| QNT-64 | Integration tests: end-to-end critical paths | #221 | Real ClickHouse, 35 new tests in 11.7s, closes QNT-148 mock-only gap |
| QNT-65 | Load test FastAPI endpoints | #226 | Re-scoped 3× before shipping; final scope = endpoint p50/p95/p99 baseline |

## What went well

- **Phase-end burndown** validated the calibration-window pattern. Six tickets in 4 days post-Phase-6-close ran at ~1.5/day sustained — well above the 0.5–1.0/day rate during Phase 6 execution. Concentration > spreading.
- **Adversarial code review** earned its keep again: code-reviewer-ediff caught BLOCKING issues pre-merge on QNT-86 (raw `os.environ` violating the "all config via Settings" rule) and QNT-62 ((asset, partition) vs (job, partition) scope drift; query limit pagination edge). It also returned 5 advisories on QNT-65 that landed as documentation-precision fixes in the same PR.
- **QNT-65 third-rescope** exemplified the "cut, don't preserve" pattern. The audit of `tests/api/test_security.py` found that every demo-protection AC from re-scope #2 was already covered by 8 unit tests; that half got dropped wholesale rather than re-shaped. Final scope shipped in one session.
- **QNT-64 integration tests** structurally closed the QNT-148 mock-only gap. Conftest applies `migrations/*.sql` idempotently on session start, refuses to run against a populated ClickHouse (with a `CI=true` bypass), and auto-truncates between tests. Real ClickHouse client against fixture-loaded data, every router covered, 11.7s for the integration suite.
- **QNT-63 retry coverage** generalized cleanly: a single `parse_retry_after` helper handles both delta-seconds and HTTP-date forms with a 300s ceiling clamp, walks `exc.response.headers` defensively so any library that attaches the original 429 response flows through the same path, and the three ingest assets switched to `Backoff.EXPONENTIAL + Jitter.PLUS_MINUS` matching the existing `DEPLOY_WINDOW_RETRY` pattern.

## What was harder than expected

- **QNT-103 needed 7 PRs to land cleanly** (1 initial + 6 follow-ups). Each follow-up was a silent NoData / empty-panel / never-fires class that no unit test could catch by design:
  - PR #197: cAdvisor `--docker_only` flag missing → Containers dashboard empty + per-container alerts never fire
  - PR #198/199: cAdvisor v0.49.1 → v0.56.2 (overlayfs), then v0.56.x not in the `gcr.io/cadvisor` registry; pinned v0.55.1
  - PR #200: node_exporter mountpoint label `/rootfs` vs `/` → Disk usage panel + HostDiskHigh alert returned NoData
  - PR #201: CD didn't restart Grafana/Prometheus on `observability/` provisioning changes
  - PR #195 (QNT-164): Dozzle OOM at 64m mem_limit on first-connect
  - PR #196 (QNT-165): Dozzle distroless image had no shell → CMD-SHELL healthcheck permanently unhealthy
- **QNT-65 was re-scoped three times** before shipping. Original (Apr 12): generic FastAPI latency probe. Phase 6 retro (May 2): demo-protection load test. Phase 7 retro (May 5): lowered-cap behavioral re-trip. Phase 7 re-scope #2 (May 6, this session): drop demo-protection entirely, ship endpoint p95 baseline only. The third pass was ruthless — audit existing test coverage and delete subsumed AC items wholesale — and it shipped immediately. Lesson: by the third re-scope, the right verb is *delete* not *preserve*.

## Lessons saved to memory

- [`feedback_obs_stack_followup_prs.md`](../../memory/feedback_obs_stack_followup_prs.md) — multi-collector / multi-dashboard infra integrations need a pre-prod smoke that exercises every panel, alert, healthcheck, and CD-mounted-config restart. Treat the smoke as a hard gate, not an advisory. Same shape as `feedback_reactive_sizing_trap.md` and `feedback_vendor_prod_docs.md`.
- [`feedback_triple_rescope_means_cut.md`](../../memory/feedback_triple_rescope_means_cut.md) — when drafting a third re-scope of the same ticket, audit which AC items have been silently subsumed by other tickets that shipped in between. Cut the subsumed items entirely with a doc pointer; don't try to preserve them in a new shape.
- [`feedback_phase_end_burndown_window.md`](../../memory/feedback_phase_end_burndown_window.md) — concentrate phase-tail execution post-milestone-close while calibration context is fresh; the marginal cost of shipping the next ticket is much lower than after a context switch.

## Invariant guards

| Invariant | Guard | Filed |
|---|---|---|
| Every dagster terminal RUN_FAILURE → Discord, with `(job, partition)` dedup over a 10-min window, query `limit=100` for 10× burst headroom | `tests/dagster/test_run_failure_sensor.py` (11 unit tests) + `reference_run_failure_sensor_verification.md` | QNT-62 (this period) |
| 429 / 5xx responses honor `Retry-After`; 4xx errors bubble immediately; jittered backoff on all 3 ingest assets | `tests/dagster/test_retry_helpers.py` + `tests/dagster/test_external_fetch_retries.py` (29 tests) | QNT-63 (this period) |
| Every API router has at least one integration test that exercises real ClickHouse against a fixture-loaded test DB (closes mock-only CTE / `ILLEGAL_AGGREGATION` / GROUP BY scope blind spots) | `tests/api/integration_*.py` (35 tests, conftest enforces fresh-CH safety gate) | QNT-64 (this period) |
| Endpoint p50/p95/p99 baseline for the 5 read endpoints; >5 % errors → exit non-zero so a fast 5xx can't masquerade as a fast endpoint | `scripts/load_test_baseline.py` + `docs/guides/load-test-baseline.md` | QNT-65 (this period) |
| Sentry initialised with `release=settings.GIT_SHA`, `traces_sample_rate=0.1`, `auto_session_tracking=True`, `send_default_pii=False`; chat SSE error paths forward original worker-thread exceptions | `tests/api/test_security.py::test_sentry_*` + `tests/api/test_sentry_init.py` (12 tests) | QNT-86 (this period) |
| Every Prometheus target / Grafana panel / alert rule has data, and every CD-mounted config restarts its consumer | NONE — proposed **QNT-172** (Ops & Reliability, Medium) | structural fix for QNT-103 reactive arc |

### Same-shape clustering

The 5 QNT-103 sub-issues, the Apr 16 SHA drift outage, the Apr 18 reboot outage, and the QNT-112/124/125/146 named-volume / bind-mount / env-vars / migrations incidents all share the same shape: **shipped infra to prod and discovered it didn't work as advertised, with every signal still saying green**. QNT-172 is the cross-cutting structural fix for the QNT-103 sub-class (observability stack ≠ wired up).

## Phase review

Phase 7 was the last planned phase. There is no Phase 8 — the project is feature-complete and remaining work lives in **Ops & Reliability** (perpetual, per `feedback_ops_reliability_is_perpetual.md`).

Recommendations from this retro that were actioned:

- Created **QNT-172** as the structural fix for the QNT-103 reactive arc (5 follow-up PRs would have been caught by an obs-smoke gate)
- Added **QNT-147 / 169 / 170 / 172** to the Ops & Reliability section of `docs/project-plan.md` (sync-docs gap fill)
- Refreshed `docs/architecture/system-overview.md` to add Observability and Resilience sections covering Phase 7 deliverables; replaced stale "Sentry hooks wired ahead of QNT-86" copy

No scope changes recommended for existing Ops & Reliability tickets — none of the open queue items had a Phase 7 lesson that invalidated their scope.

## Open phase-exit item

Line 485 of `docs/project-plan.md` is still unticked: *"Verify: End-to-end run on all 10 tickers, review Langfuse dashboard, confirm no orphaned errors in Sentry."* No QNT ticket attached; user-driven walkthrough remains.

## Next up

The Ops & Reliability queue going into the post-Phase-7 chapter:

| Ticket | Priority | Status | What it does |
|---|---|---|---|
| QNT-126 | **Urgent** | Backlog | Rotate GROQ/GEMINI keys leaked in 2026-04-24 transcript — sitting since Apr 24, should be next pick |
| QNT-104 | Medium (past due) | Todo | Autoheal sidecar for unhealthy long-running containers |
| QNT-170 | Medium | Todo | CD: serialize prod deploys + namespace temp-file (parallel-deploy race) |
| QNT-172 | Medium | Todo | obs-smoke gate (created in this retro) |
| QNT-118 | Low | Backlog | Lazy-import heavy deps in asset modules to shrink per-subprocess RSS |
| QNT-147 | Low | Backlog | Adopt a state-tracking migration tool for ClickHouse (revisit-when triggers) |
| QNT-169 | Low | Backlog | Tame ClickHouse log creep + dozzle OOM (Grafana hygiene pass) |
