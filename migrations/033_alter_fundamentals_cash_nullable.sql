-- QNT-382: missing-vs-zero, cash side. A period where yfinance omits Cash And
-- Cash Equivalents must land NULL, not 0.0 -- zero overstates EV (EV = mcap +
-- debt - cash) instead of marking the ratio not meaningful.
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN cash_and_equivalents Nullable(Float64);
