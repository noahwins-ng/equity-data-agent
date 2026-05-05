"""Unit tests for retry_helpers (QNT-63).

Covers Retry-After header parsing (delta-seconds, HTTP-date, junk) and the
exception-walking helper used by the yfinance and Finnhub callers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from dagster_pipelines.retry_helpers import (
    MAX_RETRY_AFTER_SECONDS,
    parse_retry_after,
    retry_after_seconds_from_exception,
)

# ── parse_retry_after ─────────────────────────────────────────────────────────


def test_parse_retry_after_seconds_format() -> None:
    """Plain delta-seconds — the form Yahoo and Finnhub typically return."""
    assert parse_retry_after("60") == 60.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("  120  ") == 120.0  # surrounding whitespace tolerated


def test_parse_retry_after_http_date_format() -> None:
    """RFC 9110 HTTP-date format. Use a date 30s in the future so the math
    is deterministic enough to assert a window."""
    target = datetime.now(UTC) + timedelta(seconds=30)
    header = format_datetime(target, usegmt=True)
    result = parse_retry_after(header)
    assert result is not None
    # Allow 5s of test execution slop.
    assert 25.0 <= result <= 30.5


def test_parse_retry_after_past_http_date_clamps_to_zero() -> None:
    """An HTTP-date in the past shouldn't sleep negative seconds."""
    past = datetime.now(UTC) - timedelta(minutes=5)
    header = format_datetime(past, usegmt=True)
    assert parse_retry_after(header) == 0.0


def test_parse_retry_after_clamps_to_ceiling() -> None:
    """A hostile / mis-formatted long delay must not sleep the asset for hours."""
    assert parse_retry_after("999999") == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_returns_none_for_garbage() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("not-a-number-or-date") is None


def test_parse_retry_after_accepts_bytes() -> None:
    """httpx headers occasionally surface as bytes via raw access."""
    assert parse_retry_after(b"45") == 45.0


# ── retry_after_seconds_from_exception ────────────────────────────────────────


def _httpx_status_error(
    status: int, headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    """Construct a real httpx.HTTPStatusError carrying the provided headers."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("simulated", request=request, response=response)


def test_retry_after_from_exception_reads_seconds_header() -> None:
    """The QNT-63 AC #1 path: a 429 carrying ``Retry-After: 60`` must be
    honored by sleeping 60s before re-raising. The asset code asserts the
    sleep separately; this test pins the header → seconds extraction."""
    exc = _httpx_status_error(429, {"Retry-After": "60"})
    assert retry_after_seconds_from_exception(exc) == 60.0


def test_retry_after_from_exception_reads_http_date_header() -> None:
    """Same path, HTTP-date variant — RFC 9110 allows either form."""
    target = datetime.now(UTC) + timedelta(seconds=45)
    exc = _httpx_status_error(429, {"Retry-After": format_datetime(target, usegmt=True)})
    result = retry_after_seconds_from_exception(exc)
    assert result is not None
    assert 40.0 <= result <= 45.5


def test_retry_after_from_exception_returns_none_when_header_absent() -> None:
    """No header → fall through to the caller's default delay."""
    exc = _httpx_status_error(429)
    assert retry_after_seconds_from_exception(exc) is None


def test_retry_after_from_exception_handles_no_response_attribute() -> None:
    """yfinance's YFRateLimitError discards the response — the helper must
    not blow up; callers fall back to a fixed delay in that case."""

    class FakeYFRateLimitError(Exception):
        """Stand-in for yfinance.exceptions.YFRateLimitError."""

    assert retry_after_seconds_from_exception(FakeYFRateLimitError("Too Many Requests.")) is None


def test_retry_after_from_exception_is_case_insensitive() -> None:
    """httpx normalizes header names internally (case-insensitive Headers).
    Plain dicts are not — the helper covers both paths so a future caller
    constructing an exception with a vanilla dict still works."""

    class _PlainDictResponse:
        def __init__(self, headers: dict[str, Any]) -> None:
            self.headers = headers

    class _Wrapped(Exception):
        def __init__(self, headers: dict[str, Any]) -> None:
            super().__init__("plain-dict response")
            self.response = _PlainDictResponse(headers)

    # httpx Headers (case-insensitive)
    exc = _httpx_status_error(429, {"retry-after": "30"})
    assert retry_after_seconds_from_exception(exc) == 30.0
    # Plain dict (case-sensitive) — both spellings tried internally
    assert retry_after_seconds_from_exception(_Wrapped({"Retry-After": "20"})) == 20.0
    assert retry_after_seconds_from_exception(_Wrapped({"retry-after": "10"})) == 10.0


def test_retry_after_from_exception_returns_none_for_garbage_header() -> None:
    """Malformed Retry-After short-circuits to the caller's default rather
    than returning 0 (which would mean 'don't sleep at all')."""
    exc = _httpx_status_error(429, {"Retry-After": "not-a-number"})
    assert retry_after_seconds_from_exception(exc) is None


# ── Plain non-HTTP exceptions ─────────────────────────────────────────────────


def test_retry_after_from_plain_exception_returns_none() -> None:
    """ValueError, RuntimeError, etc. — no response attribute at all."""
    assert retry_after_seconds_from_exception(ValueError("boom")) is None


@pytest.mark.parametrize(
    "header_value,expected",
    [
        ("60", 60.0),
        (b"60", 60.0),
        ("60.5", 60.5),
        ("0", 0.0),
        ("-1", 0.0),  # negative clamps to zero rather than sleeping the wrong direction
    ],
)
def test_parse_retry_after_parametrized(header_value: str | bytes, expected: float) -> None:
    assert parse_retry_after(header_value) == expected
