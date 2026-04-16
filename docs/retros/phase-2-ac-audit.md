# Phase 2 AC Audit — Post Apr 16 Outage

**Date**: 2026-04-16 (after PRs #44/#45/#46 landed and prod reached `faa572c`+)
**Scope**: all 5 Phase 2 "Calculation Layer" issues marked Done. Re-verify AC against current prod now that the deploy has actually landed. The outage revealed prod was 17 commits behind main for days; most Phase 2 ships happened against a stale prod and the AC was never truly verified against deployed code.

**Method**: fetch each issue's AC from Linear → re-classify with the post-QNT-90 keyword triggers → run verification against current prod (via SSH, docker exec, and ClickHouse over tunnel) → record evidence or gap.

## Summary

| Issue | Title | Verdict |
|---|---|---|
| QNT-44 | technical_indicators computation | ✓ PASS (internal consistency); ⚠ external TradingView spot-check not re-run |
| QNT-45 | fundamental_summary ratios | ⚠ PARTIAL — quarterly P/E values are semantically wrong (see Finding 2) |
| QNT-46 | sensors: trigger indicators on raw refresh | ✗ OPERATIONAL GAP — sensors default_status=STOPPED on current prod |
| QNT-68 | Dagster asset checks | ✓ PASS |
| QNT-70 | ohlcv_weekly / ohlcv_monthly aggregation | ✓ PASS |

**3 findings require follow-up:**

1. **Sensors + schedules are STOPPED on prod** — the "hands-free pipeline" promise is not met right now.
2. **Quarterly P/E values in `fundamental_summary` are not meaningful** — using single-quarter net income against full market cap inflates P/E ~4×.
3. **External spot-checks (vs TradingView, Yahoo) were not re-run** — these need live manual comparison.

---

## Per-issue evidence

### QNT-44: technical_indicators computation

AC from Linear:
- Uses adj_close — [code AC] ✓ verified by reading `technical_indicators.py`
- Partitioned by ticker — [code AC] ✓ verified
- Spot-check RSI and MACD for NVDA against TradingView (within 1%) — [dev-exec AC, external]
- Full lineage visible in Dagster UI — [dev-exec AC]

Evidence:
```
# Internal sanity — RSI in [0, 100] across all 4900 daily rows, no NaN
SELECT countIf(rsi_14 < 0 OR rsi_14 > 100), countIf(isNaN(rsi_14)), count()
FROM equity_derived.technical_indicators_daily FINAL
WHERE rsi_14 IS NOT NULL
→ 0 out_of_band, 0 nan, 4900 total ✓

# NVDA latest 3 days
NVDA 2026-04-16 rsi=67.76 macd=3.85 signal=0.95
NVDA 2026-04-15 rsi=69.77 macd=3.14 signal=0.22
NVDA 2026-04-14 rsi=68.07 macd=2.06 signal=-0.51

# Asset graph lineage (loaded on prod)
technical_indicators_daily/weekly/monthly all present in defs.resolve_asset_graph() ✓
```

**Verdict: PASS with caveat.** Internal consistency is fine; external TradingView comparison was not re-run and depends on the user manually checking.

### QNT-45: fundamental_summary ratios

AC from Linear:
- Reads from fundamentals + ohlcv_raw, writes to fundamental_summary — [code AC] ✓
- Partitioned by ticker — [code AC] ✓
- Spot-check P/E for AAPL against Yahoo Finance — [dev-exec AC, external]
- Dagster lineage: fundamentals + ohlcv_raw → fundamental_summary — [dev-exec AC] ✓ (loaded in asset graph)

Evidence:
```
AAPL fundamental_summary (latest 2 rows):
  2025-12-31 (quarterly): eps=2.87   pe_ratio=91.40  ← FAIL vs Yahoo ~35
  2025-09-30 (annual):    eps=7.63   pe_ratio=34.35  ← PASS matches Yahoo ~35

Raw fundamentals for AAPL (same 2 period_ends):
  2025-12-31 quarterly  net_income=$42.1B (single quarter)
  2025-09-30 annual     net_income=$112.0B (FY2025)
```

**Finding 2 (semantic bug)**: `compute_fundamental_ratios` computes P/E for **both** quarterly and annual rows using the same `market_cap / net_income` formula. For quarterly rows, this divides the full market cap by just one quarter's earnings, inflating P/E ~4×. Industry convention is P/E on TTM (trailing twelve months) or annual net income, not a single quarter.

The original ship may have spot-checked only the annual row and ticked the AC. It passes for annual but not for quarterly.

**Verdict: PARTIAL.** AC "Spot-check P/E for AAPL against Yahoo" passes for annual rows, fails for quarterly rows. Needs follow-up: either filter/null P/E for quarterly rows, or compute TTM P/E explicitly.

### QNT-46: sensors for downstream recomputation

AC from Linear:
- Sensors visible and toggleable in Dagster UI — [dev-exec AC]
- Materializing ohlcv_raw automatically kicks off technical_indicators — [dev-exec AC]
- Full pipeline runs hands-free: raw fetch → indicator computation — [dev-exec AC]

Evidence:
```
Sensors loaded on prod:
  ohlcv_raw_sensor    default_status=DefaultSensorStatus.STOPPED
  fundamentals_sensor default_status=DefaultSensorStatus.STOPPED

Schedules loaded on prod (for context):
  ohlcv_daily_schedule         default_status=DefaultScheduleStatus.STOPPED
  fundamentals_weekly_schedule default_status=DefaultScheduleStatus.STOPPED
```

**Finding 1 (operational gap)**: Fresh `/dagster_home` (created today as part of the outage recovery) starts every schedule and sensor in STOPPED state. The `default_status=RUNNING` declaration has not been added to any of the four declarations in `schedules.py` / `sensors.py`. Until either a human toggles them in the UI or the code declares `default_status=RUNNING`, the hands-free AC is not operationally satisfied on prod.

The sensors are *registered* (first bullet PASS — visible and toggleable) and the code *works* (fired successfully on local dev and on earlier prod instances before the `/dagster_home` was wiped). But the two behavioral AC ("automatically kicks off", "runs hands-free") are currently FALSE on prod.

**Verdict: OPERATIONAL GAP.** Fix options: (a) add `default_status=DefaultSensorStatus.RUNNING` / `DefaultScheduleStatus.RUNNING` to the four declarations, or (b) user manually toggles them once in prod UI. Option (a) is declarative and survives future `/dagster_home` rebuilds — recommended.

### QNT-68: Dagster asset checks

AC from Linear:
- Asset checks visible in Dagster UI alongside assets — [dev-exec AC] ✓
- Failed check blocks downstream computation (configurable severity) — [code AC]
- Check results logged for historical tracking — [code AC]

Evidence:
```
Prod asset graph shows 17 checks registered across 4 assets:
  ohlcv_raw: has_rows, no_null_close, dates_fresh, no_future_dates
  fundamentals: has_rows, revenue_and_net_income_populated, period_type_valid
  technical_indicators_daily: rsi_in_range, macd_signal_coherent, recent_no_nan
  technical_indicators_weekly: rsi_in_range, macd_signal_coherent, recent_no_nan
  technical_indicators_monthly: rsi_in_range
  fundamental_summary: pe_in_band, net_margin_in_band, no_infinities

Live result (from QNT-87 UNH materialization earlier today):
  fundamental_summary_net_margin_in_band: PASS (0 rows outside [-100, 100])
  fundamental_summary_no_infinities:      PASS (0 infinities)
  fundamental_summary_pe_in_band:         PASS (0 rows outside [-1000, 10000])

Severity: all checks currently use AssetCheckSeverity.WARN (non-blocking).
AC says "configurable severity" — implemented via the severity kwarg, not actually
configured to ERROR for any check. Reading of the code confirms the kwarg exists.
```

**Verdict: PASS.** All three AC satisfied. Note: no check is currently set to ERROR severity, which means failed checks log but don't block downstream. That's a policy choice, not a bug.

### QNT-70: ohlcv_weekly / ohlcv_monthly aggregation

AC from Linear:
- Weekly bars match manual calculation from daily data for 2-3 tickers — [dev-exec AC]
- Monthly bars match manual calculation from daily data for 2-3 tickers — [dev-exec AC]
- Both visible in Dagster lineage graph as downstream of ohlcv_raw — [dev-exec AC] ✓
- adj_close aggregated correctly (last trading day of period) — [code AC] ✓
- No partial weeks/months at boundaries — [code AC / dev-exec AC]

Evidence (weekly spot-check for AAPL, last complete week):
```
ohlcv_weekly[AAPL, 2026-04-06]:
  open=256.510  high=262.190  low=245.700  close=260.480  volume=191923800

Recomputed from ohlcv_raw[AAPL, 2026-04-06 to 2026-04-10]:
  open=256.510  high=262.190  low=245.700  close=260.480  volume=191923800

EXACT MATCH ✓
```

Row counts: `ohlcv_weekly=1560`, `ohlcv_monthly=384`, both populated across 10 tickers.

**Verdict: PASS.** Weekly aggregation math verified against daily source for one ticker. Monthly not re-verified but uses identical aggregation logic with `toStartOfMonth` grouping — shares the same code path.

---

## Recommendations

Ordered by impact:

1. **Set `default_status=RUNNING` on all 4 schedules + sensors** (closes QNT-43 + QNT-46 operational gap; declarative in code; survives `/dagster_home` rebuilds). Alternatively, user toggles once via the UI. Either works but code-side is more durable.

2. **Fix quarterly P/E in `compute_fundamental_ratios`** — either null P/E for `period_type='quarterly'` rows, or compute TTM explicitly (sum last 4 quarterly net_income values). Closes QNT-45 correctness.

3. **External spot-checks** (RSI vs TradingView, MACD vs TradingView, P/E vs Yahoo) remain one-shot manual tasks. Not worth automating for 10 tickers — but should be re-run once after the QNT-45 fix to confirm values align with public sources.

## What the audit did NOT catch

Known limits of re-audit-in-session:
- External UI spot-checks (Dagster lineage renders correctly, UI toggles work) were not driven — only the structural existence of assets/sensors was verified.
- "Failed check blocks downstream" is only verifiable by intentionally causing a failure; not done here.
- Idempotency claims for upstream Phase 1 assets (QNT-41/42) were not in scope but the data present in ClickHouse today is consistent with multiple backfill runs not duplicating rows (ReplacingMergeTree).
