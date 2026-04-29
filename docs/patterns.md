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

**Concurrency pre-flight** (required when adding a scheduled / sensor-triggered partitioned job):

Every partitioned asset fans out to N subprocess workers per trigger. The `QueuedRunCoordinator` cap in `dagster.yaml` (set by QNT-113) limits how many can run concurrently — raising the cap without raising the daemon's `mem_limit` re-opens the Apr 20 2026 OOM-cascade failure mode.

Before shipping a new scheduled/sensor asset, compute:

```
safe_concurrent_runs = (mem_limit_on_dagster_daemon − 660 MB) / 360 MB
```

Where `660 MB ≈ 260 MB daemon baseline + 400 MB sensor-tick headroom` and `360 MB` is the observed per-run-worker peak RSS during `__ASSET_JOB` materialization (revised from 150 MB after Apr 21 2026 OOM; see QNT-115).

At the current `mem_limit: 3g` (QNT-115) this gives `(3072 − 660) / 360 ≈ 6` theoretical concurrent ceiling. Practical cap stays at `max_concurrent_runs: 3` (QNT-113) — the ceiling is headroom, not a target.

- If `max_concurrent_runs` in `dagster.yaml` is already less than `safe_concurrent_runs`, no change needed — your new asset will queue behind the existing jobs.
- If the total fan-out across ALL scheduled/sensor jobs × their partition counts, triggered within a single cron firing window, would exceed `max_concurrent_runs`, either (a) raise `mem_limit` on `dagster-daemon` in `docker-compose.yml` AND scale `max_concurrent_runs` proportionally in the same PR, or (b) stagger the schedules so fan-out doesn't overlap.
- Serialized throughput is the safe default. A backfill taking ~10× longer is correct; an OOM cascade that kills the daemon is not.

Reference: `docs/guides/ops-runbook.md` §"Dagster backfill OOM-kill" for the incident history + diagnosis commands.

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
- Run via `make migrate` locally; CD applies all `migrations/*.sql` automatically on every deploy (QNT-146)

---

## Retry policy (deploy-window protection)

**Location**: `packages/dagster-pipelines/src/dagster_pipelines/retry.py` (QNT-110)

Two layers protect Dagster runs from transient failures. Apply both to auto-triggered jobs; apply neither to manual / UI-launched jobs (real errors should fail loud — operator is watching).

**Layer 1 — Op-level (`DEPLOY_WINDOW_RETRY`)**: handles flaky ops *inside* a running run. yfinance timeout, transient ClickHouse error, etc. Retries the failing step in place without re-launching the whole run.

```python
from dagster_pipelines.retry import DEPLOY_WINDOW_RETRY

some_job = define_asset_job(
    name="some_job",
    selection=AssetSelection.assets(...),
    op_retry_policy=DEPLOY_WINDOW_RETRY,   # 3 retries, 30s exp backoff, jitter
)
```

**Layer 2 — Run-level (`DEPLOY_WINDOW_RUN_RETRY_TAGS`)**: handles whole-run failures including dequeue/launch errors. The Apr 19 incident was in this bucket — daemon got gRPC UNAVAILABLE while dequeuing a run because the code-server container was mid-restart from a deploy, so no op ever ran. Op-level retry doesn't help; the instance re-launches the whole run instead.

```python
from dagster_pipelines.retry import DEPLOY_WINDOW_RUN_RETRY_TAGS

some_job = define_asset_job(
    name="some_job",
    selection=AssetSelection.assets(...),
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,     # dagster/max_retries: "3"
)
```

Run-level retry is **activated globally** by `run_retries.enabled: true` in `dagster.yaml`, with `retry_on_asset_or_op_failure: false` so *only* launch-level failures retry. User errors inside ops still fail loud — a real bug in an asset should not silently retry (that's what layer 1 is for, scoped to transient ops).

**Which layer for which job**:

| Job type | Op retry (in-run) | Run retry (re-launch) |
|---|---|---|
| Sensor-triggered (`*_downstream_job`) | ✓ | ✓ |
| Schedule-triggered (`*_daily_job`, `*_weekly_job`) | Skip — fresh materialization, re-launch is cleaner | ✓ |
| Manual / UI-launched | Skip | Skip |

**News jobs (QNT-53 / QNT-54)**: apply both layers from day one — same lesson as `feedback_sensor_batch_from_day_one`. Don't retrofit later; inherit the protection at build-time.

---

## Tracing a LangGraph Node or Tool (Langfuse)

**Location**: anything in `packages/agent/src/agent/` that calls the LLM or is a graph node / tool.

ADR-007 pins the graph at `plan → gather → synthesize`; QNT-61 wires Langfuse so every one of those nodes, plus every tool, shows up in a single trace with prompt / output / tokens / latency captured.

```python
from agent.llm import get_llm
from agent.tracing import langfuse, observe

@observe()  # graph node — one span per node, auto-nested under the run's trace
def synthesize(state: State) -> State:
    llm = get_llm()
    response = langfuse.traced_invoke(
        llm,
        build_prompt(state),
        name="synthesize",  # shows up as the generation span name in the UI
    )
    return {"thesis": response.content}

@observe()  # tool — same pattern, different `as_type` default
def get_technical_report(ticker: str) -> str:
    return httpx.get(f"{api_url}/api/v1/reports/technical/{ticker}").text
```

**Two non-negotiables**:
1. Every LLM call goes through `langfuse.traced_invoke(...)`. `tests/test_tracing.py::test_no_raw_llm_invoke_in_agent_package` fails CI if a raw `llm.invoke(...)` sneaks into the agent package.
2. Every graph node and every tool carries `@observe()`. Without it, the Langfuse trace tree collapses and you lose the `plan → gather → synthesize` boundaries in the UI.

**Config**: `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` in `.env`. Unset keys → tracing auto-disables, `traced_invoke` becomes a pass-through, nothing blows up in tests or offline dev. Region matters: the US project and EU project live on different hosts (`us.cloud.langfuse.com` vs `cloud.langfuse.com`) — a trace sent to the wrong region is silently dropped with no auth error.

**Short-lived processes** (CLI, scripts): call `langfuse.flush()` before exit — otherwise the last span never reaches the server. `agent.__main__.main()` already does this in a `finally` block; copy the pattern for any new entry point.

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
