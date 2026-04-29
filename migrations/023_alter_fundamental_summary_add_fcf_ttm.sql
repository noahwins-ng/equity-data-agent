ALTER TABLE equity_derived.fundamental_summary
    ADD COLUMN IF NOT EXISTS fcf_ttm Nullable(Float64);
