CREATE TABLE IF NOT EXISTS equity_raw.earnings_calendar (
    ticker             LowCardinality(String),
    next_earnings_date Date,
    fetched_at         DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY ticker
ORDER BY (ticker);
