ALTER TABLE equity_raw.fundamentals ADD COLUMN IF NOT EXISTS implied_shares_outstanding UInt64 DEFAULT shares_outstanding AFTER shares_outstanding;
