# Indicator Validation Fixtures

Deterministic fixture data for `packages/dagster-pipelines/.../technical_indicators.py`.

## Files

| File | Rows | Notes |
|---|---|---|
| `AAPL_ohlcv_2023_2024.csv` | 501 | Daily OHLCV 2023-01-03 → 2024-12-30 |
| `MSFT_ohlcv_2023_2024.csv` | 501 | Daily OHLCV 2023-01-03 → 2024-12-30 |
| `AAPL_indicators_expected.csv` | 501 | Snapshot of `compute_indicators(AAPL)` output |
| `MSFT_indicators_expected.csv` | 501 | Snapshot of `compute_indicators(MSFT)` output |

## Data source

Daily OHLCV was fetched once from Yahoo Finance via `yfinance` (`auto_adjust=False`)
and committed verbatim. The pipeline's production path uses the same fetcher, so
tests exercise the same column shape, `adj_close` semantics, and split adjustments
that prod sees.

Expected indicator snapshots are the raw output of `compute_indicators()` run on the
committed OHLCV fixtures. They are regenerated whenever the indicator definitions
intentionally change — see `docs/guides/regenerate-indicator-fixtures.md`.

## External cross-reference

The indicators in `compute_indicators()` follow industry-standard definitions:

- **RSI-14** — Wilder (1978), exponential smoothing with α = 1/14 — matches TradingView
  and Yahoo Finance defaults.
- **MACD (12, 26, 9)** — Appel's classic definition; identical to TradingView default.
- **Bollinger Bands (20, 2)** — 20-period SMA ± 2 sample std (ddof=1); matches
  TradingView default.

`test_indicator_validation.py::test_<rsi|macd>_matches_canonical_reference_<ticker>`
re-derives RSI-14 and MACD from the fixture prices using a scalar Python loop
implementation of the Wilder/Appel recurrences. The scalar reference shares no code
with `compute_indicators()` (which uses `pandas.ewm`/`rolling`), so agreement
within 1% validates that our vectorized path produces the same numbers a trader
would see in TradingView or Yahoo Finance for these tickers on these dates.
