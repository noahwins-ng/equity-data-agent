# Fundamental Ratio Validation Fixtures

Synthetic fundamentals with round-number inputs chosen so every expected ratio
can be verified by hand. Tests ensure `compute_fundamental_ratios()` produces
the exact values a reviewer would derive from the standard financial formulas.

## Layout

| File | Contents |
|---|---|
| `synthetic_fundamentals.csv` | 2 annual periods + 8 quarterly periods for a single synthetic ticker |

## Why synthetic (vs. a real ticker)

The AC calls for **exact match given same inputs** for fundamental ratios —
i.e. the formulas must produce specific values, not approximate them. Real
yfinance data drifts (restatements, changes in adjustments), which makes an
exact-match regression test brittle; synthetic inputs with clean numbers
make the expected values inspectable and stable.

## How to read the expected values

`tests/dagster/test_fundamental_ratios.py` uses `latest_close=200.00` and
computes expected ratios by hand from the committed CSV. The test file itself
is the source of truth for the formulas — every expected constant has a
comment showing the derivation.
