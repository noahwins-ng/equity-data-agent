ALTER TABLE equity_derived.fundamental_summary
    ADD COLUMN IF NOT EXISTS net_income_ttm Nullable(Float64);
