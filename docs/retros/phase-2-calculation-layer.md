# Retrospective — Phase 2: Calculation Layer

**Timeline:** 2026-04-15 11:44 UTC → 2026-04-16 16:46 UTC (~29 hours elapsed, single cycle)
**Shipped:** 12 issues, 12 PRs merged (6 planned + 6 reactive); 1 cancelled (QNT-82 `make seed`)

## What shipped

### Planned scope (the milestone proper)

| Issue | Deliverable | PR |
|---|---|---|
| QNT-70 | `ohlcv_weekly` + `ohlcv_monthly` aggregation assets | #33 |
| QNT-44 | `technical_indicators_daily/weekly/monthly` (RSI-14, MACD, SMA, EMA, BB) | #35 |
| QNT-45 | `fundamental_summary` (15 ratios across 5 categories) | #36 |
| QNT-46 | Sensors: raw materializations trigger downstream recomputation | #37 + #38 |
| QNT-68 | 17 Dagster asset checks across 6 assets | #40 |
| QNT-47 | Validation tests: snapshot + canonical Wilder/Appel cross-reference for 2 tickers, exact-match fundamentals | #49 |

### Reactive follow-ups (not in the original plan — opened during/after the outage)

| Issue | Deliverable | PR |
|---|---|---|
| QNT-87 | Null out P/E when `|EPS| < $0.10` (N/M convention) | #43 |
| QNT-88 | CD asserts prod git SHA matches merged commit | #44 |
| QNT-89 | CD asserts Dagster loads expected asset graph | #45 |
| QNT-90 | Harden `/go` pipeline AC gates | #46 |
| QNT-91 | Quarterly P/E uses TTM net_income | #48 |
| QNT-92 | `default_status=RUNNING` on sensors + schedules | #50 |

Plus two non-QNT infra fixes (PR #41 prod Dagster persistence + UI tunnel, PR #42 deploy fails loudly on drift) and the audit doc itself (PR #47).

## Velocity

6 planned ships in the first ~12 hours (QNT-70 → QNT-68), then the Apr 16 outage fractured the rest of the phase into 6 reactive ships over the next ~18 hours. Average shipping cadence was ~2.5 hours per issue, including sanity-check, review, PR, CI, deploy, and post-deploy verification — which is fast because most items were single-file or single-module changes.

## What went well

- **Calculation-layer scope landed cleanly.** All 10 tickers produce daily/weekly/monthly indicators + quarterly/annual fundamental ratios automatically, with Dagster sensors wiring the pipeline end-to-end.
- **Asset checks immediately earned their keep.** QNT-68's `pe_in_band` bound (-1000, 10000) caught two distinct P/E formula bugs (UNH near-zero EPS inflating to 28,545; quarterly P/E using single-quarter income instead of TTM) that both passed human code review. Justifies asset checks as real bug-finders, not decoration.
- **Validation uses independent references, not new deps.** QNT-47 re-derives RSI-14 and MACD from a scalar Wilder/Appel Python loop that shares no code with the `pandas.ewm`/`rolling`-based production path. Agreement within 1% validates that our output matches what TradingView/Yahoo show, with zero new dependencies.
- **Post-outage hardening was declarative.** QNT-88/89/90/92 all translate "I manually checked this post-deploy" into a gate that runs every deploy forever.

## What was harder than expected

- **The Apr 16 outage exposed three independent silent-failure modes at once.** CD reported green while prod was 17 commits behind main (git pull aborted on local drift, `set -e` missing in the bash heredoc, Docker layer cache reused old images, `/health` still 200). This burned the second half of the cycle on remediation rather than feature work.
- **Runtime state drift.** Sensors + schedules were operationally RUNNING only because UI toggles had been flipped manually and persisted in `/dagster_home`. The outage rebuild wiped that state; the code defaults (STOPPED) silently won. Fixed with QNT-92.
- **Semantic bugs can hide in "AC-checked" work.** QNT-45's quarterly P/E bug passed the original ship's spot-check because only annual rows were sampled. Caught by the Phase 2 AC audit re-reading each AC against current prod.
- **QNT-46 sensor rewrite.** Initial implementation processed one event per tick (PR #37); partition catch-up was too slow. Rewritten to batch all new events per tick (PR #38) before the rest of the phase could proceed.

## Key lessons (saved to memory)

- **`feedback_asset_checks_catch_real_bugs`** — Set real domain bounds on asset checks (P/E 10k, margins ±100%, RSI 0-100). "Not null" checks don't catch formula bugs; real-bound checks do.
- **`feedback_deploy_green_isnt_code_deployed`** — CD green + `/health` green ≠ new code running. Always assert SHA + runtime-load identity. `set -euo pipefail` on every bash heredoc in CD.
- **`feedback_runtime_state_must_be_declarative`** — Any stateful runtime config (Dagster default_status, cron enabled, feature flags, log levels) must be declared in code. "It works because someone toggled it two weeks ago" is an invisible dependency.
- **`feedback_linear_links_resets_state`** — `save_issue(links=...)` on Linear can silently revert status (In Review → In Progress). Re-assert state after attaching or at /ship Step 7.
- **`feedback_sensor_batch_from_day_one`** (new) — Dagster sensors should batch all pending events per tick from day one. Single-event-per-tick is too slow for catch-up and forced a mid-phase rewrite.
- **`feedback_sample_ac_broadly`** (new) — AC spot-checks must vary across all row-type dimensions. Checking only annual rows missed the quarterly P/E bug; two follow-up PRs (QNT-87, QNT-91) were needed to fix what one broad sample would have caught up front.

## System-overview updates

Added the CD hard-gate step to the Infrastructure section (SHA match + asset-graph load). Added a note to `equity_derived.fundamental_summary` documenting the TTM-quarterly-P/E + N/M-threshold behaviour. Added a "Data quality" line summarising the 17 asset checks registered.

## Phase review — applying lessons to upcoming phases

Phase 2 taught four concrete things. Each was cross-referenced against the upcoming phase specs, producing four actioned scope changes rather than just flags.

| Phase | Action | Issue | Change | Lesson applied |
|---|---|---|---|---|
| 3 | modify | QNT-69 | Report templates must specify null/N/M display conventions (near-zero EPS → N/M, quarterly TTM, warm-up Insufficient-data) | QNT-87 + QNT-91 |
| 3 | modify | QNT-51 | `/health` exposes `deploy.git_sha`, `dagster_assets`, `dagster_checks` for runtime identity verification | QNT-88 + QNT-89 |
| 4 | modify | QNT-53 | News schedule declares `default_status=RUNNING`; downstream sensor batches all pending events per tick from day one | QNT-46 + QNT-92 |
| 4 | add    | QNT-93 | Dagster asset checks for `news_raw` and `news_embeddings` (real domain bounds, not just "not null") | QNT-68 |

All four updated in Linear with audit-trail comments. Plan (`docs/project-plan.md`) and spec (`docs/project-requirement.md`) updated inline. No ADR warranted — these refine existing requirements rather than changing architecture.

Phases 5, 6, 7 reviewed — no changes warranted. The "interpret, don't calculate" agent boundary is validated by Phase 2 (all math lives in Dagster). Frontend null handling is already implicit in the Phase 6 spec. Phase 7 observability is covered by the CD hardening that already shipped.

## Up next — Phase 3: API Layer

FastAPI endpoints that turn the computed data into reports for the agent and JSON arrays for the frontend.

Suggested pull for the first Phase-3 cycle (capped at ~6 issues — Phase 2's observed core-feature velocity, excluding the outage surge):

| Priority | Issue | Notes |
|---|---|---|
| High | QNT-69 | Design report templates (now includes null/N/M conventions) — unblocks all `/reports/*` |
| High | QNT-48 | `/reports/technical/{ticker}` — template for the other report endpoints |
| High | QNT-76 | `/ohlcv/{ticker}?timeframe=` — feeds the frontend candlestick chart |
| High | QNT-77 | `/indicators/{ticker}?timeframe=` — pairs with QNT-76 for chart overlays |
| High | QNT-51 | `/health` enhanced (services + deploy identity) |
| Medium | QNT-78 | `/tickers` — cheap utility, good first-endpoint test |

## Timeline reference

```
2026-04-15 11:44  QNT-70 ohlcv_weekly/monthly           PR #33
2026-04-15 14:27  QNT-44 technical_indicators           PR #35
2026-04-15 15:15  QNT-45 fundamental_summary            PR #36
2026-04-15 15:38  QNT-46 sensors (after rewrite)        PR #37+#38
2026-04-16 12:52  QNT-68 asset checks                   PR #40
2026-04-16 14:47  QNT-87 P/E N/M threshold              PR #43
2026-04-16 15:03  QNT-88 CD SHA gate                    PR #44
2026-04-16 15:06  QNT-89 CD asset-graph gate            PR #45
2026-04-16 15:32  QNT-90 /go hardening                  PR #46
2026-04-16 16:03  QNT-91 quarterly P/E TTM              PR #48
2026-04-16 16:32  QNT-47 validation tests               PR #49
2026-04-16 16:46  QNT-92 default_status=RUNNING         PR #50
```
