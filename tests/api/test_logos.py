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

import pytest
from api.routers.logos import _FINNHUB_CDN_HOST_PATTERN


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
