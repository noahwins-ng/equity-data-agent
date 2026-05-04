"""Integration-test scaffolding for QNT-64 critical-path tests.

Three responsibilities:

1. **Skip gate** — auto-skip integration tests when ClickHouse is unreachable
   (no SSH tunnel locally; CI service container down). Inherited from the
   QNT-43 single-test conftest this file replaced.

2. **Schema bootstrap** — apply ``migrations/*.sql`` once per session against
   the live ClickHouse before any integration test runs. Migrations are
   idempotent (``CREATE TABLE IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS``)
   so re-running on an already-migrated DB is a no-op.

3. **Prod-data guard** — refuse to run if the target ClickHouse has > 0 rows
   in ``equity_raw.ohlcv_raw`` (i.e. looks like tunneled prod). Integration
   tests truncate these tables between runs; an accidental local invocation
   against ``make tunnel`` would otherwise destroy real data. CI bypasses
   the guard via the ``CI=true`` env var GitHub Actions sets automatically.

Tables are truncated function-scoped (autouse) so each test starts from a
clean slate regardless of order. The truncate set covers both ``equity_raw``
and ``equity_derived`` — every table the production code paths touch.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import clickhouse_connect
import pytest
from clickhouse_connect.driver.client import Client
from shared.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

# Every table integration tests interact with. Truncated before each test
# so the seed → query → assert flow doesn't see leftover rows from a prior
# test in the same session. Keep alphabetised within each database for
# readability — order is unimportant since TRUNCATE doesn't have FK
# constraints to honour.
_TRUNCATE_TABLES: tuple[str, ...] = (
    "equity_raw.fundamentals",
    "equity_raw.news_raw",
    "equity_raw.ohlcv_raw",
    "equity_derived.fundamental_summary",
    "equity_derived.ohlcv_monthly",
    "equity_derived.ohlcv_weekly",
    "equity_derived.technical_indicators_daily",
    "equity_derived.technical_indicators_monthly",
    "equity_derived.technical_indicators_weekly",
)


def _ch_client(timeout: int = 5) -> Client:
    """Open a ClickHouse client at the configured host/port."""
    return clickhouse_connect.get_client(
        host=settings.CLICKHOUSE_HOST,
        port=settings.CLICKHOUSE_PORT,
        connect_timeout=timeout,
        compress=False,
    )


def _ch_reachable() -> bool:
    try:
        _ch_client(timeout=2).query("SELECT 1")
        return True
    except Exception:
        return False


def _looks_like_prod(client: Client) -> tuple[str, int] | None:
    """Return the first ``(table, count)`` pair where the row count > 0.

    Used by the prod-data guard. Any non-zero value triggers a hard abort
    when not running in CI — integration tests truncate these tables and
    we refuse to wipe what looks like real data. Checks every prod-data
    table, not just ``ohlcv_raw``: a freshly-migrated prod that has news
    or fundamentals ingested but no OHLCV yet would otherwise slip past
    a single-table check.
    """
    for table in ("equity_raw.ohlcv_raw", "equity_raw.news_raw", "equity_raw.fundamentals"):
        try:
            result = client.query(f"SELECT count() FROM {table}")
        except Exception:
            # Table doesn't exist yet (fresh CH); migrations will create it.
            continue
        if result.result_rows and int(result.result_rows[0][0]) > 0:
            return (table, int(result.result_rows[0][0]))
    return None


def _apply_migrations(client: Client) -> None:
    """Run every ``migrations/*.sql`` file in lexical order, one statement each.

    Mirrors what ``make migrate`` does over HTTP. Files are intentionally
    one-statement each (the HTTP interface rejects multi-statement bodies —
    see ``feedback_clickhouse_migrations``), so feeding each to
    ``client.command`` is the same shape.
    """
    for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = sql_file.read_text().strip()
        if not sql:
            continue
        client.command(sql)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Auto-skip integration tests when ClickHouse is unreachable."""
    if "integration" not in item.keywords:
        return
    if not _ch_reachable():
        pytest.skip("ClickHouse not reachable — run 'make tunnel' to enable integration tests")


@pytest.fixture(scope="session")
def ch_session_client() -> Iterator[Client]:
    """Session-scoped ClickHouse client with schema applied + safety gate.

    Skipped when CH is unreachable so the rest of the unit-test session
    isn't penalised; an explicit ``pytest.exit`` is reserved for the prod-
    data guard since silently skipping there would let tests pass while
    quietly avoiding their own preconditions.
    """
    if not _ch_reachable():
        pytest.skip("ClickHouse not reachable")
    client = _ch_client()
    existing = _looks_like_prod(client)
    if existing is not None and os.environ.get("CI") != "true":
        table, count = existing
        pytest.exit(
            f"Refusing to run integration tests: {table} has "
            f"{count} rows. This looks like a real ClickHouse — "
            "the integration suite truncates these tables between runs and "
            "would destroy that data.\n\n"
            "Run against a dedicated empty CH instead, e.g.:\n"
            "  docker run --rm -d -p 8124:8123 clickhouse/clickhouse-server:24\n"
            "  CLICKHOUSE_PORT=8124 uv run pytest -m integration\n",
            returncode=1,
        )
    _apply_migrations(client)
    yield client
    # Session-end truncate so a follow-up local run doesn't trip the
    # prod-data guard on rows the just-finished session inserted. CI
    # workflows spin up a fresh container per job so this is a no-op
    # there; locally it makes ``pytest -m integration`` rerunnable.
    for table in _TRUNCATE_TABLES:
        try:
            client.command(f"TRUNCATE TABLE IF EXISTS {table}")
        except Exception:
            pass
    client.close()


@pytest.fixture(autouse=True)
def _truncate_between_tests(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Truncate equity_* tables before each integration test.

    Function-scoped + autouse so seed-then-assert tests start clean. Only
    fires for tests carrying the ``integration`` mark — unit tests pay
    nothing. Pulls the session client lazily so unit-only runs never even
    open a CH connection.
    """
    if "integration" not in request.keywords:
        yield
        return
    client = request.getfixturevalue("ch_session_client")
    for table in _TRUNCATE_TABLES:
        client.command(f"TRUNCATE TABLE IF EXISTS {table}")
    yield


@pytest.fixture
def ch_client(ch_session_client: Client) -> Client:
    """Per-test alias for ``ch_session_client``.

    Most tests want a function-scoped name even though the underlying
    connection is session-scoped (clickhouse-connect's HttpClient is fine
    to reuse across the session). This fixture exists so the dependency
    chain is explicit when a test asks for ``ch_client``.
    """
    return ch_session_client


@pytest.fixture(autouse=True, scope="session")
def _stub_finnhub_logo_fetch() -> Iterator[None]:
    """Replace ``_fetch_logo_data_url`` with a no-op for the whole session.

    Without this, every integration test that opens a FastAPI TestClient
    spawns a daemon prewarm thread that hits real Finnhub. The daemon
    keeps running across test boundaries and silently rewrites the
    ``_logo_cache`` between the autouse cache-clear and the test body —
    a race that broke
    ``test_logos_endpoint_returns_per_ticker_map_without_finnhub_key``
    intermittently.

    Stubbing at session scope means no integration test ever reaches the
    real Finnhub CDN. The "without API key" contract is still verifiable
    (the test that asserts it monkeypatches FINNHUB_API_KEY="" and
    relies on the logo router's no-key short-circuit, not on the network
    fetch returning None).
    """
    from api.routers import logos as logos_module

    original = logos_module._fetch_logo_data_url
    logos_module._fetch_logo_data_url = lambda *_args, **_kwargs: None
    yield
    logos_module._fetch_logo_data_url = original
