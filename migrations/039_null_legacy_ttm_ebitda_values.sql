-- QNT-382 follow-up data fix: rows written before 2026-07-18 carry the OLD
-- ebitda semantics - a TTM info snapshot, roughly 4x a single quarter. The
-- new EV/EBITDA and TTM-margin math rolls 4-quarter sums, and a legacy TTM
-- value inside a window would poison the sum with a unit-incompatible
-- number. NULL them out - rows re-ingested on or after the cutover carry
-- genuine per-period figures and are untouched. ReplacingMergeTree keeps
-- superseded versions around, so this also touches obsolete versions, which
-- is harmless (FINAL reads the newest version).
ALTER TABLE equity_raw.fundamentals UPDATE ebitda = NULL WHERE fetched_at < toDateTime('2026-07-18 09:00:00');
