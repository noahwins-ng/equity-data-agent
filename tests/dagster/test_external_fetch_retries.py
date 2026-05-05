"""Asset + fetcher retry behavior (QNT-63).

Three surfaces verified end-to-end with stubbed transports / monkeypatched
yfinance, so the AC items are pinned at the call-site, not just the helper:

1. ``ohlcv_raw`` 429 with Retry-After → sleeps the header value, then re-raises.
2. ``fundamentals`` 429 with Retry-After → same path.
3. ``fetch_company_news`` survives a single transient 5xx; 3 consecutive 5xx
   bubble to ``httpx.HTTPStatusError`` so the asset-level RetryPolicy engages.

Each asset test uses ``build_asset_context`` rather than going through the
full Dagster materialization machinery — that path is covered by
``test_retry_policy.py``. We only need to drive the function body and
assert on the side effects (sleep duration, raised exception).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from importlib import import_module

import httpx
import pytest
from dagster import Backoff, Jitter, build_asset_context
from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.news_raw import news_raw
from dagster_pipelines.assets.ohlcv_raw import OHLCVConfig, ohlcv_raw

# ``dagster_pipelines.assets.__init__`` re-exports the AssetsDefinition under
# the same name as the submodule, so ``import dagster_pipelines.assets.ohlcv_raw``
# resolves the attribute (the asset) instead of the module. Use import_module
# to grab the actual module object so monkeypatching ``time``/``yf`` works.
_OHLCV_MODULE = import_module("dagster_pipelines.assets.ohlcv_raw")
_FUND_MODULE = import_module("dagster_pipelines.assets.fundamentals")

# ── shared helpers ────────────────────────────────────────────────────────────


def _httpx_status_error(
    status: int,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    """Real ``httpx.HTTPStatusError`` carrying the requested headers.

    The asset 429 paths only inspect ``exc.response.headers['Retry-After']``;
    a real httpx response object exercises the same attribute walk
    production hits when an httpx-based session surfaces the 429.
    """
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(
        f"simulated {status}",
        request=request,
        response=response,
    )


@pytest.fixture
def captured_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every ``time.sleep`` call inside both yfinance asset modules.

    Both assets import ``time`` at module scope and call ``time.sleep`` only
    in the 429-retry-after path and the post-insert rate-limiter. Tests
    assert on the first element (the Retry-After sleep); the rate-limit
    sleep at the end never fires here because the asset re-raises before
    reaching it.
    """
    sleeps: list[float] = []

    def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_OHLCV_MODULE.time, "sleep", _record)
    monkeypatch.setattr(_FUND_MODULE.time, "sleep", _record)
    return sleeps


# ── ohlcv_raw 429 + Retry-After ──────────────────────────────────────────────


def test_ohlcv_raw_429_with_retry_after_sleeps_header_value(
    monkeypatch: pytest.MonkeyPatch,
    captured_sleeps: list[float],
) -> None:
    """AC #1: yfinance 429 carrying ``Retry-After: 60`` sleeps 60s before
    re-raising. Mocks the yfinance call to raise an httpx.HTTPStatusError
    with the header — the actual yfinance YFRateLimitError path uses the
    same handler but contributes no header (covered separately below)."""

    def _raise_429(*_args: object, **_kwargs: object) -> None:
        raise _httpx_status_error(429, {"Retry-After": "60"})

    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", _raise_429)

    context = build_asset_context(partition_key="NVDA")
    with pytest.raises(httpx.HTTPStatusError):
        ohlcv_raw(context, OHLCVConfig(period="5d"), clickhouse=_DummyClickHouse())

    assert captured_sleeps == [60.0]


def test_ohlcv_raw_429_without_retry_after_re_raises_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
    captured_sleeps: list[float],
) -> None:
    """yfinance's own ``YFRateLimitError`` discards the response — there's
    no header to honor, so the asset re-raises immediately and the
    asset-level RetryPolicy applies the configured 30s exp+jitter delay."""

    class _FakeYFRateLimitError(Exception):
        """Stand-in matching yfinance.exceptions.YFRateLimitError shape."""

    def _raise_yf429(*_args: object, **_kwargs: object) -> None:
        raise _FakeYFRateLimitError("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", _raise_yf429)

    context = build_asset_context(partition_key="NVDA")
    with pytest.raises(_FakeYFRateLimitError):
        ohlcv_raw(context, OHLCVConfig(period="5d"), clickhouse=_DummyClickHouse())

    # No sleep — header absent. The asset RetryPolicy owns the wait.
    assert captured_sleeps == []


def test_ohlcv_raw_non_429_failure_skips_silently(
    monkeypatch: pytest.MonkeyPatch,
    captured_sleeps: list[float],
) -> None:
    """A non-rate-limit failure (e.g. timeout) logs and returns — no retry,
    no sleep. Pre-existing behavior; covered to pin against regressions
    when the 429 branch is touched."""

    def _raise_other(*_args: object, **_kwargs: object) -> None:
        raise ValueError("connection reset")

    monkeypatch.setattr(_OHLCV_MODULE.yf, "download", _raise_other)

    context = build_asset_context(partition_key="NVDA")
    # Returns normally — does not raise.
    ohlcv_raw(context, OHLCVConfig(period="5d"), clickhouse=_DummyClickHouse())
    assert captured_sleeps == []


# ── fundamentals 429 + Retry-After ───────────────────────────────────────────


def test_fundamentals_429_with_retry_after_sleeps_header_value(
    monkeypatch: pytest.MonkeyPatch,
    captured_sleeps: list[float],
) -> None:
    """AC #1 mirror for the second yfinance call site. Same handler, same
    assertions — pinning both call sites stops a future refactor that
    only updates one of them."""

    def _raise_429(*_args: object, **_kwargs: object) -> object:
        raise _httpx_status_error(429, {"Retry-After": "60"})

    monkeypatch.setattr(_FUND_MODULE.yf, "Ticker", _raise_429)

    context = build_asset_context(partition_key="NVDA")
    with pytest.raises(httpx.HTTPStatusError):
        fundamentals(context, clickhouse=_DummyClickHouse())

    assert captured_sleeps == [60.0]


def test_fundamentals_429_without_retry_after_re_raises_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
    captured_sleeps: list[float],
) -> None:
    """Mirror of the ohlcv test — confirms YFRateLimitError still bubbles
    cleanly with no extra sleep when no header is attached."""

    class _FakeYFRateLimitError(Exception):
        pass

    def _raise_yf429(*_args: object, **_kwargs: object) -> object:
        raise _FakeYFRateLimitError("Too Many Requests. Rate limited.")

    monkeypatch.setattr(_FUND_MODULE.yf, "Ticker", _raise_yf429)

    context = build_asset_context(partition_key="NVDA")
    with pytest.raises(_FakeYFRateLimitError):
        fundamentals(context, clickhouse=_DummyClickHouse())

    assert captured_sleeps == []


# ── fetch_company_news intra-attempt 5xx retry ────────────────────────────────


@pytest.fixture(autouse=True)
def _set_finnhub_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    from shared.config import settings

    monkeypatch.setattr(settings, "FINNHUB_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _disable_finnhub_intra_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the in-attempt backoff sleep so tests don't actually wait
    1–2s between retry tries. The retry *count* is what matters here,
    not the wall-clock delay between retries."""
    from dagster_pipelines import news_feeds as _news_feeds

    monkeypatch.setattr(_news_feeds.time, "sleep", lambda _s: None)


def _make_handler_sequence(
    statuses: list[int],
    payload: list[dict[str, str]] | None = None,
    *,
    headers_at: dict[int, dict[str, str]] | None = None,
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    """Build a httpx handler that returns ``statuses[i]`` on the i-th call.

    Returns the handler and a mutable counter so tests can assert on the
    number of calls made (= retry attempts consumed).
    """
    counter = [0]
    headers_at = headers_at or {}

    def handler(_request: httpx.Request) -> httpx.Response:
        i = counter[0]
        counter[0] += 1
        status = statuses[i]
        if status == 200:
            return httpx.Response(200, json=payload or [])
        return httpx.Response(status, headers=headers_at.get(i, {}))

    return handler, counter


def test_fetch_company_news_survives_single_transient_5xx() -> None:
    """AC #2 first half: 503 → retry → 200 succeeds without raising. The
    asset-level RetryPolicy is never engaged because the fetcher absorbed
    the blip in-attempt."""
    from dagster_pipelines.news_feeds import fetch_company_news

    payload = [{"headline": "ok", "url": "https://x.example/1"}]
    handler, counter = _make_handler_sequence([503, 200], payload=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_company_news(
            "NVDA",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 7),
            client=client,
        )

    assert result == payload
    assert counter[0] == 2  # one fail, one success — one retry consumed


def test_fetch_company_news_three_consecutive_5xx_bubble_to_caller() -> None:
    """AC #2 second half: 503 / 503 / 503 exhausts the in-attempt budget
    and raises ``httpx.HTTPStatusError`` so the asset RetryPolicy can
    re-launch. The ``2`` retry budget + 1 initial attempt = 3 total tries."""
    from dagster_pipelines.news_feeds import fetch_company_news

    handler, counter = _make_handler_sequence([503, 503, 503])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_company_news(
                "NVDA",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 7),
                client=client,
            )

    assert counter[0] == 3


def test_fetch_company_news_4xx_does_not_retry() -> None:
    """Auth / bad-symbol failures (401, 403, 404) are not transient. They
    must raise immediately so the asset surfaces the misconfiguration —
    burning retry budget on a permanent error wastes time and noise."""
    from dagster_pipelines.news_feeds import fetch_company_news

    handler, counter = _make_handler_sequence([401])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_company_news(
                "NVDA",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 7),
                client=client,
            )

    assert counter[0] == 1  # no retry attempts


def test_fetch_company_news_429_retry_after_drives_sleep_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #1 mirror for Finnhub: a 429 carrying ``Retry-After: 5`` makes
    the in-attempt loop sleep ~5s before retrying. Captures the actual
    sleep argument so a refactor that drops the header parse fails here."""
    from dagster_pipelines import news_feeds as _news_feeds
    from dagster_pipelines.news_feeds import fetch_company_news

    sleeps: list[float] = []
    monkeypatch.setattr(_news_feeds.time, "sleep", lambda s: sleeps.append(s))

    payload = [{"headline": "after-rate-limit", "url": "https://x.example/y"}]
    handler, _counter = _make_handler_sequence(
        [429, 200],
        payload=payload,
        headers_at={0: {"Retry-After": "5"}},
    )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_company_news(
            "NVDA",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 7),
            client=client,
        )

    assert result == payload
    # First sleep is the Retry-After honor; nothing else should have slept
    # in this code path (no explicit base-delay use because header was set).
    assert sleeps == [5.0]


def test_fetch_company_news_5xx_without_retry_after_uses_base_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the upstream omits Retry-After, the in-attempt loop falls
    back to exponential base delay (1s, then 2s) so we still pace
    ourselves under Finnhub's 60 RPM bucket."""
    from dagster_pipelines import news_feeds as _news_feeds
    from dagster_pipelines.news_feeds import (
        _INTRA_ATTEMPT_BASE_DELAY_SECONDS,
        fetch_company_news,
    )

    sleeps: list[float] = []
    monkeypatch.setattr(_news_feeds.time, "sleep", lambda s: sleeps.append(s))

    handler, _counter = _make_handler_sequence([503, 503, 200], payload=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetch_company_news(
            "NVDA",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 7),
            client=client,
        )

    # Exponential: 1s after first failure, 2s after second.
    assert sleeps == [_INTRA_ATTEMPT_BASE_DELAY_SECONDS, _INTRA_ATTEMPT_BASE_DELAY_SECONDS * 2]


# ── Asset RetryPolicy jitter check ────────────────────────────────────────────


def test_external_ingest_assets_use_jittered_backoff() -> None:
    """AC #3: ohlcv_raw, fundamentals, news_raw RetryPolicy uses jitter.

    Pre-QNT-63 these used Backoff.EXPONENTIAL with no jitter; thundering-
    herd risk was small at 10 partitions but cheap to fix here. Pin the
    config so a future refactor doesn't quietly drop it."""
    for asset_def in (ohlcv_raw, fundamentals, news_raw):
        # AssetsDefinition exposes the underlying op (carrying RetryPolicy)
        # as ``.op``; the policy itself lives on ``OpDefinition.retry_policy``.
        policy = asset_def.op.retry_policy
        assert policy is not None, f"{asset_def.key} missing retry_policy"
        assert policy.max_retries == 3
        assert policy.delay == 30
        assert policy.backoff == Backoff.EXPONENTIAL
        assert policy.jitter == Jitter.PLUS_MINUS, (
            f"{asset_def.key} backoff missing jitter — see QNT-63"
        )


# ── Stub for ClickHouse resource ──────────────────────────────────────────────


class _DummyClickHouse:
    """Asset 429 paths re-raise before reaching insert_df, so a no-op stub
    is sufficient. Keeps the test free of ClickHouseResource construction
    cost (which would otherwise require pull-from-env config + a tunnel)."""

    def insert_df(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("insert_df must not be called on the 429-path tests")
