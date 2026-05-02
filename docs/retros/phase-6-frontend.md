# Retrospective: Phase 6 — Frontend

**Timeline**: 2026-04-26 → 2026-05-02 (≈6 days, 1 cycle)
**Shipped**: 29 issues, 34 PRs merged
**Linear**: 100% complete

## What went well

- **Mid-milestone scope tripled cleanly.** Started as a 5-ticket frame (QNT-71/72/73/74/75); absorbed 24 mid-flight tickets across three waves (pre-Phase-6 prep, narrow-viewport polish, deploy-blocker + abuse hardening) without slipping a single one. Validates `feedback_calibration_window.md` — building when context was fresh kept rework cheap.
- **Design-first ADRs landed before consumer code.** ADR-014 (rendering modes), ADR-015 (news-source/sentiment topology), ADR-016 (publisher canonicalisation), ADR-017 (truly-public chat auth model), ADR-018 (Cloudflare quick tunnel) all gated their consumer tickets. No "wrote a page, then realised the API was wrong" episodes.
- **`/health` provenance strip held.** QNT-132's source-of-truth approach to the bottom-strip was the right call — the schedule-shift QNT-143 (4h → daily) flipped the UI without a frontend deploy.
- **Reviewer agent caught 3 blocking issues per ticket on the harder PRs.** QNT-161 (rate-limit XFF, fail-closed loop, contextvar leak) and QNT-75 (latest-tag, dormant-profile restart, missing --remove-orphans) both got blocking issues fixed pre-merge that would have been outages otherwise. The pattern is now reliable.
- **Asset checks paid for themselves again.** QNT-122 / QNT-148 / QNT-93 surfaced bugs that code review missed.

## What was harder than expected

- **Backend support packs always need consumer-exercise rework.** QNT-134 took 5 PRs (initial + 4 follow-ups: partition mismatch breaking prod code-server, OBV split-adjusted volume mismatch with TradingView, ebitda_margin_pct TTM-only fix, docs clarification). Same shape on QNT-141 and QNT-148 (3-4 PRs each). The "complete" backend ticket isn't actually complete until a real consumer exercises it. Saved as `feedback_backend_pack_consumer_loop.md`.
- **Responsive viewport polish shipped as 3 separate tickets when it should have been 1.** QNT-151 / QNT-152 / QNT-153 were three back-to-back tickets each fixing a 14" MacBook viewport regression — combined ~6 PRs across the cluster. One "narrow-viewport audit" ticket would have done. Saved as `feedback_responsive_audit_one_ticket.md`.
- **QNT-148 prod 500 (ClickHouse CTE alias collision).** Mock-only API tests passed; the real `ILLEGAL_AGGREGATION` error only surfaced in prod. Already documented in `reference_clickhouse_cte_alias_collision.md`. Phase 7 / QNT-64 now has an explicit AC requiring real-SQL integration tests per router.
- **QNT-75 outage during ship (2026-05-02, ~10 min).** pydantic-settings v2 expects JSON for `list[str]` env vars; `.env.example` and the deploy guide both documented comma-separated. CORS_ALLOWED_ORIGINS landed in `.env.sops` using the documented format and crashed the api on startup. PR #187 hotfix (JSON-encode the env value) recovered prod; PR #188 added a `NoDecode + field_validator` parser that accepts both formats plus 5 regression tests in `tests/shared/test_config_list_parsing.py`. Pattern: docs promised one shape, code only honored another, and tests overrode fields directly without going through env parsing. Saved as `reference_pydantic_settings_list_parsing.md`.
- **Vercel framework auto-detection misread the monorepo.** With Root Directory set to `frontend` in the dashboard, detection still ran against repo root (Python uv workspace) and locked "no framework," deploying as 214 raw static files. Manual Build/Output overrides got a build but not Next.js runtime. Fixed by committing `frontend/vercel.json` with `framework=nextjs` (PR #185). Declarative; survives re-imports. Saved as `reference_vercel_framework_detection.md`.
- **`docker compose --remove-orphans` doesn't catch profile moves.** Caddy was moved from `prod` to dormant `prod-caddy` profile in QNT-75; `--remove-orphans` only removes containers whose service is undefined in compose. Caddy kept running on prod after deploy. Documented in `docs/guides/vercel-deploy.md` step 2 (manual cleanup). Saved as `reference_compose_remove_orphans_profiles.md`.

## Lessons saved to memory

- `feedback_backend_pack_consumer_loop.md` — backend "add columns/endpoints" tickets need 3-5 follow-up PRs once frontend exercises them; pair with first downstream consumer in same cycle
- `feedback_responsive_audit_one_ticket.md` — viewport / a11y / dark-mode polish ships as one audit ticket, not per-bug
- `reference_pydantic_settings_list_parsing.md` — list[str] env vars need NoDecode + field_validator to accept both JSON and comma-separated; test env-var parsing not just direct field overrides
- `reference_vercel_framework_detection.md` — Vercel Root Directory dashboard setting doesn't re-run detection; commit vercel.json with framework=nextjs to force it
- `reference_compose_remove_orphans_profiles.md` — orphan = "not defined in compose," not "not in active profile"

## Invariant guards

| Incident | Invariant | Guard |
|---|---|---|
| QNT-148 (CTE alias collision, prod 500) | API endpoints execute real SQL syntax in CI, not just mocked queries | NONE for the broader gap → modified Phase 7 / QNT-64 to mandate real-SQL integration tests per router |
| QNT-148 (Finnhub redirect 57% throttle) | Vendor rate-limits in code match prod behavior | ADR-016 + memory pin the empirical ceiling — accepted risk |
| QNT-163 (Finnhub `static.finnhub.io` → `static2.finnhub.io`) | Vendor URL surface change won't silently 4xx | regex-fullmatch + 13 regression tests in `tests/api/test_logos.py` + size-limit regression test pinning >= 1.5x largest observed PNG |
| QNT-75 (pydantic-settings list[str] crash) | list[str] settings parse the format documented in `.env.example` | `tests/shared/test_config_list_parsing.py` — 5 tests pinning JSON + comma-separated + whitespace + default + PROVENANCE_SOURCES |
| QNT-75 (Vercel detection trap) | Vercel auto-detects framework correctly for monorepo subdirs | `frontend/vercel.json` declarative override — can't drift |
| QNT-75 (caddy orphan after profile move) | Profile moves clean up orphaned containers | Documented manual step in `docs/guides/vercel-deploy.md` step 2 — accepted risk (rare, low-impact) |
| QNT-134 (partition mismatch breaking prod code-server) | CD hard gate covers per-job partition resolution | NONE — `feedback_dagster_resolve_asset_graph_blind_spot.md` notes the gap; no Ops ticket filed yet (broader Dagster topology stable post-QNT-116) |

**Same-shape clustering**: incidents (1) and (7) are both "static tests miss runtime invariants" — already a documented memory pattern (`feedback_aggregate_signals_hide_invariants.md`, `feedback_health_endpoint_is_not_durability.md`). Each gets a tactical regression test rather than one big architectural fix.

## Phase review (changes pushed forward to Phase 7)

- **modify QNT-86** (Sentry FastAPI integration) — explicitly scope around the existing hooks (`record_burst_alert`, `record_breaker_trip`, `sentry_sdk.init` guarded by `SENTRY_DSN`) wired by QNT-161, instead of treating as greenfield. New ACs include release-tagging via `GIT_SHA`, capturing chat-stream errors from QNT-150, and asserting burst/breaker hooks emit Sentry events when DSN is set.
- **modify QNT-64** (integration tests for critical paths) — add explicit AC requiring at least one test per API router that exercises the real ClickHouse client (no mocks) against a fixture-loaded test DB, pinned to catch the QNT-148 class of bugs.
- **modify QNT-65** (load test FastAPI) — re-scope to "validate Groq TPD circuit breaker under simulated load" instead of generic endpoint latency probing. The actual prod bottleneck per QNT-161 is Groq's free-tier TPD ceiling, not API CPU/IO. With 10 tickers + free-tier LLM, generic load testing exercises the wrong dimension.

## Next up: Phase 7 — Observability & Polish

Open issues: QNT-103 (observability stack), QNT-86 (Sentry), QNT-62 (Dagster alerting), QNT-63 (retry logic), QNT-65 (load test), QNT-64 (integration tests).

Suggested pull order based on Phase 6 lessons: **QNT-86 → QNT-64 → QNT-62**. QNT-86 unlocks observability for the QNT-161 abuse hooks already in prod; QNT-64 closes the mock-only-test gap that produced QNT-148; QNT-62 closes the Dagster materialization-failure alerting gap (QNT-125 lesson). QNT-103, QNT-63, QNT-65 are lower priority and can ship later in the cycle.
