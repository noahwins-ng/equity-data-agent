ALTER TABLE equity_derived.technical_indicators_weekly
    ADD COLUMN IF NOT EXISTS sma_200 Nullable(Float64),
    ADD COLUMN IF NOT EXISTS adx_14 Nullable(Float64),
    ADD COLUMN IF NOT EXISTS atr_14 Nullable(Float64),
    ADD COLUMN IF NOT EXISTS obv Nullable(Float64),
    ADD COLUMN IF NOT EXISTS bb_pct_b Nullable(Float64),
    ADD COLUMN IF NOT EXISTS macd_bullish_cross UInt8 DEFAULT 0;
