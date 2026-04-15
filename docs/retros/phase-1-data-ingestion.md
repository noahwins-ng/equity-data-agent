Retrospective: Phase 1 — Data Ingestion
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Timeline: April 13 → April 15 (1 cycle)
Shipped:  4 issues, 4 PRs merged, 1 cancelled

Issues:
  QNT-40  Dagster resource: ClickHouse client       Done  PR #20
  QNT-41  Dagster asset: ohlcv_raw                  Done  PR #22
  QNT-42  Dagster asset: fundamentals               Done  PR #26
  QNT-43  Dagster schedules: daily OHLCV, weekly     Done  PR #29
  QNT-82  Implement make seed                       Cancelled (not needed)

What went well:
  - Fast execution — all 4 issues shipped in a single day
  - patterns.md recipes kept asset implementations consistent across ohlcv_raw and fundamentals
  - ReplacingMergeTree + StaticPartitionsDefinition = clean idempotent ingestion, no dedup logic needed
  - yfinance API integration was straightforward — retry policy + rate limiting handled edge cases
  - QNT-82 correctly identified as unnecessary and cancelled rather than built

What was harder than expected:
  - Execution AC verification failed 3 times (QNT-41, QNT-42, QNT-43)
    - Kept classifying "visible in UI" and "data populated" as code ACs instead of execution ACs
    - Root cause: /go command inlined a diluted version of /sanity-check logic
    - Fix: reworked /go to invoke sub-commands via Skill tool so full instructions load fresh
  - Linear MCP tool quirks: "Canceled" vs "Cancelled" spelling, can't unset milestone (requires UUID)

Lessons saved to memory:
  - AC verification: keyword trigger list updated with "toggle", UI-visible ACs need dev server running
  - /go rework: must invoke sub-commands, never inline their logic

Next up: Phase 2 — Calculation Layer (6 issues)
  QNT-70  ohlcv_weekly + ohlcv_monthly aggregation      Medium
  QNT-44  technical_indicators (daily/weekly/monthly)    High
  QNT-45  fundamental_summary (15 ratios)                (in plan)
  QNT-46  Dagster sensors for downstream recomputation   (in plan)
  QNT-68  Asset checks for data quality validation       (in plan)
  QNT-47  Validation tests: indicators vs external       Medium
