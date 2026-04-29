ALTER TABLE equity_derived.fundamental_summary
    ADD COLUMN IF NOT EXISTS gross_margin_bps_yoy Nullable(Float64);
