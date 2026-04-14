CREATE TABLE IF NOT EXISTS equity_raw.fundamentals (
    ticker               LowCardinality(String),
    period_end           Date,
    period_type          LowCardinality(String),
    revenue              Float64,
    gross_profit         Float64,
    net_income           Float64,
    total_assets         Float64,
    total_liabilities    Float64,
    current_assets       Float64,
    current_liabilities  Float64,
    free_cash_flow       Float64,
    ebitda               Float64,
    total_debt           Float64,
    cash_and_equivalents Float64,
    shares_outstanding   UInt64,
    market_cap           Float64,
    fetched_at           DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY ticker
ORDER BY (ticker, period_end, period_type);
