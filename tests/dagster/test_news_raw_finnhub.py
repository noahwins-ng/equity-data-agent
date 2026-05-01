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
    resolve_publisher_host,
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


def _stub_resolver_client(handler: Any) -> httpx.Client:
    """An ``httpx.Client`` whose transport is the supplied handler.

    Tests use this to drive ``resolve_publisher_host`` deterministically —
    no real network in the unit-test path. Mirrors the
    ``fetch_company_news`` stubbing pattern below.
    """
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def test_article_to_row_maps_all_columns() -> None:
    article = _finnhub_article()
    # Direct outlet URL — resolver short-circuits to host(url), no transport call.
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
    # example.com is already a direct outlet — short-circuits to the host.
    assert row["resolved_host"] == "example.com"
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
def _disable_finnhub_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the inter-request sleep in tests.

    The resolver's 1.5s rate limit is correct for prod (under Finnhub's
    60 RPM bucket; see ``news_feeds._INTER_FINNHUB_REQUEST_SECONDS``)
    but would make every redirect-resolver test wait ~1.5s and turn the
    20-test suite into a 30s minimum. Zero is safe in tests because the
    transport is mocked; no real network call leaves the process.
    """
    from dagster_pipelines import news_feeds as _news_feeds

    monkeypatch.setattr(_news_feeds, "_INTER_FINNHUB_REQUEST_SECONDS", 0.0)
    monkeypatch.setattr(_news_feeds, "_FINNHUB_JITTER_SECONDS", 0.0)
    monkeypatch.setattr(_news_feeds, "_last_finnhub_request_at", 0.0)


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


# ── resolve_publisher_host ────────────────────────────────────────────────────


def test_resolve_publisher_host_short_circuits_for_direct_outlets() -> None:
    """Non-finnhub hosts skip the network entirely.

    AC #2 in QNT-148: "Skips resolution for URLs already on a non-finnhub.io host."
    Verified by handing in a transport that *errors* on every call — if the
    resolver tried to call it, the test would explode.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called for direct outlets")

    with _stub_resolver_client(handler) as client:
        assert resolve_publisher_host("https://www.fool.com/article", client=client) == "fool.com"
        assert (
            resolve_publisher_host("https://finance.yahoo.com/x", client=client)
            == "finance.yahoo.com"
        )
        assert resolve_publisher_host("https://cnbc.com/x", client=client) == "cnbc.com"


def test_resolve_publisher_host_follows_finnhub_redirect_to_outlet() -> None:
    """Standard Finnhub redirect path: HEAD on finnhub.io → 302 → outlet."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "finnhub.io":
            return httpx.Response(302, headers={"location": "https://www.cnbc.com/article"})
        if request.url.host == "www.cnbc.com":
            return httpx.Response(200)
        raise AssertionError(f"unexpected host {request.url.host}")

    with _stub_resolver_client(handler) as client:
        result = resolve_publisher_host("https://finnhub.io/api/news?id=12345", client=client)
    # `www.` stripped — frontend used to do this, now centralised at the API
    # boundary so the canonical publisher field is render-ready.
    assert result == "cnbc.com"


def test_resolve_publisher_host_soft_fails_on_timeout() -> None:
    """Per AC #2: timeouts soft-fail to '' so the asset doesn't crash on
    flaky outlet servers — the frontend pill falls back via the API's
    ``multiIf`` chain (resolved_host → host(url) → publisher_name)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated outlet timeout")

    with _stub_resolver_client(handler) as client:
        assert resolve_publisher_host("https://finnhub.io/api/news?id=1", client=client) == ""


def test_resolve_publisher_host_soft_fails_on_4xx() -> None:
    """Outlet returning a 404 / 410 also soft-fails — we'd rather store ''
    and let the publisher_name fallback render than persist a misleading host."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with _stub_resolver_client(handler) as client:
        assert resolve_publisher_host("https://finnhub.io/api/news?id=2", client=client) == ""


def test_resolve_publisher_host_handles_405_with_get_fallback() -> None:
    """Some outlet servers (cloudflare-fronted) reject HEAD with 405 but
    accept GET. The resolver retries with a streamed GET in that case.

    Real-world topology: Finnhub serves a clean 302; the *outlet* is what
    rejects HEAD. We model that here so a future regression that special-
    cases "405 only on the redirect host" surfaces against this test.
    """

    call_count = {"head": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            call_count["head"] += 1
            if request.url.host == "finnhub.io":
                # Finnhub itself answers HEAD with a clean redirect.
                return httpx.Response(302, headers={"location": "https://seekingalpha.com/article"})
            # Outlet (cloudflare-fronted) rejects HEAD entirely.
            return httpx.Response(405)
        call_count["get"] += 1
        # GET fallback: Finnhub redirects, outlet returns 200 with body.
        if request.url.host == "finnhub.io":
            return httpx.Response(302, headers={"location": "https://seekingalpha.com/article"})
        return httpx.Response(200)

    with _stub_resolver_client(handler) as client:
        assert (
            resolve_publisher_host("https://finnhub.io/api/news?id=3", client=client)
            == "seekingalpha.com"
        )
    # HEAD ran at least once; GET fallback engaged because the outlet 405'd.
    assert call_count["head"] >= 1
    assert call_count["get"] >= 1


def test_resolve_publisher_host_returns_empty_when_final_url_stays_on_finnhub() -> None:
    """If headers come back with a 200 and the final URL is still finnhub.io
    (no redirect happened — Finnhub returned a 200 directly, or the chain
    looped back), we treat as unresolved. The API fallback chain renders
    ``publisher_name`` instead of crediting "finnhub.io" as the outlet."""

    def handler(request: httpx.Request) -> httpx.Response:
        # 200 with no redirect — final URL stays on finnhub.io.
        if request.url.host == "finnhub.io":
            return httpx.Response(200)
        raise AssertionError("should not have left finnhub.io")

    with _stub_resolver_client(handler) as client:
        assert resolve_publisher_host("https://finnhub.io/api/news?id=4", client=client) == ""


def test_resolve_publisher_host_soft_fails_on_redirect_loop() -> None:
    """Httpx raises TooManyRedirects when ``max_redirects`` is exceeded.
    The resolver must catch it (it's a subclass of ``httpx.HTTPError``)
    and return '' rather than letting the partition run crash on a
    malformed redirect chain."""

    def handler(_request: httpx.Request) -> httpx.Response:
        # Always redirect back to ourselves — httpx will hit max_redirects
        # and raise TooManyRedirects before the resolver sees a status.
        return httpx.Response(302, headers={"location": "https://finnhub.io/api/news?id=loop"})

    with _stub_resolver_client(handler) as client:
        assert resolve_publisher_host("https://finnhub.io/api/news?id=loop", client=client) == ""


def test_resolve_publisher_host_handles_malformed_url() -> None:
    """Garbage URLs return '' rather than blowing up the partition run."""
    assert resolve_publisher_host("") == ""
    assert resolve_publisher_host("not-a-url") == ""


def test_finnhub_rate_limit_sleeps_between_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inter-request sleep guards every Finnhub call.

    Drives the helper directly (rather than through ``resolve_publisher_host``)
    so the assertion is on the rate-limit math and not coupled to which
    branches inside the resolver fire.

    The autouse fixture above zeroes out ``_INTER_FINNHUB_REQUEST_SECONDS``
    for the rest of the suite; this test re-arms it. Without this regression
    test the 1.5s production sleep could be silently dropped by a refactor
    and AC#4 would quietly regress from 80%+ back to 43% (per QNT-148 prod
    smoke pre-rate-limiter).
    """
    from dagster_pipelines import news_feeds as _news_feeds

    monkeypatch.setattr(_news_feeds, "_INTER_FINNHUB_REQUEST_SECONDS", 0.05)
    monkeypatch.setattr(_news_feeds, "_FINNHUB_JITTER_SECONDS", 0.0)
    monkeypatch.setattr(_news_feeds, "_last_finnhub_request_at", 0.0)

    sleep_calls: list[float] = []
    monkeypatch.setattr(_news_feeds.time, "sleep", lambda d: sleep_calls.append(d))

    # Frozen monotonic so the math is determ. The function calls monotonic
    # twice per invocation (once to compute, once to write the tracker); a
    # single fixed value is enough because the test isn't simulating real
    # elapsed time, just the "is the budget consumed" check.
    monkeypatch.setattr(_news_feeds.time, "monotonic", lambda: 1.0)

    # First call: tracker=0.0, now=1.0 → deadline=0.05, 1.0 >= 0.05, no sleep.
    _news_feeds._sleep_for_finnhub_rate_limit()
    assert sleep_calls == []

    # Second call: tracker=1.0 (set by the first call), now=1.0 → deadline=1.05,
    # 1.0 < 1.05 → sleep ~0.05s.
    _news_feeds._sleep_for_finnhub_rate_limit()
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.05, abs=1e-9)


# ── _article_to_row + resolver wiring ─────────────────────────────────────────


def test_article_to_row_populates_resolved_host_for_finnhub_redirect() -> None:
    """The asset hands its pooled httpx.Client to ``_article_to_row`` so
    every article in the partition shares one connection pool. AC #2:
    finnhub.io URLs get resolved; non-finnhub URLs short-circuit."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "finnhub.io":
            return httpx.Response(302, headers={"location": "https://www.benzinga.com/article"})
        return httpx.Response(200)

    with _stub_resolver_client(handler) as client:
        row = _article_to_row(
            _finnhub_article(url="https://finnhub.io/api/news?id=999"),
            ticker="MSFT",
            resolver_client=client,
        )

    assert row is not None
    assert row["resolved_host"] == "benzinga.com"


# Hide the test-key env var from any subsequent integration-style imports.
def teardown_module() -> None:
    os.environ.pop("FINNHUB_API_KEY", None)
