CREATE TABLE IF NOT EXISTS equity_raw.ohlcv_raw (
    ticker       LowCardinality(String),
    date         Date,
    open         Float64,
    high         Float64,
    low          Float64,
    close        Float64,
    adj_close    Float64,
    volume       UInt64,
    fetched_at   DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY ticker
ORDER BY (ticker, date);
