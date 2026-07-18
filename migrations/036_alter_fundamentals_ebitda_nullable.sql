-- QNT-382 follow-up: ebitda becomes the per-period income-statement figure
-- (EBITDA / Normalized EBITDA line as of that period). A period yfinance has
-- no line for carries NULL - never the TTM info snapshot stamped across
-- history, which was a different unit (trailing twelve months) on quarterly
-- rows. Comment text must avoid semicolons and quotes (runner splitter).
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN ebitda Nullable(Float64);
