CREATE TABLE IF NOT EXISTS equity_derived.ohlcv_monthly (
    ticker      LowCardinality(String),
    month_start Date,
    open        Float64,
    high        Float64,
    low         Float64,
    close       Float64,
    adj_close   Float64,
    volume      UInt64,
    computed_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY ticker
ORDER BY (ticker, month_start);
