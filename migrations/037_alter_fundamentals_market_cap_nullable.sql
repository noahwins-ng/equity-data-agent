-- QNT-382 follow-up: market_cap is a pure info snapshot with no per-period
-- source and no downstream reader (API surfaces recompute live from close x
-- shares). Stamped only on the newest period, NULL on history.
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN market_cap Nullable(Float64);
