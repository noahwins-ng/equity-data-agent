"""Shared API-test fixtures (QNT-161).

The chat endpoint now carries a SlowAPI rate limiter and an in-memory
per-IP / global token budget. Both are module-level singletons that
persist across the entire test session by default — without a per-test
reset the second test that POSTs to ``/api/v1/agent/chat`` from the same
TestClient hits the cap (``5/minute`` from ``(testclient)``) and the
assertion suite collapses with confusing 429 noise.

This conftest resets both stores around every test in ``tests/api/`` so
each test runs against a clean limiter / budget regardless of execution
order. Tests that explicitly want to exercise the rate-limit edge
(e.g. ``test_rate_limit_returns_429``) get a clean slate too — they fire
the requests they need and the next test starts from zero.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api.routers import agent_chat as chat_module
from api.security import budget, burst_alerter, limiter


@pytest.fixture(autouse=True)
def _reset_security_state() -> Iterator[None]:
    """Clear the limiter, token budget, burst alerter, AND the per-process
    breaker-alerted-date before AND after each test. Both ends are
    belt-and-braces: ``before`` covers cross-test bleed-in; ``after`` keeps
    the test session clean for any later module that imports the same
    singletons.

    ``_BREAKER_ALERTED_DATE`` is module-level state in ``agent_chat`` that
    dedups the daily Sentry trip-alert; without resetting it here, a test
    earlier in the run could prime today's value and a later test
    exercising the breaker path would get a (false) "already alerted"
    skip. Test independence wins over the production "one alert per day
    per worker" semantic in the test process.
    """
    limiter.reset()
    budget.reset()
    burst_alerter.reset()
    chat_module._BREAKER_ALERTED_DATE = None
    yield
    limiter.reset()
    budget.reset()
    burst_alerter.reset()
    chat_module._BREAKER_ALERTED_DATE = None
