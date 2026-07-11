# Codebase Patterns

Established recipes for common tasks. Follow these patterns when implementing - don't reinvent structure.

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

## Adding a Source-Boundary Data Contract (shared)

**Location**: `packages/shared/src/shared/contracts.py` (QNT-259)

**Pattern**: A Pandera `DataFrameSchema` per ingestion source is the executable spec of the *shape* the source hands us. The ingestion asset calls `validate_contract(df, <SOURCE>_CONTRACT)` before any DB write. Two-tier policy:
- **Schema violation** (renamed/missing column, dtype change, empty frame) → `validate_contract` raises `SchemaContractViolation`. Left uncaught in the asset, it hard-fails the partition and fires the QNT-62 Discord run-failure sensor.
- **Value violation** (out-of-range/bad-enum cell) → returned in `ContractResult.value_rejects`; the asset maps them to `Reject(reason="contract_value_violation", ...)` and routes to the `ingest_rejects` sink. Clean rows proceed.

```python
result = validate_contract(df, OHLCV_CONTRACT)   # raises on schema drift
df = result.valid_df
if result.value_rejects:
    record_rejects(context, clickhouse, source_asset="ohlcv_raw", rejects=[
        Reject(ticker=ticker, reason="contract_value_violation",
               payload={"column": r.column, "value": r.failure_case, "check": r.check})
        for r in result.value_rejects
    ])
```

**Conventions**:
- `strict=False` (tolerate extra/reordered columns - they don't break us) and `coerce=False` (so dtype drift is *detected*, not silently coerced).
- Pin dtypes only where drift is meaningful and the payload is stable; make benign-NaN columns `nullable=True` so clean inputs aren't newly quarantined.
- A new source (e.g. EDGAR 8-K) reuses this: add a `*_CONTRACT`, wire one `validate_contract` call.

**Evolving a contract**: when a source *legitimately* changes shape, bump the `*_CONTRACT` in the same commit (diff-visible, same discipline as a ClickHouse migration) and update the fixtures in `tests/dagster/test_source_contracts.py`. Never widen a contract just to silence an alert. Full steps in the `contracts.py` module docstring.

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
- Config fields default to empty/zero - fall back to `shared.settings` at runtime
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

# Partition by ticker - all per-ticker assets use this
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

Every partitioned asset fans out to N subprocess workers per trigger. The `QueuedRunCoordinator` cap in `dagster.yaml` (set by QNT-113) limits how many can run concurrently - raising the cap without raising the daemon's `mem_limit` re-opens the Apr 20 2026 OOM-cascade failure mode.

Before shipping a new scheduled/sensor asset, compute:

```
safe_concurrent_runs = (mem_limit_on_dagster_daemon − 660 MB) / 360 MB
```

Where `660 MB ≈ 260 MB daemon baseline + 400 MB sensor-tick headroom` and `360 MB` is the observed per-run-worker peak RSS during `__ASSET_JOB` materialization (revised from 150 MB after Apr 21 2026 OOM; see QNT-115).

At the current `mem_limit: 3g` (QNT-115) this gives `(3072 − 660) / 360 ≈ 6` theoretical concurrent ceiling. Practical cap stays at `max_concurrent_runs: 3` (QNT-113) - the ceiling is headroom, not a target.

- If `max_concurrent_runs` in `dagster.yaml` is already less than `safe_concurrent_runs`, no change needed - your new asset will queue behind the existing jobs.
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
    """Report endpoint - returns text consumed by the agent."""
    # Validate ticker against TICKERS list
    # Query ClickHouse using settings.clickhouse_url
    # Return pre-computed data - NO arithmetic in endpoint code
```

**Conventions**:
- All endpoints under `/api/v1/`
- Report endpoints return text strings (consumed by agent)
- Data endpoints return JSON arrays (consumed by frontend)
- Validate `{ticker}` against `shared.tickers.TICKERS` - return 404 for unknown
- Always use `SELECT ... FROM table FINAL` for ClickHouse queries (ReplacingMergeTree consistency)
- No authentication (read-only public market data)
- Use `shared.settings` for all config - never hardcode connection details

**When routers are added**: Create `packages/api/src/api/routers/<domain>.py`, use `APIRouter(prefix="/api/v1/<domain>")`, and include in `main.py` via `app.include_router(router)`.

---

## Report Display Precision (QNT-361)

**The report layer owns rounding; the narrator quotes verbatim.** Per the core no-arithmetic rule, the LLM never re-rounds a number - so a report value printed at the "wrong" precision invites spoken rounding that the grounding check correctly flags as drift (trace d9bbf008: "+16.60%" spoken as "16.6%" got redacted).

**Conventions** (all in `packages/api/src/api/formatters.py` + templates, per ADR-012):
- **Percentages render at one decimal** - `format_signed_pct` / `format_pct` default `precision=1`; inline f-string percentage sites use `:.1f`/`:+.1f`. Finance convention quotes growth rates at 1dp.
- **Peer-delta percentages render at integer precision** (`(72% premium)`, not `(72.4% premium)`) - every observed narrator rounding of a peer delta spoke exactly `round(x)`, and a tenth of a percent on a peer premium is spurious precision (medians move daily). Growth/margin percentages stay 1dp.
- **Prices, ratios, and EPS stay at two decimals** (`format_ratio`, `format_currency` defaults).
- **Absolute dollar magnitudes render scale-suffixed at one decimal** (`format_currency_compact`: `$129.2B`, `$3.0T`) - a raw `$129,174,000,000` invites the narrator to speak `$129.2B`, which is rounding. Share prices are not magnitudes; they stay exact at 2dp.
- **Never format a percentage inline at 2dp in a template** - if you need a percent string, use the formatter helpers or `:.1f`.
- The grounding check (`agent/evals/hallucination.py`) treats trailing fractional zeros as formatting (`16.60` == `16.6`) but genuine rounding as drift (`19.36` != `19.4`) - exact value equality, not tolerance. If a narrator rounding shows up as a grounding miss, fix the report precision, don't loosen the checker.

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
- Sequential numbering: `000`, `001`, `002`, ... - check last file and increment
- `equity_raw.*` for ingested data, `equity_derived.*` for computed data
- Always `ReplacingMergeTree` with a version column (`fetched_at` or `computed_at`)
- Always `PARTITION BY ticker` and `ORDER BY (ticker, <temporal_key>)`
- `LowCardinality(String)` for ticker column
- Run via `make migrate` locally; CD applies all `migrations/*.sql` automatically on every deploy (QNT-146)

---

## Retry policy (deploy-window protection)

**Location**: `packages/dagster-pipelines/src/dagster_pipelines/retry.py` (QNT-110)

Two layers protect Dagster runs from transient failures. Apply both to auto-triggered jobs; apply neither to manual / UI-launched jobs (real errors should fail loud - operator is watching).

**Layer 1 - Op-level (`DEPLOY_WINDOW_RETRY`)**: handles flaky ops *inside* a running run. yfinance timeout, transient ClickHouse error, etc. Retries the failing step in place without re-launching the whole run.

```python
from dagster_pipelines.retry import DEPLOY_WINDOW_RETRY

some_job = define_asset_job(
    name="some_job",
    selection=AssetSelection.assets(...),
    op_retry_policy=DEPLOY_WINDOW_RETRY,   # 3 retries, 30s exp backoff, jitter
)
```

**Layer 2 - Run-level (`DEPLOY_WINDOW_RUN_RETRY_TAGS`)**: handles whole-run failures including dequeue/launch errors. The Apr 19 incident was in this bucket - daemon got gRPC UNAVAILABLE while dequeuing a run because the code-server container was mid-restart from a deploy, so no op ever ran. Op-level retry doesn't help; the instance re-launches the whole run instead.

```python
from dagster_pipelines.retry import DEPLOY_WINDOW_RUN_RETRY_TAGS

some_job = define_asset_job(
    name="some_job",
    selection=AssetSelection.assets(...),
    tags=DEPLOY_WINDOW_RUN_RETRY_TAGS,     # dagster/max_retries: "3"
)
```

Run-level retry is **activated globally** by `run_retries.enabled: true` in `dagster.yaml`, with `retry_on_asset_or_op_failure: false` so *only* launch-level failures retry. User errors inside ops still fail loud - a real bug in an asset should not silently retry (that's what layer 1 is for, scoped to transient ops).

**Which layer for which job**:

| Job type | Op retry (in-run) | Run retry (re-launch) |
|---|---|---|
| Sensor-triggered (`*_downstream_job`) | ✓ | ✓ |
| Schedule-triggered (`*_daily_job`, `*_weekly_job`) | Skip - fresh materialization, re-launch is cleaner | ✓ |
| Manual / UI-launched | Skip | Skip |

**News jobs (QNT-53 / QNT-54)**: apply both layers from day one - same lesson as `feedback_sensor_batch_from_day_one`. Don't retrofit later; inherit the protection at build-time.

---

## Tracing the Agent (Langfuse)

**Location**: the request boundary (`api.routers.agent_chat`, `agent.__main__`) and any graph node in `packages/agent/src/agent/`.

ADR-019 replaced the Phase-5 per-node `@observe` + `traced_invoke` wrapper with the Langfuse-LangGraph cookbook pattern: open **one** parent trace at the request boundary, attach a single `CallbackHandler` at graph entry, and let LangGraph propagate the runnable `config` so every node's `llm.invoke(...)` is auto-traced. This halved per-chat events to stay under the 50k/mo free tier - the tool wrappers are **not** traced (they are HTTP calls to FastAPI, not LLM calls).

```python
# Request boundary - parent trace + one handler attached at graph entry.
from agent.tracing import make_callback_handler, observe, propagate_attributes

@observe(name="agent-chat")
def _runner(...):
    with propagate_attributes(trace_name="agent-chat",
                              session_id=thread_id,
                              user_id=sha256(client_ip)[:12]):
        handler = make_callback_handler()
        config = {"callbacks": [handler]} if handler else {}
        graph.invoke(state, config=config)

# Graph node - accept config, forward it to the LLM call.
def synthesize(state: AgentState, config: RunnableConfig) -> dict:
    llm = get_llm()
    response = llm.invoke(build_prompt(state), config=_prompt_cfg(config, "system-prompt"))
    return {"thesis": response.content}
```

**Two non-negotiables**:
1. Every `llm.invoke(...)` passes `config=` so callbacks propagate. `tests/agent/test_tracing.py::test_llm_invoke_calls_pass_config_kwarg` fails CI if a call omits it. (This replaced the old "route through `traced_invoke`" invariant.)
2. The parent trace lives at the request boundary, not per-node. Don't re-add `@observe` to nodes or tools - it double-roots the trace and re-inflates event volume (the exact thing ADR-019 cut).

**Prompt linking (QNT-199)**: `_prompt_cfg(config, "<prompt-name>")` in `graph.py` attaches `langfuse_prompt` metadata so each generation links to its registered prompt in the Langfuse UI. `scripts/push_prompts.py` registers the five named prompts on every deploy; when keys are unset it falls back to the QNT-187 content-hash (`prompt_version`) metadata.

**Config**: `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` in `.env`. Unset keys → `make_callback_handler()` returns `None`, callers branch on the empty-config form, nothing blows up in tests or offline dev. Region matters: the US and EU projects live on different hosts (`us.cloud.langfuse.com` vs `cloud.langfuse.com`) - a trace sent to the wrong region is silently dropped with no auth error.

**Short-lived processes** (CLI, scripts): call `tracing.flush()` before exit - otherwise the last span never reaches the server. `agent.__main__` already does this in a `finally` block; copy the pattern for any new entry point.

---

## Adding a Ticker

**Location**: `packages/shared/src/shared/tickers.py` - but it is not a one-liner.

Adding or removing a ticker touches **four registry structures** in `tickers.py` - `TICKERS`, `TICKER_METADATA`, `NEWS_RELEVANCE`, `TICKER_NAME_ALIASES` - plus several backfill surfaces and an eval-golden sweep. Dagster partitions, API endpoints, and agent tools derive from `TICKERS` automatically, but the metadata / relevance / alias structures and the goldens do not - a missing entry fails *silently* (no news, unparseable in chat, or a broken eval row).

**Follow `docs/guides/ticker-lifecycle.md`** for the full checklist. Never hardcode ticker lists in business logic - derive from the registry or the `/api/v1/tickers` endpoint.

---

## Dependency Patterns

**Between packages** (via `pyproject.toml`):
```
shared              ← no deps (the glue)
dagster-pipelines   ← shared
agent               ← shared
api                 ← shared + agent
```

**Within a package** - import from `shared`:
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
