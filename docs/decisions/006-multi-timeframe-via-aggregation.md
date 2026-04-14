# ADR-006: Multi-Timeframe OHLCV via Aggregation, Not Separate Ingestion

**Date**: 2026-04-13
**Status**: Accepted

## Context

The agent and frontend need price data at three timeframes — daily, weekly, and monthly — to support multi-timeframe technical analysis (e.g., daily RSI for short-term momentum, weekly RSI for medium-term trend health, monthly for regime context).

The question was whether to ingest weekly and monthly bars directly from yfinance, or derive them by aggregating the daily bars already in ClickHouse.

## Decision

Ingest **daily bars only** from yfinance into `equity_raw.ohlcv_raw`. Derive weekly and monthly bars via Dagster aggregation assets that write to `equity_derived.ohlcv_weekly` and `equity_derived.ohlcv_monthly`.

Aggregation rules:
- `open` = first trading day's open for the period
- `close` / `adj_close` = last trading day's close for the period
- `high` = `MAX(high)` across the period
- `low` = `MIN(low)` across the period
- `volume` = `SUM(volume)` across the period
- `week_start` = `toMonday(date)`, `month_start` = `toStartOfMonth(date)`

Technical indicators (RSI, MACD, SMA, EMA, Bollinger Bands) are then computed on each timeframe's OHLCV table, writing to `technical_indicators_daily`, `_weekly`, `_monthly`.

## Alternatives Considered

**Fetch weekly and monthly bars directly from yfinance**
- yfinance supports `interval="1wk"` and `interval="1mo"`
- Rejected for three reasons:
  1. **Single source of truth**: daily bars are already being fetched and stored. Fetching weekly/monthly separately means three separate ingestion jobs, three separate data sources that could diverge (e.g., yfinance returning slightly different adjusted close values across intervals due to caching or calculation differences).
  2. **Derivability**: weekly and monthly bars are 100% derivable from daily bars with no information loss. Any data that can be derived deterministically should be derived, not fetched separately.
  3. **Dagster lineage**: aggregation assets make the `ohlcv_raw → ohlcv_weekly → technical_indicators_weekly` lineage explicit and visible in the Dagster UI. Separate ingestion assets would obscure this relationship.

**ClickHouse Materialized Views**
- Could auto-aggregate weekly/monthly at insert time with zero Dagster code
- Rejected: materialized views are invisible to Dagster's lineage graph. The whole point of Software-Defined Assets is that the full pipeline — Raw → Aggregated → Indicators → Reports — is visible and replayable. Hiding aggregations inside the database breaks that.

## Consequences

**Easier:**
- Rebuild any timeframe by re-running one Dagster asset — no external API call needed
- No risk of daily/weekly/monthly data drifting out of sync (single source)
- Full lineage visible in Dagster UI
- Sensor can trigger indicator recomputation for all timeframes when daily raw data refreshes

**Harder:**
- Two extra Dagster assets to write and maintain (`ohlcv_weekly`, `ohlcv_monthly`)
- Sensor triggering logic is slightly more complex (raw → aggregations → indicators, in sequence)
- Partial weeks/months at data boundaries require handling (e.g., the current incomplete week should not be emitted as a weekly bar until the week closes)
