# ADR-001: ClickHouse over PostgreSQL for structured storage

**Date**: 2026-04-12
**Status**: Accepted

## Context
Need a database for time-series financial data (OHLCV, fundamentals, indicators). The workload is append-heavy with analytical queries — aggregations over date ranges, grouped by ticker.

## Decision
Use ClickHouse with `ReplacingMergeTree` engine.

## Alternatives Considered
- **PostgreSQL + TimescaleDB**: Mature, general-purpose. But requires more RAM for comparable analytical performance. Heavier on a 16GB VPS.
- **DuckDB**: Excellent for analytics, but embedded (no server mode for multi-service access). Can't share between Dagster and FastAPI without file locking.
- **QuestDB**: Good for time-series but smaller ecosystem and less community support.

## Consequences
- **Positive**: Blazing fast aggregations, columnar storage is memory-efficient, `ReplacingMergeTree` gives us idempotency for free, `LowCardinality(String)` is perfect for ticker columns.
- **Negative**: No transactions, eventual consistency on dedup (need `FINAL` keyword or `OPTIMIZE TABLE`), less mature ORM support in Python.
- **Mitigated by**: Using Dagster for write orchestration (no concurrent writes), querying with `FINAL` in FastAPI for consistent reads.
