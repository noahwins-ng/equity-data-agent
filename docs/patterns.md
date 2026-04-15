# Codebase Patterns

Established recipes for common tasks. Follow these patterns when implementing — don't reinvent structure.

---

## Adding a Pydantic Schema (shared)

**Location**: `packages/shared/src/shared/schemas/<domain>.py`

**Pattern**: One file per domain (ohlcv, indicators, fundamentals, news). Each class maps to a ClickHouse table.

```python
# packages/shared/src/shared/schemas/ohlcv.py
from datetime import date, datetime
from pydantic import BaseModel

class OHLCVRow(BaseModel):
    """Maps to equity_raw.ohlcv_raw."""
    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int
    fetched_at: datetime | None = None
```

**Conventions**:
- Class name = `<Entity>Row` (matches the DB table it maps to)
- Docstring says which table it maps to: `"""Maps to <database>.<table>."""`
- Use `float` for numeric columns, `int` for integers, `date`/`datetime` for temporal
- Nullable fields default to `None`

**Wiring**:
1. Create the file in `packages/shared/src/shared/schemas/`
2. Export the class from `packages/shared/src/shared/schemas/__init__.py`:
   ```python
   from shared.schemas.your_domain import YourRow
   # Add to __all__
   ```

---

## Adding a Dagster Resource

**Location**: `packages/dagster-pipelines/src/dagster_pipelines/resources/<name>.py`

**Pattern** (see `clickhouse.py`):

```python
from dagster import ConfigurableResource
from pydantic import Field
from shared.config import settings

class MyResource(ConfigurableResource):
    """Brief description. Defaults to shared.Settings."""
    host: str = Field(default="")
    port: int = Field(default=0)

    def _client(self):
        return create_client(
            host=self.host or settings.MY_HOST,
            port=self.port or settings.MY_PORT,
        )

    def some_method(self, ...) -> ...:
        """Always include retry logic for external services."""
        # Use _MAX_RETRIES pattern from clickhouse.py
```

**Conventions**:
- Config fields default to empty/zero — fall back to `shared.settings` at runtime
- This lets tests override via Dagster config without touching env vars
- Include retry logic for any external I/O (see `_MAX_RETRIES` pattern in `clickhouse.py`)
- Use `logging.getLogger(__name__)` for structured logging

**Wiring**:
1. Create the file in `resources/`
2. Export from `resources/__init__.py`: `from dagster_pipelines.resources.my_resource import MyResource`
3. Add to `definitions.py` resources dict:
   ```python
   resources={"my_resource": MyResource()}
   ```
4. Export from package `__init__.py`

---

## Adding a Dagster Asset

**Location**: `packages/dagster-pipelines/src/dagster_pipelines/assets/<name>.py`

**Pattern** (see `ohlcv_raw.py`):

```python
import logging
from dagster import (
    AssetExecutionContext,
    Backoff,
    Config,
    RetryPolicy,
    StaticPartitionsDefinition,
    asset,
)
from shared.tickers import TICKERS
from dagster_pipelines.resources.clickhouse import ClickHouseResource

logger = logging.getLogger(__name__)

# Partition by ticker — all per-ticker assets use this
my_partitions = StaticPartitionsDefinition(TICKERS)

class MyConfig(Config):
    period: str = "2y"  # sensible default, overridable per-run

@asset(
    partitions_def=my_partitions,
    retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL),
    group_name="ingestion",  # or "derived", "news", etc.
)
def my_asset(
    context: AssetExecutionContext,
    config: MyConfig,
    clickhouse: ClickHouseResource,
) -> None:
    """Docstring explaining what this asset does."""
    ticker = context.partition_key
    # Fetch/compute → transform to DataFrame → insert
    clickhouse.insert_df("database.table", df)
    context.log.info("Inserted %d rows for %s", len(df), ticker)
```

**Conventions**:
- Always partitioned by ticker via `StaticPartitionsDefinition(TICKERS)`
- Always has `RetryPolicy` with exponential backoff for external calls
- Function name = snake_case matching the ClickHouse table name
- Resources are injected by name (e.g., `clickhouse: ClickHouseResource`)
- Use `context.log` for Dagster-managed logging, `logger` for module-level
- Rate limiting: `time.sleep(1.5)` at end if calling external APIs

**Wiring**:
1. Create the file in `assets/`
2. Export from `assets/__init__.py`: `from dagster_pipelines.assets.my_asset import my_asset`
3. Add to `definitions.py` assets list:
   ```python
   assets=[ohlcv_raw, my_asset]
   ```
4. Export from package `__init__.py`

---

## Adding a FastAPI Endpoint

**Location**: `packages/api/src/api/main.py` (simple) or `packages/api/src/api/routers/<name>.py` (when routers are split)

**Pattern** (see `main.py`):

```python
from fastapi import FastAPI, Response
from shared.config import settings

app = FastAPI(title="Equity Data Agent API")

@app.get("/api/v1/reports/technical/{ticker}")
def get_technical_report(ticker: str) -> dict:
    """Report endpoint — returns text consumed by the agent."""
    # Validate ticker against TICKERS list
    # Query ClickHouse using settings.clickhouse_url
    # Return pre-computed data — NO arithmetic in endpoint code
```

**Conventions**:
- All endpoints under `/api/v1/`
- Report endpoints return text strings (consumed by agent)
- Data endpoints return JSON arrays (consumed by frontend)
- Validate `{ticker}` against `shared.tickers.TICKERS` — return 404 for unknown
- Always use `SELECT ... FROM table FINAL` for ClickHouse queries (ReplacingMergeTree consistency)
- No authentication (read-only public market data)
- Use `shared.settings` for all config — never hardcode connection details

**When routers are added**: Create `packages/api/src/api/routers/<domain>.py`, use `APIRouter(prefix="/api/v1/<domain>")`, and include in `main.py` via `app.include_router(router)`.

---

## Adding a ClickHouse Migration

**Location**: `migrations/<NNN>_<description>.sql`

**Pattern** (see `003_create_ohlcv_raw.sql`):

```sql
CREATE TABLE IF NOT EXISTS <database>.<table> (
    ticker       LowCardinality(String),
    date         Date,
    column_name  Float64,
    ...
    fetched_at   DateTime DEFAULT now()   -- or computed_at
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY ticker
ORDER BY (ticker, date);
```

**Conventions**:
- Sequential numbering: `000`, `001`, `002`, ... — check last file and increment
- `equity_raw.*` for ingested data, `equity_derived.*` for computed data
- Always `ReplacingMergeTree` with a version column (`fetched_at` or `computed_at`)
- Always `PARTITION BY ticker` and `ORDER BY (ticker, <temporal_key>)`
- `LowCardinality(String)` for ticker column
- Run via `make migrate`

---

## Adding a Ticker

**Location**: `packages/shared/src/shared/tickers.py`

1. Add the ticker string to `TICKERS` list
2. Add metadata to `TICKER_METADATA` dict
3. That's it — all Dagster partitions, API endpoints, and agent tools derive from this list automatically

---

## Dependency Patterns

**Between packages** (via `pyproject.toml`):
```
shared              ← no deps (the glue)
dagster-pipelines   ← shared
agent               ← shared
api                 ← shared + agent
```

**Within a package** — import from `shared`:
```python
from shared.config import settings        # Config singleton
from shared.config import Settings        # Type (for testing)
from shared.tickers import TICKERS        # Ticker list
from shared.schemas.ohlcv import OHLCVRow # Pydantic schema
```

**Adding a new package dep**: Add to `dependencies` in the package's `pyproject.toml`, then `uv sync --all-packages`.

---

## Config Access

Always use `shared.config.settings` (the singleton). Never create a new `Settings()` instance in business logic.

```python
from shared.config import settings

# Use properties
url = settings.clickhouse_url       # http://localhost:8123
is_prod = settings.is_prod          # False in dev

# Use fields
host = settings.CLICKHOUSE_HOST     # localhost (dev) or clickhouse (prod)
```

For new env vars: add them to `Settings` class in `config.py`, then to `.env.example`.

---

## Export Pattern (__init__.py)

Every package and sub-package uses explicit `__all__` exports:

```python
# packages/shared/src/shared/__init__.py
from shared.config import Settings, settings
from shared.tickers import TICKER_METADATA, TICKERS

__all__ = ["Settings", "settings", "TICKERS", "TICKER_METADATA"]
```

When adding a new public symbol: import it and add to `__all__`. Don't rely on implicit imports.
