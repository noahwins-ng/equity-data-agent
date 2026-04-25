# Phase 4 Retro Sweep — Asset Check Composite-Key Aggregation Audit (QNT-122)

**Trigger.** QNT-120 exposed a silent off-by-dedup bug in
`news_embeddings_vector_count_matches_source`: the asset writes one Qdrant point
per `(ticker, url_id)` but the check compared per-ticker Qdrant counts to
`count() FROM news_raw FINAL GROUP BY ticker`. ReplacingMergeTree on `news_raw`
keys `(ticker, published_at, id)`, so an RSS feed bumping `published_at` for an
existing URL produced two source rows but one Qdrant point — flagged as drift
for 9/10 tickers. PR #105 switched the check to `uniqExact(id) GROUP BY ticker`,
matching the asset's effective key. `feedback_fix_pattern_not_example.md`
requires we sweep for every other instance of the class.

## Method

For each `*_checks.py` file under
`packages/dagster-pipelines/src/dagster_pipelines/asset_checks/`:

1. Classify each check as **cross-store** (compares cardinality between two
   distinct stores or tables) or **single-table** (range / invariant / presence
   on one table only).
2. For cross-store checks, paste the upstream-side aggregation SQL and the
   downstream's effective key, then verify they agree at the same grain.
3. Single-table checks are out of scope for this class of bug — the off-by-dedup
   pattern only fires when an aggregation on store A claims to be a count of
   the items materialised in store B. They are inventoried for completeness.

## Per-file findings

### `news_embeddings_checks.py`

| Check | Class | Upstream agg | Downstream key | Verdict |
|---|---|---|---|---|
| `news_embeddings_vector_count_matches_source` | cross-store (CH ↔ Qdrant) | `uniqExact(id) GROUP BY ticker` from `equity_raw.news_raw FINAL` | Qdrant point ID = `blake2b(f"{ticker}:{news_raw.id}")` — one point per `(ticker, news_raw.id)`. The `url_id` parameter name in `point_id(ticker, url_id)` is bound to `int(record["id"])` at the call site (`news_embeddings.py:124`) | ✓ pass |
| `news_embeddings_no_orphaned_vectors` | cross-store (CH ↔ Qdrant) | per-ticker `id` set from `news_raw FINAL`, lifted to Python set of `point_id(ticker, int(i))` | same as above | ✓ pass — RMT's `(ticker, published_at, id)` order-by keeps a separate row whenever `published_at` bumps for an existing `(ticker, id)` (the RSS-republish case); the Python set keyed on `point_id(ticker, id)` collapses those repeats to one expected point, matching the upsert |
| `news_embeddings_embedding_dimension` | single-store (Qdrant config) | n/a | n/a | ✓ pass — collection-config invariant, not cardinality |

The `vector_count_matches_source` fix (PR #105) is the canonical pattern for
this class: when the asset's effective key is a strict subset of the source
table's RMT order-by tuple (here `(ticker, id)` ⊂ `(ticker, published_at, id)`),
the upstream side **must** aggregate by the asset key (`uniqExact(<asset_key>)`),
not row count. The orphan check sidesteps the issue by lifting both sides into
Python sets keyed on the asset's `point_id(ticker, news_raw.id)` — duplicates
collapse on set construction.

### `technical_indicators_checks.py`

Seven checks across daily / weekly / monthly: `_rsi_check_result` (range
`[0,100]`), `_macd_signal_check_result` (per-ticker latest-row coherence via
`argMax(macd, <date_col>)` / `argMax(macd_signal, <date_col>)` from the
indicator table itself, where `<date_col>` is `date` for daily and
`week_start` for weekly), `_recent_nan_check_result` (NULL counts in the most
recent 30 bars per ticker via
`row_number() OVER (PARTITION BY ticker ORDER BY <date_col> DESC)`).
**No cross-table count comparisons.** Each indicator table is RMT keyed
`(ticker, date)` matching `ohlcv_raw`'s `(ticker, date)` 1:1; if a future check
ever compares per-ticker indicator counts against `ohlcv_raw`, both sides can
use plain `count() FINAL GROUP BY ticker` because the keys are identical (no
extra dimension to collapse). ✓ pass — no off-by-dedup risk by construction.

### `fundamentals_checks.py`

Three checks on `equity_raw.fundamentals` (RMT key `(ticker, period_end,
period_type)`): `fundamentals_has_rows` (global `count() FINAL > 0`),
`fundamentals_period_type_valid` (membership in `{quarterly, annual}`),
`fundamentals_revenue_and_net_income_populated` (per-ticker
`countIf(revenue != 0 OR net_income != 0)` HAVING `populated_rows = 0`). All
single-table on the source — no downstream store / asset-key comparison. The
`HAVING populated_rows = 0` aggregation operates on the same grain RMT
materialises (one row per `(ticker, period_end, period_type)`) and asks a
populatedness question, not a cardinality question, so the QNT-93 class does
not apply. ✓ pass.

### `fundamental_summary_checks.py`

Three checks on `equity_derived.fundamental_summary` (RMT key `(ticker,
period_end, period_type)` — same grain as upstream `fundamentals`):
`pe_in_band` (range `[-10000, 10000]`), `net_margin_in_band` (range `[-100,
100]`), `no_infinities` (`countIf(isInfinite(col))` over 15 ratio columns).
All band / invariant checks on a single table; no count comparison against
`fundamentals` or any other source. ✓ pass.

### `news_raw_checks.py`

Five checks on `equity_raw.news_raw`: `has_rows` (`count() AS row_count FROM
news_raw FINAL GROUP BY ticker` returns the per-ticker row count, then Python
cross-references against `shared.tickers.TICKERS` to flag missing or
zero-count tickers — presence, not cardinality),
`no_empty_headlines` (`countIf(empty(trim(headline)))`), `valid_urls`
(`countIf NOT startsWith http(s)://`), `no_future_published_at`
(`published_at > now() + tolerance`), `recent_ingestion`
(`max(fetched_at) GROUP BY ticker` staleness). All single-table — `news_raw`
is the only store touched and no per-ticker cardinality is compared against
another store. The `has_rows` check uses raw `count()` but the question is
"is this group non-empty?" which is invariant under RMT row duplication. ✓
pass.

### `ohlcv_raw_checks.py`

Four checks on `equity_raw.ohlcv_raw` (RMT key `(ticker, date)`):
`has_rows` (global `count() FINAL > 0`), `no_null_close`
(`count WHERE close IS NULL`), `no_future_dates` (`count WHERE date >
today()`), `dates_fresh` (`dateDiff('day', max(date), today())` global
staleness). All single-table invariants; no cross-store count. ✓ pass.

## Summary

| File | Checks | Cross-store | Off-by-dedup risk | Verdict |
|---|---:|---:|---|---|
| `news_embeddings_checks.py` | 3 | 2 | none after PR #105 | ✓ |
| `technical_indicators_checks.py` | 7 | 0 | n/a | ✓ |
| `fundamentals_checks.py` | 3 | 0 | n/a | ✓ |
| `fundamental_summary_checks.py` | 3 | 0 | n/a | ✓ |
| `news_raw_checks.py` | 5 | 0 | n/a | ✓ |
| `ohlcv_raw_checks.py` | 4 | 0 | n/a | ✓ |
| **Total** | **25** | **2** | **0 open** | **✓** |

**No follow-up tickets required.** PR #105 was the only off-by-dedup instance
in the current asset-check surface, and the orphan check sidesteps the class by
construction. The negative finding is itself the load-bearing result: 23 of 25
checks aren't exposed to this bug class because they don't compare cardinality
across stores in the first place.

## Pattern codified — required for any future cross-store check

The QNT-93 → QNT-120 → PR #105 arc generalises to a one-line rule for any new
asset check that compares cardinality between an upstream source table and a
downstream store / table:

> If the downstream's effective key is a strict subset of the upstream RMT's
> `ORDER BY` tuple, the upstream-side aggregation **must** be `uniqExact(<the
> downstream key>) GROUP BY <partition dim>`, never `count()`. The dimensions
> RMT keeps but the downstream collapses (e.g. `published_at` in `news_raw` →
> dropped by the `(ticker, id)` Qdrant key) are exactly the dimensions that
> create false drift if `count()` is used.

This sits naturally with `feedback_pre_design_cross_store_identity.md`: when
adding a new asset that bridges two stores, write the upstream PK tuple →
downstream PK tuple mapping in the asset's docstring (as `point_id` does in
`news_embeddings.py`), then write the cross-store check using the downstream
key directly. The two artefacts together — asset-side mapping + check-side
aggregation — make the off-by-dedup class visible at code-review time instead
of waiting for a domain-bounded WARN to fire in prod.

## Forward exposure

Phase 5 (LangGraph state ↔ ClickHouse via tool calls) and Phase 6 (Next.js
frontend ↔ FastAPI ↔ ClickHouse) introduce two more cross-store bridges. Both
are already covered by the forward-prevention scope changes from the Phase 4
retro (QNT-57 tool-contract block; QNT-67 per-ticker golden-set invariant;
QNT-121 ADR-011 rendering modes), but the rule above should be cited
explicitly when either phase adds its first cross-store asset check.
