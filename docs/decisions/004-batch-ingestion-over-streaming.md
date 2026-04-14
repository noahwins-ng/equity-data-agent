# ADR-004: Batch Ingestion Over Streaming

**Date**: 2026-04-13
**Status**: Accepted

## Context

The data ingestion layer needs to fetch OHLCV, fundamentals, and news from external sources on a recurring basis. Two broad approaches exist: **batch** (scheduled runs that fetch a window of data) and **streaming** (always-on consumers processing events as they arrive).

The project uses yfinance as the primary market data source and free news APIs for narrative data. The scope is 10 US equities running on a Hetzner CX41 with 16GB RAM.

## Decision

Use **batch ingestion exclusively** for all data pipelines.

- OHLCV: daily Dagster schedule (~5-6 PM ET, after market close), fetch last 5 trading days per ticker
- Fundamentals: weekly Dagster schedule, fetch all available quarters
- News: periodic Dagster schedule (every few hours), fetch recent articles

`ReplacingMergeTree` in ClickHouse handles deduplication — overlapping fetches are safe and idempotent.

## Alternatives Considered

**Streaming (Kafka + consumers)**
- Would require Kafka or Redis Streams, a persistent consumer process, and offset management
- yfinance has no real-time feed — daily bars are only available after market close
- Adds ~2-4GB memory overhead on a 16GB VPS that already allocates 10GB to ClickHouse + Ollama
- Streaming makes sense for sub-minute data; this project uses daily bars for investment thesis generation

**WebSocket feeds from a broker API (e.g., Alpaca, Polygon)**
- Would enable real-time price updates
- Requires a paid subscription for reliable real-time data
- The agent generates investment theses, not intraday trading signals — real-time data provides no meaningful value here

## Consequences

**Easier:**
- Simple Dagster schedules — no always-on consumer infrastructure
- Full pipeline runs in ~15-20 seconds for 10 tickers
- Fits comfortably within Hetzner memory budget
- Dagster handles retries, backfill, and lineage natively for batch workloads

**Harder:**
- Data is stale until the next scheduled run (acceptable for daily analysis)
- If added in the future, real-time features would require a separate streaming layer — but this is additive, not a rewrite
