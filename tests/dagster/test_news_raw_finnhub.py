"""Tests for the Finnhub /company-news mapping in news_raw (QNT-141).

The asset itself runs through Dagster machinery in
``test_news_asset_checks.py``; this file pins the *mapping contract* between
Finnhub's response shape and our news_raw row shape so a vendor field rename
or unit-format flip surfaces here, not in production.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from dagster_pipelines.assets.news_raw import _article_to_row, _url_hash
from dagster_pipelines.news_feeds import (
    FinnhubAPIKeyMissing,
    fetch_company_news,
)

# ── _article_to_row ───────────────────────────────────────────────────────────


def _finnhub_article(**overrides: Any) -> dict[str, Any]:
    """Return a complete Finnhub /company-news article dict, with overrides."""
    base: dict[str, Any] = {
        "category": "company news",
        "datetime": 1714000000,  # 2024-04-25 epoch UTC
        "headline": "NVDA reports record Q1 revenue",
        "id": 12345,
        "image": "https://cdn.example.com/nvda-q1.jpg",
        "related": "NVDA",
        "source": "Reuters",
        "summary": "Revenue grew 114% YoY to $26B.",
        "url": "https://example.com/nvda-q1-2024",
    }
    base.update(overrides)
    return base


def test_article_to_row_maps_all_columns() -> None:
    article = _finnhub_article()
    row = _article_to_row(article, ticker="NVDA")
    assert row is not None
    assert row["id"] == _url_hash(article["url"])
    assert row["ticker"] == "NVDA"
    assert row["headline"] == article["headline"]
    assert row["body"] == article["summary"]
    # Ingest provenance: literal string "finnhub", not the originating publisher.
    assert row["source"] == "finnhub"
    assert row["url"] == article["url"]
    assert row["publisher_name"] == article["source"]
    assert row["image_url"] == article["image"]
    assert row["sentiment_label"] == "pending"
    # Epoch -> naive UTC datetime, matching ClickHouse DateTime semantics.
    expected_published = datetime.fromtimestamp(article["datetime"], tz=UTC).replace(tzinfo=None)
    assert row["published_at"] == expected_published


def test_article_to_row_skips_when_url_missing() -> None:
    assert _article_to_row(_finnhub_article(url=""), ticker="NVDA") is None
    assert _article_to_row(_finnhub_article(url="   "), ticker="NVDA") is None


def test_article_to_row_skips_when_headline_missing() -> None:
    assert _article_to_row(_finnhub_article(headline=""), ticker="NVDA") is None


def test_article_to_row_skips_when_datetime_invalid() -> None:
    assert _article_to_row(_finnhub_article(datetime=0), ticker="NVDA") is None
    assert _article_to_row(_finnhub_article(datetime=-1), ticker="NVDA") is None
    assert _article_to_row(_finnhub_article(datetime="not an int"), ticker="NVDA") is None


def test_article_to_row_handles_missing_image_and_summary() -> None:
    """Empty image + empty summary are valid — design v2 renders placeholder."""
    row = _article_to_row(
        _finnhub_article(image="", summary=""),
        ticker="AAPL",
    )
    assert row is not None
    assert row["image_url"] == ""
    assert row["body"] == ""


def test_article_to_row_strips_whitespace() -> None:
    row = _article_to_row(
        _finnhub_article(
            headline="  Headline with whitespace  ",
            summary="  body  ",
            url="  https://example.com/x  ",
            source="  Reuters  ",
            image="  https://i.example.com/x.jpg  ",
        ),
        ticker="AAPL",
    )
    assert row is not None
    assert row["headline"] == "Headline with whitespace"
    assert row["body"] == "body"
    assert row["url"] == "https://example.com/x"
    assert row["publisher_name"] == "Reuters"
    assert row["image_url"] == "https://i.example.com/x.jpg"


def test_url_hash_is_deterministic_and_per_url() -> None:
    """Same URL -> same id; different URL -> different id (collision-free dedup key)."""
    a = _url_hash("https://example.com/a")
    b = _url_hash("https://example.com/b")
    assert a == _url_hash("https://example.com/a")
    assert a != b


# ── fetch_company_news ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _set_finnhub_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-empty key so most tests skip the missing-key guard."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    # shared.config.settings is a module-level singleton evaluated at import
    # time; patch the attribute directly so news_feeds sees the test key.
    from shared.config import settings

    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "test-key")


def test_fetch_company_news_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(FinnhubAPIKeyMissing):
        fetch_company_news("NVDA", from_date=date(2024, 1, 1), to_date=date(2024, 1, 7))


def test_fetch_company_news_returns_articles_on_200() -> None:
    """Stub the httpx transport so the request never leaves the process."""
    payload = [_finnhub_article(), _finnhub_article(headline="Second", url="https://example.com/2")]

    def handler(request: httpx.Request) -> httpx.Response:
        # Sanity-check the request URL and params we constructed.
        assert request.url.path == "/api/v1/company-news"
        assert request.url.params["symbol"] == "NVDA"
        assert request.url.params["from"] == "2024-01-01"
        assert request.url.params["to"] == "2024-01-07"
        assert request.url.params["token"] == "test-key"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        articles = fetch_company_news(
            "NVDA",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 7),
            client=client,
        )
    assert len(articles) == 2
    assert articles[0]["headline"] == payload[0]["headline"]


def test_fetch_company_news_returns_empty_list_on_object_payload() -> None:
    """Finnhub error payloads come back as {"error": "..."} instead of a list.

    The fetcher logs a warning and returns [] so the asset can decide
    'no news this tick' rather than crashing on indexing into a dict."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "Symbol not supported"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        articles = fetch_company_news(
            "BOGUS",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 1),
            client=client,
        )
    assert articles == []


def test_fetch_company_news_raises_on_4xx() -> None:
    """Auth / rate-limit failures must raise so the asset's RetryPolicy can engage."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API key."})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_company_news(
                "NVDA",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 1),
                client=client,
            )


# Hide the test-key env var from any subsequent integration-style imports.
def teardown_module() -> None:
    os.environ.pop("FINNHUB_API_KEY", None)
