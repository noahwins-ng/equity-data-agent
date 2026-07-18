-- QNT-382 follow-up: refresh column comments to the per-period semantics.
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN ebitda 'EBITDA for this period from the income statement (EBITDA or Normalized EBITDA line). NULL when yfinance omits it - never the TTM info snapshot (QNT-382 follow-up).';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN market_cap 'Point-in-time yfinance marketCap snapshot, stamped only on the newest period, NULL on history. Unread downstream - live surfaces recompute close x shares (QNT-382 follow-up).';
