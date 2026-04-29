ALTER TABLE equity_derived.fundamental_summary
    ADD COLUMN IF NOT EXISTS revenue_ttm Nullable(Float64);
