ALTER TABLE equity_derived.fundamental_summary
    ADD COLUMN IF NOT EXISTS net_margin_bps_yoy Nullable(Float64);
