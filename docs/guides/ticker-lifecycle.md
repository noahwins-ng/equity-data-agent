# Ticker Lifecycle - Adding & Removing a Symbol

The "adding a ticker = adding one string" claim is folklore. In reality a ticker
touches **four registry structures** in one file, **several backfill surfaces**
across the medallion, the **frontend logo set**, and an **eval-golden sweep**.
This guide is the runbook. Three load-time asserts in
`packages/shared/src/shared/tickers.py` keep the four registry structures
honest; everything else is process.

The portfolio is fixed at **10 tickers** (`TICKERS`) plus a benchmark set
(`BENCHMARK_TICKERS` - currently just `SPY`, OHLCV-only). A swap is an ADD
followed by a REMOVE; do them as **separate PRs** (add first, verify backfills
land, remove second).

---

## ADD a ticker

### 1. Registry edits - `packages/shared/src/shared/tickers.py`

All four are in one file. The module-load asserts will refuse to import if you
miss `TICKER_METADATA`, `NEWS_RELEVANCE`, or `TICKER_NAME_ALIASES`, so you cannot
half-add a ticker.

1. **`TICKERS`** - append the symbol string.

2. **`TICKER_METADATA`** - the real labour. A full editorial profile with all
   seven required keys: `name`, `sector`, `industry`, `description`,
   `key_competitors`, `key_risks`, `watch`. Source `description` / `key_risks`
   from the company's 10-K business overview and risk factors; keep each terse
   so the company-knowledge static report stays under a screen and the LLM can
   quote it verbatim. `_validate_metadata_coverage` asserts this at import - a
   missing entry or missing key fails at startup, not at request time.

3. **`NEWS_RELEVANCE`** - `aliases` (symbol + company name + 1-2 distinctive
   product/person tokens) and `scope`. Use `scope="any"` (headline OR body)
   normally; use `scope="headline"` for high-false-positive symbols whose name
   appears as common prose (META's lowercase "meta", V's bare "Visa"). For
   `scope="any"` tickers the bare symbol **must** be in `aliases`
   (`test_news_relevance.py` enforces this). The `set(NEWS_RELEVANCE) ==
   set(TICKERS)` assert enforces coverage.

4. **`TICKER_NAME_ALIASES`** (QNT-257) - the company name + common short name a
   user types in chat (e.g. `"GOOGL": ["Google", "Alphabet"]`). The agent's
   ticker parser (`agent.intent.extract_tickers`) matches these so a name-only
   ask ("thesis on micron") resolves instead of bouncing to the clarify node.
   Keep it **conservative**: company / short name only - NOT exec names or
   product brands (those over-resolve in prose; that's why this is separate from
   `NEWS_RELEVANCE.aliases`). Do **not** repeat the bare symbol (matched
   separately). The `set(TICKER_NAME_ALIASES) == set(TICKERS)` assert enforces
   coverage; `test_ticker_name_aliases.py` pins the invariants.

Run the asserts + config tests before touching data:

```bash
uv run pytest tests/shared/ -q
```

### 2. Backfills - in dependency order

Dagster uses `StaticPartitionsDefinition(TICKERS)`, so the new partition key
appears automatically on code reload (dev: restart `make dev-dagster`; prod:
deploy). Then materialize, **upstream first**. The CLI form (works in dev from
the repo root; prod runs the same binary inside the daemon container - see
ops-runbook):

```bash
# a. OHLCV - fresh materialize defaults to period="2y" (the backfill default;
#    the daily schedule overrides to "5d"). Downstream aggregations + indicators
#    fire automatically via ohlcv_raw_sensor.
uv run dagster asset materialize --select ohlcv_raw --partition <T> \
    -m dagster_pipelines.definitions

# b. Fundamentals - fundamentals_sensor auto-fires fundamental_summary downstream.
#    (fundamental_summary also rides ohlcv_downstream_job, so it may already have
#    materialized once in step a - a second run here is expected, not a fault.)
uv run dagster asset materialize --select fundamentals --partition <T> \
    -m dagster_pipelines.definitions

# c. News (12 months) - direct Finnhub → ClickHouse backfill. NOTE: this script
#    writes news_raw with client.insert_df; it does NOT emit a Dagster
#    materialization event, so news_raw_sensor will NOT auto-fire embeddings.
#    --tickers is comma-separated; pass the bare symbol for a single add.
uv run --package dagster-pipelines python scripts/backfill_finnhub_news.py \
    --tickers <T> --months 12
```

You can also materialize a/b from the Dagster UI (`http://localhost:3000`) by
selecting the asset and the new partition - equivalent to the CLI.

**Embeddings are rolling-7d by design (ADR-009).** `news_embeddings` only embeds
articles `published_at >= now() - 7 days`; the 12-month backfill stays in
ClickHouse (it powers the news card on the ticker-detail page). So you do **not**
backfill a year into Qdrant - the new ticker's semantic-search index warms
organically over the first ~7 days as the daily `news_raw` schedule materializes
its partition and `news_raw_sensor` fires `news_embeddings`. Expect semantic
news search for the new ticker to be shallow for about a week. (No manual
embeddings step is required, and there is no path to retro-embed the year
without changing the asset's window - that's intentional.)

### 3. Frontend

The ticker list is **API-served** (dynamic) - the new symbol appears in the UI
selector once the API restarts with the new `TICKERS`. The one static surface is
the **logo set**; add an entry if the frontend keeps per-ticker logos and the
new symbol isn't covered, otherwise it falls back to the default glyph.

### 4. Eval goldens (optional)

Adding golden coverage for the new ticker is optional but recommended once its
data has landed - see the goldens under
`packages/agent/src/agent/evals/goldens/`. Not required to ship the add.

### 5. Asset-check bounds

Domain-bounded asset checks (e.g. P/E) were calibrated on the existing set. A
structurally different add (a deeply cyclical name with null/negative P/E in a
downcycle, a recently re-listed name with thin history) may trip them. Re-derive
the bound against the new ticker's real data - don't explain the WARN away
(QNT-142 calibration-inheritance pattern).

---

## REMOVE a ticker

### 1. Registry edits

Delete the symbol from **all four** structures in `tickers.py` (`TICKERS`,
`TICKER_METADATA`, `NEWS_RELEVANCE`, `TICKER_NAME_ALIASES`). The three load-time
asserts keep them in sync - drop it from `TICKERS` only and import fails.

### 2. Dagster partitions

The static partition disappears on code reload. Stale materialization records
for the dropped partition are harmless - leave them.

### 3. Data - keep-vs-drop decision

**Do NOT drop data initially.** All consumer endpoints gate on `TICKERS`, so a
removed ticker goes invisible the moment the API restarts. Dormant data is
zero-risk and fully reversible (re-add the string to restore it instantly - no
re-backfill). This is the default.

**Only when you actually want the space back:** the data is partitioned by
ticker, so removal is instant -

```sql
-- per ClickHouse table that has PARTITION BY ticker (ohlcv_raw, fundamentals,
-- news_raw, the aggregation/indicator tables, ...)
ALTER TABLE equity_raw.ohlcv_raw DROP PARTITION '<T>';
-- ... repeat per table.
```

For Qdrant, do a **filtered point-delete** scoped to the ticker payload - the
sibling pattern of `scripts/drop_qdrant_news_collection.py` (which drops the
whole collection; for a single ticker, delete by the `ticker` payload filter
instead, the same filter `ticker_filter()` builds in `news_embeddings.py`).

### 4. Eval-golden SWEEP - the step nobody remembers

`packages/agent/src/agent/evals/goldens/questions.yaml` **and**
`dialogue.yaml` name concrete tickers. A golden that references a removed symbol
either breaks outright or, worse, silently exercises a now-rejected ticker and
passes for the wrong reason. **Grep the goldens for the symbol and rewrite or
delete every hit before shipping the removal:**

```bash
grep -rn '<T>' packages/agent/src/agent/evals/goldens/
```

(Use word-boundary care for short symbols - `V` will match a lot; review hits
by hand.)

---

## Verify (both directions)

```bash
uv run pytest tests/shared/ -q        # registry asserts + config integrity
uv run pytest tests/agent/evals/ -q   # goldens parse (after a remove sweep)
```

---

## Future convenience (not built)

A `make ticker-backfill TICKER=<T>` target chaining the materialize sequence in
2 would be nice, but is only worth building if ticker churn exceeds ~1-2× per
year. Until then, the commands above are the process.
