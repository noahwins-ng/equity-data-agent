"""Tests for the reject sink and its three wired drop sites (QNT-243).

Bad source rows used to vanish into logs. These tests pin the new contract:
a deliberately-bad row lands in ``equity_raw.ingest_rejects`` (here: the
recording fake's ``insert_df``) rather than disappearing, and the per-run
reject count is emitted as asset metadata.

Each asset test drives the asset body via ``build_asset_context`` with a
recording ClickHouse fake and monkeypatched upstream fetchers — same style as
``test_external_fetch_retries.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

import numpy as np
import pandas as pd
import pytest
from dagster import build_asset_context
from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.news_raw import news_raw
from dagster_pipelines.assets.ohlcv_raw import OHLCVConfig, ohlcv_raw
from dagster_pipelines.rejects import REJECTS_TABLE, _reject_id

_NEWS_MODULE = import_module("dagster_pipelines.assets.news_raw")
_FUND_MODULE = import_module("dagster_pipelines.assets.fundamentals")
_OHLCV_MODULE = import_module("dagster_pipelines.assets.ohlcv_raw")


@dataclass
class _RecordingClickHouse:
    """Captures every ``insert_df`` so tests can assert what landed in the sink."""

    inserts: list[tuple[str, pd.DataFrame]] = field(default_factory=list)

    def insert_df(self, table: str, df: pd.DataFrame) -> None:
        self.inserts.append((table, df))

    def reject_rows(self) -> pd.DataFrame:
        frames = [df for table, df in self.inserts if table == REJECTS_TABLE]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ── _reject_id (dedup key) ────────────────────────────────────────────────────
#
# add_output_metadata only binds inside a real asset invocation, so record_rejects
# is exercised through the assets below (which DO bind the context). Here we pin
# only the dedup-key contract that ReplacingMergeTree idempotency relies on.


def test_reject_id_deterministic_and_payload_sensitive() -> None:
    """Same dropped record hashes identically across runs (so the
    ReplacingMergeTree collapses re-materialized rejects); a different payload
    or reason yields a different id."""
    payload = {"period_end": "2026-03-31"}
    a = _reject_id("NVDA", "fundamentals", "nan_period", payload)
    assert a == _reject_id("NVDA", "fundamentals", "nan_period", dict(payload))
    assert a != _reject_id("NVDA", "fundamentals", "nan_period", {"period_end": "2026-06-30"})
    assert a != _reject_id("NVDA", "fundamentals", "empty_fetch", payload)
    assert a != _reject_id("AAPL", "fundamentals", "nan_period", payload)


# ── news_raw: relevance gate drop site ────────────────────────────────────────


def _finnhub_article(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "datetime": 1714000000,
        "headline": "NVDA reports record Q1 revenue",
        "source": "Reuters",
        "summary": "Revenue grew 114% YoY.",
        "url": "https://example.com/a",
    }
    base.update(overrides)
    return base


def test_news_raw_records_below_relevance_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """An article Finnhub tags NVDA but that never mentions it lands in the
    sink with reason=below_relevance instead of vanishing."""
    irrelevant = _finnhub_article(
        headline="AMD beats earnings on data-center revenue",
        summary="TSMC outlook strong; Intel guides cautious.",
        url="https://example.com/irrelevant",
    )
    monkeypatch.setattr(_NEWS_MODULE, "fetch_company_news", lambda *a, **k: [irrelevant])

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    news_raw(ctx, _NEWS_MODULE.NewsRawConfig(), clickhouse=ch)  # type: ignore[arg-type]

    df = ch.reject_rows()
    assert len(df) == 1
    assert df["reason"].iloc[0] == "below_relevance"
    assert df["ticker"].iloc[0] == "NVDA"
    assert df["source_asset"].iloc[0] == "news_raw"
    # The article body was never inserted into news_raw.
    assert all(table == REJECTS_TABLE for table, _ in ch.inserts)
    # Count surfaces as asset metadata for the Dagster UI / QNT-240 dashboard.
    meta = ctx.get_output_metadata("result")
    assert meta is not None and meta["rejected_rows"].value == 1


def test_news_raw_clean_run_records_zero_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relevant article is kept; the reject sink gets no insert and the
    per-run metric is emitted as 0 (so the dashboard sees every run)."""
    kept = _finnhub_article()  # default headline names NVDA, url is a direct outlet
    monkeypatch.setattr(_NEWS_MODULE, "fetch_company_news", lambda *a, **k: [kept])

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    news_raw(ctx, _NEWS_MODULE.NewsRawConfig(), clickhouse=ch)  # type: ignore[arg-type]

    assert ch.reject_rows().empty
    assert not any(table == REJECTS_TABLE for table, _ in ch.inserts)
    meta = ctx.get_output_metadata("result")
    assert meta is not None and meta["rejected_rows"].value == 0


def test_news_raw_no_articles_still_emits_zero_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Finnhub returns nothing the asset exits early, but still emits a
    rejected_rows=0 metric so the QNT-240 dashboard series has no gaps."""
    monkeypatch.setattr(_NEWS_MODULE, "fetch_company_news", lambda *a, **k: [])

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    news_raw(ctx, _NEWS_MODULE.NewsRawConfig(), clickhouse=ch)  # type: ignore[arg-type]

    assert ch.inserts == []
    meta = ctx.get_output_metadata("result")
    assert meta is not None and meta["rejected_rows"].value == 0


def test_news_raw_records_unusable_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structurally-broken article (no url) is classified 'unusable'."""
    broken = _finnhub_article(url="", headline="NVDA something")
    monkeypatch.setattr(_NEWS_MODULE, "fetch_company_news", lambda *a, **k: [broken])

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    news_raw(ctx, _NEWS_MODULE.NewsRawConfig(), clickhouse=ch)  # type: ignore[arg-type]

    df = ch.reject_rows()
    assert len(df) == 1
    assert df["reason"].iloc[0] == "unusable"


# ── fundamentals: NaN-period skip drop site ───────────────────────────────────


def _frame(line_items: dict[str, list[float]], periods: list[str]) -> pd.DataFrame:
    cols = pd.to_datetime(periods)
    return pd.DataFrame(line_items, index=cols).T


class _FakeStock:
    def __init__(self, quarterly: pd.DataFrame) -> None:
        self.info = {"ebitda": 0, "sharesOutstanding": 1, "marketCap": 0}
        self.quarterly_financials = quarterly
        self.quarterly_balance_sheet = pd.DataFrame()
        self.quarterly_cashflow = pd.DataFrame()
        self.financials = pd.DataFrame()
        self.balance_sheet = pd.DataFrame()
        self.cashflow = pd.DataFrame()


def test_fundamentals_records_nan_period_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A period whose Total Revenue is NaN lands in the sink as reason=nan_period
    rather than being silently skipped."""
    quarterly = _frame(
        {"Total Revenue": [np.nan], "Gross Profit": [np.nan], "Net Income": [np.nan]},
        ["2026-03-31"],
    )
    monkeypatch.setattr(_FUND_MODULE.yf, "Ticker", lambda _t: _FakeStock(quarterly))

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="AAPL")
    fundamentals(ctx, clickhouse=ch)  # type: ignore[arg-type]

    df = ch.reject_rows()
    assert len(df) == 1
    assert df["reason"].iloc[0] == "nan_period"
    assert df["ticker"].iloc[0] == "AAPL"
    assert "2026-03-31" in df["raw_payload"].iloc[0]


# ── ohlcv_raw: empty / failed fetch drop sites ────────────────────────────────


def test_ohlcv_records_empty_fetch_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", lambda *a, **k: pd.DataFrame())

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    ohlcv_raw(ctx, OHLCVConfig(period="5d"), clickhouse=ch)  # type: ignore[arg-type]

    df = ch.reject_rows()
    assert len(df) == 1
    assert df["reason"].iloc[0] == "empty_fetch"
    assert df["ticker"].iloc[0] == "NVDA"


def test_ohlcv_records_failed_fetch_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise ValueError("connection reset")

    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", _boom)
    monkeypatch.setattr(_OHLCV_MODULE.time, "sleep", lambda _s: None)

    ch = _RecordingClickHouse()
    ctx = build_asset_context(partition_key="NVDA")
    ohlcv_raw(ctx, OHLCVConfig(period="5d"), clickhouse=ch)  # type: ignore[arg-type]

    df = ch.reject_rows()
    assert len(df) == 1
    assert df["reason"].iloc[0] == "fetch_failed"
    assert "connection reset" in df["detail"].iloc[0]
