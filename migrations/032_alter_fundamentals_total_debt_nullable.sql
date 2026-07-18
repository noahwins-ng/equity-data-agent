-- QNT-382: missing-vs-zero. A period where yfinance omits Total Debt must
-- land NULL, not 0.0 -- zero reads as debt-free (debt_to_equity 0) and
-- understates EV/EBITDA. NULL propagates NaN through fundamental_summary and
-- renders N/M in reports.
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN total_debt Nullable(Float64);
