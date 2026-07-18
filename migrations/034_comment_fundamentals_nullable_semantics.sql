-- QNT-382: refresh column comments from 026 that still described the old
-- snapshot-stamped semantics. Comment text must avoid semicolons (the runner
-- splits on every semicolon, including inside SQL comments).
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN shares_outstanding 'Share count as of this period end from the balance sheet (Ordinary Shares Number). Newest period falls back to the current yfinance snapshot. NULL when unavailable (QNT-382).';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN implied_shares_outstanding 'All-class share count (market cap basis). Point-in-time snapshot stamped only on the newest period, NULL on history (QNT-382).';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN total_debt 'Total debt for the period. NULL when yfinance omits the line item - never zero-coerced (QNT-382).';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN cash_and_equivalents 'Cash and equivalents for the period. NULL when yfinance omits the line item - never zero-coerced (QNT-382).';
