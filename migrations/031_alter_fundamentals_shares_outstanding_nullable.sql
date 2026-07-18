-- QNT-382: shares_outstanding is now the per-period balance-sheet count
-- (Ordinary Shares Number as of period end). A period yfinance has no count
-- for carries NULL rather than the current snapshot stamped across history.
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN shares_outstanding Nullable(UInt64);
