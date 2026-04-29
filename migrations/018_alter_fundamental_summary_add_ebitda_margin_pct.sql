ALTER TABLE equity_derived.fundamental_summary
    ADD COLUMN IF NOT EXISTS ebitda_margin_pct Nullable(Float64);
