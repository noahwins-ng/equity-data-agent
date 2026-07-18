-- QNT-382: implied_shares_outstanding is a point-in-time yfinance snapshot
-- with no historical series. The ingest now stamps it only on the newest
-- period and leaves older periods NULL instead of rewriting history with the
-- current count. Modified first so 031 can make the column referenced by its
-- DEFAULT expression nullable with matching types (the inherited DEFAULT
-- itself is removed in 035 - MODIFY COLUMN with only a type keeps it).
-- NOTE: the migration runner splits on every semicolon and does not
-- understand -- comments, so comment text must avoid semicolons and quotes.
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN implied_shares_outstanding Nullable(UInt64);
