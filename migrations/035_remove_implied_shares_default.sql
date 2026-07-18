-- QNT-382: drop the inert DEFAULT shares_outstanding left from 025. The asset
-- always supplies implied_shares_outstanding explicitly, and a default that
-- copies the per-period share count would silently contradict the
-- newest-period-only snapshot semantics if an insert ever omitted the column.
ALTER TABLE equity_raw.fundamentals MODIFY COLUMN implied_shares_outstanding REMOVE DEFAULT;
