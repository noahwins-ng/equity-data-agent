CREATE TABLE IF NOT EXISTS equity_derived.technical_indicators_weekly (
    ticker      LowCardinality(String),
    week_start  Date,
    sma_20      Nullable(Float64),
    sma_50      Nullable(Float64),
    ema_12      Nullable(Float64),
    ema_26      Nullable(Float64),
    rsi_14      Nullable(Float64),
    macd        Nullable(Float64),
    macd_signal Nullable(Float64),
    macd_hist   Nullable(Float64),
    bb_upper    Nullable(Float64),
    bb_middle   Nullable(Float64),
    bb_lower    Nullable(Float64),
    computed_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY ticker
ORDER BY (ticker, week_start);
