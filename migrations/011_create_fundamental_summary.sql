CREATE TABLE IF NOT EXISTS equity_derived.fundamental_summary (
    ticker             LowCardinality(String),
    period_end         Date,
    period_type        LowCardinality(String),
    -- Valuation
    pe_ratio           Nullable(Float64),
    ev_ebitda          Nullable(Float64),
    price_to_book      Nullable(Float64),
    price_to_sales     Nullable(Float64),
    eps                Nullable(Float64),
    -- Growth
    revenue_yoy_pct    Nullable(Float64),
    net_income_yoy_pct Nullable(Float64),
    fcf_yoy_pct        Nullable(Float64),
    -- Profitability
    net_margin_pct     Nullable(Float64),
    gross_margin_pct   Nullable(Float64),
    roe                Nullable(Float64),
    roa                Nullable(Float64),
    -- Cash
    fcf_yield          Nullable(Float64),
    -- Leverage
    debt_to_equity     Nullable(Float64),
    -- Liquidity
    current_ratio      Nullable(Float64),
    computed_at        DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY ticker
ORDER BY (ticker, period_end, period_type);
