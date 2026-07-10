"""Tests for the company-logo endpoint (QNT-162, QNT-163).

Focused on the SSRF allowlist that pins the second-stage GET to a Finnhub
CDN host — the JSON returned from ``/stock/profile2`` is attacker-influenced
(any compromise of Finnhub or a network MITM could substitute the URL), so
the host check is the only thing preventing the request handler from
reaching arbitrary internal endpoints. Two scenarios matter:

1. **Real Finnhub shards must be accepted.** QNT-163 surfaced this
   in prod: Finnhub started returning ``static2.finnhub.io`` URLs and the
   strict equality check rejected every one, breaking logo display.
2. **Look-alike hosts must be rejected.** Subdomain spoofing
   (``static.finnhub.io.evil.com``), suffix tricks
   (``static.finnhub.io2``), unrelated hosts, and non-https URLs must all
   fail closed.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from api.routers import logos as logos_module
from api.routers.logos import _FINNHUB_CDN_HOST_PATTERN, _fetch_logo_data_url


@pytest.mark.parametrize(
    "host",
    [
        "static.finnhub.io",  # original (pre-shard)
        "static2.finnhub.io",  # current (post-shard, QNT-163)
        "static3.finnhub.io",  # future shard — forward-compat
        "static99.finnhub.io",  # high-cardinality forward-compat
    ],
)
def test_finnhub_cdn_pattern_accepts_real_shards(host: str) -> None:
    """The pattern must accept ``staticN.finnhub.io`` for any N >= 0,
    including bare ``static.finnhub.io``. This is the regression guard
    for QNT-163: a future Finnhub CDN expansion must not silently break
    logo display the way the original equality check did."""
    assert _FINNHUB_CDN_HOST_PATTERN.fullmatch(host) is not None


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",  # unrelated host
        "static.finnhub.io.evil.com",  # subdomain spoof
        "static.finnhub.io2",  # suffix trick
        "static.finnhub.com",  # tld swap
        "staticx.finnhub.io",  # non-numeric shard suffix
        "static-2.finnhub.io",  # hyphen instead of digit
        "STATIC.FINNHUB.IO",  # case mismatch (urlparse lowercases hostnames,
        #                       but defence-in-depth — the regex is anchored
        #                       to lowercase since urlparse normalises)
        "finnhub.io",  # missing the static. prefix
        "",  # empty string (parsed.hostname can be None or "")
    ],
)
def test_finnhub_cdn_pattern_rejects_lookalikes(host: str) -> None:
    """SSRF guard: hostnames that are NOT a Finnhub CDN shard must be
    rejected, including subdomain spoofs, suffix tricks, and unrelated
    domains. The cost of a false negative here is reaching an internal
    or attacker-controlled endpoint with a Finnhub-shaped URL."""
    assert _FINNHUB_CDN_HOST_PATTERN.fullmatch(host) is None


def test_max_logo_bytes_accommodates_observed_real_logos() -> None:
    """QNT-163 follow-up: the byte cap must accept the largest real
    Finnhub logo we've observed (JPM at ~83 KB) plus headroom. The
    original 64 KB cap from QNT-162 was set on a 5-15 KB assumption that
    turned out wrong for the bank / healthcare brands; this test pins the
    cap so a future "let's tighten it back to 64 KB" sweep doesn't
    silently re-break JPM-class logos.
    """
    from api.routers.logos import _MAX_LOGO_BYTES

    # Observed largest legitimate logo (JPM, May 2026): 83397 bytes.
    largest_observed = 83397
    headroom_factor = 1.5  # absorb a future bank with a slightly bigger PNG
    assert _MAX_LOGO_BYTES >= int(largest_observed * headroom_factor), (
        f"_MAX_LOGO_BYTES={_MAX_LOGO_BYTES} too tight; needs to accept "
        f"observed {largest_observed} bytes with headroom for future "
        f"larger logos (recommended >= {int(largest_observed * headroom_factor)})"
    )


class _FakeResponse:
    def __init__(self, *, json_data=None, content=b"", headers=None):
        self._json = json_data
        self.content = content
        self.headers = headers or {"content-type": "image/png"}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._json


class _ScriptedClient:
    """httpx.Client stand-in that plays back a scripted sequence of GET
    results (either a _FakeResponse or an Exception to raise)."""

    def __init__(self, script: list) -> None:
        self._script = script
        self.calls = 0

    def get(self, url, params=None):
        result = self._script[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def test_transient_failure_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout on the first profile2 fetch must not permanently blank the
    logo — the fetch retries and succeeds. This is the QNT prod regression:
    a single boot-time blip was caching None for the pod's whole lifetime
    (NVDA/MSFT one boot, AMD the next), then freezing into the Vercel build.
    """
    monkeypatch.setattr(logos_module, "_RETRY_BACKOFF_SECONDS", 0)  # no real sleep
    monkeypatch.setattr(logos_module.settings, "FINNHUB_API_KEY", "test-key")
    png = b"\x89PNG\r\n\x1a\nfake-bytes"
    good_url = "https://static2.finnhub.io/file/stock_logo/AMD.png"
    client = _ScriptedClient(
        [
            httpx.ReadTimeout("boom"),  # attempt 1: transient profile2 timeout
            _FakeResponse(json_data={"logo": good_url}),  # attempt 2: profile2 ok
            _FakeResponse(content=png),  # attempt 2: image bytes ok
        ]
    )

    result = _fetch_logo_data_url("AMD", client)  # type: ignore[arg-type]

    expected = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    assert result == expected
    assert client.calls == 3


def test_hard_failure_does_not_burn_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``logo`` field is terminal — return None on the first
    attempt without spending retries on a ticker Finnhub simply has no
    logo for."""
    monkeypatch.setattr(logos_module.settings, "FINNHUB_API_KEY", "test-key")
    client = _ScriptedClient([_FakeResponse(json_data={"logo": ""})])

    assert _fetch_logo_data_url("XYZ", client) is None  # type: ignore[arg-type]
    assert client.calls == 1
