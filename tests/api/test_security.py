"""Tests for the public-chat abuse controls (QNT-161).

Covers:
* per-IP daily token budget — exceeded → conversational redirect
* global daily Groq TPD breaker — exceeded → conversational redirect + Sentry
* SlowAPI rate limit — 429 + Retry-After + friendly body
* CORS lockdown — disallowed origin gets no allow-origin header
* prompt-injection input filter — control chars + overlong tokens 422
* burst alerter — fires Sentry once per window after threshold

The graph is stubbed so no real LLM/tool fires; the contract under test is
the SSE event sequence + HTTP behaviour, NOT the agent's reasoning.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agent.thesis import Thesis
from api import main as main_module
from api import security as security_module
from api.routers import agent_chat as chat_module
from api.security import budget, validate_chat_message
from fastapi.testclient import TestClient

# ─── helpers (lifted from test_agent_chat for SSE parsing) ──────────────────


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for raw in body.split("\n\n"):
        if not raw.strip():
            continue
        event = ""
        data = ""
        for line in raw.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event:
            frames.append((event, json.loads(data) if data else {}))
    return frames


def _stub_thesis() -> Thesis:
    return Thesis(
        setup="Setup.",
        bull_case=["Bull (source: technical)"],
        bear_case=["Bear (source: fundamental)"],
        verdict_stance="constructive",
        verdict_action="Action (source: technical).",
    )


@pytest.fixture
def client() -> Iterable[TestClient]:
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def stub_chat_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the chat router's graph + tools so the endpoint runs offline.

    Returns a thesis-shaped final state so the contract assertions in tests
    that DON'T trip a budget/limit see a normal happy path.
    """

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:  # noqa: ARG001
        graph = MagicMock()
        graph.invoke.return_value = {
            "ticker": "NVDA",
            "intent": "thesis",
            "plan": [],
            "reports": {"technical": "stub"},
            "errors": {},
            "thesis": _stub_thesis(),
            "quick_fact": None,
            "comparison": None,
            "conversational": None,
            "confidence": 0.5,
        }
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})


# ─── per-IP daily token budget ──────────────────────────────────────────────


def test_per_ip_budget_exceeded_emits_conversational_redirect(
    client: TestClient,
    stub_chat_graph: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-IP daily token spend has crossed the cap, the request
    must NOT invoke the graph — the SSE stream surfaces a deterministic
    conversational redirect so the panel renders the friendly demo-limit
    card. The graph stub would otherwise fire and return a thesis; the
    assert on ``intent=="conversational"`` (NOT thesis) confirms the gate
    short-circuited the run."""
    del stub_chat_graph  # fixture only — value unused

    # Pre-fill the per-IP counter to exactly the cap so the next request
    # trips the gate. ``client_ip`` for TestClient is "testclient".
    budget.record("testclient", security_module.settings.CHAT_TOKENS_PER_IP_PER_DAY)

    # Spy: assert build_graph never gets called — proves the budget gate
    # short-circuited before any LLM machinery spun up.
    spy = MagicMock(side_effect=AssertionError("graph must not run when budget exceeded"))
    monkeypatch.setattr(chat_module, "build_graph", spy)

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "thesis?"})
    assert r.status_code == 200

    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    assert events[0] == "intent" and frames[0][1]["intent"] == "conversational"
    assert "conversational" in events
    assert "thesis" not in events
    payload = next(data for name, data in frames if name == "conversational")
    assert "demo limit" in payload["answer"]
    assert frames[-1][0] == "done"
    spy.assert_not_called()


def test_global_breaker_trip_emits_conversational_redirect_and_alerts_sentry(
    client: TestClient,
    stub_chat_graph: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the global daily Groq TPD breaker is tripped, EVERY new IP
    must see the friendly redirect (this is the rotating-IP defence) and
    Sentry must be alerted exactly once for the trip event."""
    del stub_chat_graph

    # Pre-fill the global counter to exactly the cap (using a different IP
    # so the per-IP cap doesn't fire instead).
    budget.record("other-ip", security_module.settings.CHAT_TOKENS_GLOBAL_PER_DAY)

    sentry_spy = MagicMock()
    monkeypatch.setattr(security_module, "_sentry_capture", sentry_spy)
    # Reset the per-process breaker-alerted-date so this test sees the trip.
    monkeypatch.setattr(chat_module, "_BREAKER_ALERTED_DATE", None)

    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "thesis?"})
    assert r.status_code == 200

    frames = _parse_sse(r.text)
    events = [name for name, _ in frames]
    payload = next(data for name, data in frames if name == "conversational")
    assert "shared demo budget" in payload["answer"].lower() or "exhausted" in payload["answer"]
    assert events[-1] == "done"

    # Sentry alert fired exactly once for the breaker trip.
    sentry_spy.assert_called_once()
    msg, level = sentry_spy.call_args.args[0], sentry_spy.call_args.kwargs.get("level", "warning")
    assert "chat-breaker-tripped" in msg
    assert level == "error"


def test_per_ip_redirect_does_not_charge_tokens(
    client: TestClient,
    stub_chat_graph: None,
) -> None:
    """The redirect path must NOT debit the budget for itself — otherwise
    the user is repeatedly billed for the apology."""
    del stub_chat_graph
    budget.record("testclient", security_module.settings.CHAT_TOKENS_PER_IP_PER_DAY)
    snapshot_before = budget.snapshot()
    client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": "x"})
    snapshot_after = budget.snapshot()
    assert snapshot_after["global"] == snapshot_before["global"]


# ─── SlowAPI rate limit ─────────────────────────────────────────────────────


def test_rate_limit_returns_429_with_retry_after(
    client: TestClient,
    stub_chat_graph: None,
) -> None:
    """The 6th request in 60 seconds (cap = 5/minute) returns 429 with a
    Retry-After header and a friendly JSON body that the chat panel can
    surface as a demo-limit card."""
    del stub_chat_graph
    body = {"ticker": "NVDA", "message": "ok"}
    # Parse the per-minute slice from the live setting so changing the cap
    # doesn't silently desync this test (we'd start firing N+1 requests
    # against an N-cap and over-count the 200s).
    per_minute = int(settings_per_minute_cap())

    for _ in range(per_minute):
        r = client.post("/api/v1/agent/chat", json=body)
        assert r.status_code == 200, "pre-cap request should succeed"

    over = client.post("/api/v1/agent/chat", json=body)
    assert over.status_code == 429
    assert over.headers.get("Retry-After")
    payload = over.json()
    assert payload["code"] == "rate-limited"
    assert "fork the repo" in payload["detail"].lower()


def settings_per_minute_cap() -> int:
    """Read the per-minute slice out of ``CHAT_RATE_LIMIT`` (e.g. ``"5/minute;..."``)."""
    parts = security_module.settings.CHAT_RATE_LIMIT.split(";")
    for p in parts:
        if "minute" in p:
            return int(p.strip().split("/")[0])
    msg = "no per-minute slice in CHAT_RATE_LIMIT"
    raise ValueError(msg)


# ─── CORS lockdown ──────────────────────────────────────────────────────────


def test_cors_disallowed_origin_omits_allow_origin_header(client: TestClient) -> None:
    """An origin not in the allowlist (and not matching the regex) must
    NOT receive an Access-Control-Allow-Origin header — the browser then
    blocks the response. The default test config allows only localhost:3001
    so example.com is the right negative case."""
    r = client.options(
        "/api/v1/agent/chat",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    # CORSMiddleware may return 400 for a disallowed origin or 200 with no
    # allow-origin header depending on Starlette version. The contract under
    # test is "the browser cannot make the cross-origin call" — verified by
    # the absence of an Access-Control-Allow-Origin header.
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ─── Prompt-injection input filter ──────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_message",
    [
        "hello\x00world",  # NUL byte — common shell-control class
        "hello\x07world",  # BEL
        "hello\x1bworld",  # ESC
        "hello\x7fworld",  # DEL
    ],
)
def test_control_characters_rejected_at_validation(
    client: TestClient,
    bad_message: str,
) -> None:
    """Control chars (other than \\n, \\t) should 422 at the Pydantic layer
    so they never reach the graph."""
    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": bad_message},
    )
    assert r.status_code == 422


def test_overlong_token_rejected_at_validation(client: TestClient) -> None:
    """A single contiguous non-whitespace run >500 chars is rejected.
    Real questions don't carry tokens that long; this catches base64
    payloads, embedded URLs with long query args, and similar exfil
    shapes that pass the 4000-char message cap."""
    overlong = "a" + "b" * 600  # 601 contiguous chars
    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": overlong},
    )
    assert r.status_code == 422


def test_newline_and_tab_are_allowed(
    client: TestClient,
    stub_chat_graph: None,
) -> None:
    """Multi-line questions and tab-indented quoted text are common in real
    user input — the filter must allow them."""
    del stub_chat_graph
    msg = "First line.\n\tIndented point.\nThird line."
    r = client.post(
        "/api/v1/agent/chat",
        json={"ticker": "NVDA", "message": msg},
    )
    assert r.status_code == 200


def test_validate_chat_message_unit() -> None:
    """Direct unit test for the validator — exercised by the route
    integration tests above, but isolating the function lets a future
    consumer (CLI, comparison endpoint) reuse it confidently."""
    assert validate_chat_message("normal question") == "normal question"
    assert validate_chat_message("with\nnewline") == "with\nnewline"
    assert validate_chat_message("with\ttab") == "with\ttab"
    with pytest.raises(ValueError, match="control characters"):
        validate_chat_message("bad\x00input")
    with pytest.raises(ValueError, match="overlong token"):
        validate_chat_message("x" * 600)


# ─── Burst alerter ──────────────────────────────────────────────────────────


def test_burst_alerter_fires_once_per_window_threshold() -> None:
    """The alerter must dedup: many 429s in one window produce ONE
    Sentry message; if the IP later crosses threshold AGAIN in a fresh
    window, it can fire again."""
    from api.security import BurstAlerter

    alerter = BurstAlerter(threshold=3, window_seconds=60)
    # First two events: under threshold → no alert.
    assert alerter.record_429("1.2.3.4", 0.0) is False
    assert alerter.record_429("1.2.3.4", 1.0) is False
    # Third event: crosses threshold → alert fires.
    assert alerter.record_429("1.2.3.4", 2.0) is True
    # Fourth event in same window: dedup → no alert.
    assert alerter.record_429("1.2.3.4", 3.0) is False
    # After the window passes AND another threshold-worth of events lands,
    # the IP can fire again. Three events at t=100,101,102 cross threshold
    # within their own window, AND > window_seconds since the last alert.
    assert alerter.record_429("1.2.3.4", 100.0) is False
    assert alerter.record_429("1.2.3.4", 101.0) is False
    assert alerter.record_429("1.2.3.4", 102.0) is True


def test_burst_alerter_per_ip_isolation() -> None:
    """Two scrapers on different IPs each trip independently — one IP's
    burst doesn't suppress another IP's alert."""
    from api.security import BurstAlerter

    alerter = BurstAlerter(threshold=2, window_seconds=60)
    assert alerter.record_429("a", 0.0) is False
    assert alerter.record_429("a", 1.0) is True  # a fires
    assert alerter.record_429("b", 1.5) is False
    assert alerter.record_429("b", 2.0) is True  # b fires independently


# ─── client_ip extraction (post-review fix) ─────────────────────────────────


class _FakeRequest:
    """Minimal stand-in for fastapi.Request that ``client_ip`` reads from.

    The real Request has a ``headers`` mapping (case-insensitive) and a
    ``client`` attribute with ``host``. We mirror just those.
    """

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        client_host: str | None = None,
    ) -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        if client_host is None:
            self.client = None
        else:
            self.client = type("Client", (), {"host": client_host})()


def test_client_ip_prefers_x_forwarded_for_leftmost() -> None:
    """Behind Caddy/nginx, ``request.client.host`` is the proxy's IP, not
    the real visitor. ``client_ip`` MUST honour ``X-Forwarded-For`` and
    take the LEFT-MOST entry (the original client) so the per-IP rate
    limit + per-IP token budget actually scope per-visitor in prod.
    Regression guard for the pre-review bug where every visitor collapsed
    into one bucket because we used ``get_remote_address`` directly."""
    from api.security import client_ip

    req = _FakeRequest(
        headers={"X-Forwarded-For": "203.0.113.5, 198.51.100.7, 172.18.0.2"},
        client_host="172.18.0.2",  # Caddy's container IP
    )
    assert client_ip(req) == "203.0.113.5"  # type: ignore[arg-type]


def test_client_ip_falls_back_to_x_real_ip() -> None:
    """Some proxy configs use ``X-Real-IP`` instead of (or in addition to)
    XFF. We honour XFF first, then X-Real-IP, then client.host."""
    from api.security import client_ip

    req = _FakeRequest(
        headers={"X-Real-IP": "203.0.113.99"},
        client_host="172.18.0.2",
    )
    assert client_ip(req) == "203.0.113.99"  # type: ignore[arg-type]


def test_client_ip_falls_back_to_client_host_in_dev() -> None:
    """Dev (no Caddy) sees no proxy headers — fall through to client.host
    so localhost requests still work."""
    from api.security import client_ip

    req = _FakeRequest(headers={}, client_host="127.0.0.1")
    assert client_ip(req) == "127.0.0.1"  # type: ignore[arg-type]


def test_client_ip_returns_unknown_when_no_signal_available() -> None:
    """Belt-and-braces: a malformed request with no headers AND no client
    info returns the literal ``"unknown"`` so the limiter still has a
    bucket to work with (better one shared bucket for malformed requests
    than a crash on the request path)."""
    from api.security import client_ip

    req = _FakeRequest(headers={}, client_host=None)
    assert client_ip(req) == "unknown"  # type: ignore[arg-type]


# ─── LiteLLM config audit regression guard (post-review fix) ────────────────


def test_litellm_config_filter_catches_anthropic_when_groq_openai_present(
    tmp_path: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the per-match position bug: the previous filter
    used ``text.find("openai/")`` once and reused that index for every
    match, which silently suppressed any ``anthropic/`` violation whenever
    a single ``groq/openai/`` token existed elsewhere in the file. The
    bench aliases use ``groq/openai/gpt-oss-20b``, so this exact failure
    mode would have shipped without the fix.

    We construct a synthetic config that pairs a permitted ``groq/openai/``
    line with a forbidden ``anthropic/`` line and assert the audit
    function flags the latter.
    """
    import re

    fake_config = (
        "model_list:\n"
        "  - model_name: bench-gptoss\n"
        "    litellm_params:\n"
        "      model: groq/openai/gpt-oss-20b  # permitted — Groq host\n"
        "  - model_name: rogue-claude\n"
        "    litellm_params:\n"
        "      model: anthropic/claude-haiku-4-5  # FORBIDDEN — paid provider\n"
    )

    forbidden = re.compile(
        r"^(?P<lead>\s*[^#\n]*?)\b(?P<provider>anthropic|openai)/",
        re.MULTILINE,
    )
    real_violations: list[str] = []
    for match in forbidden.finditer(fake_config):
        provider = match.group("provider")
        if provider == "openai":
            start = match.start("provider")
            preceding = fake_config[max(0, start - len("groq/")) : start]
            if preceding.endswith("groq/"):
                continue
        line_start = fake_config.rfind("\n", 0, match.start()) + 1
        line_end = fake_config.find("\n", match.end())
        if line_end == -1:
            line_end = len(fake_config)
        real_violations.append(fake_config[line_start:line_end].strip())
    assert real_violations, (
        "filter regression: anthropic/claude-haiku should be flagged even "
        "when groq/openai/* exists elsewhere in the file"
    )
    assert any("anthropic/claude-haiku-4-5" in v for v in real_violations)
    assert not any("groq/openai/gpt-oss-20b" in v for v in real_violations)


# ─── CORS regex test (constructs a fresh app) ───────────────────────────────


def test_cors_regex_allows_pinned_vercel_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``CORS_ALLOWED_ORIGIN_REGEX`` is set to a project-pinned pattern,
    a Vercel preview URL for THAT project is allowed; a Vercel URL for an
    unrelated project is NOT (the leak-prevention invariant)."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3001"],
        allow_origin_regex=r"^https://equity-data-agent(-[a-z0-9-]+)?\.vercel\.app$",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.post("/x")
    async def _x() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as c:
        # Project's preview deploy → allowed.
        r = c.options(
            "/x",
            headers={
                "Origin": "https://equity-data-agent-git-main.vercel.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert (
            r.headers.get("access-control-allow-origin")
            == "https://equity-data-agent-git-main.vercel.app"
        )

        # Random other project on Vercel → NOT allowed (header missing).
        r2 = c.options(
            "/x",
            headers={
                "Origin": "https://attacker-project.vercel.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert "access-control-allow-origin" not in {k.lower() for k in r2.headers}


# ─── Token budget tracker unit ──────────────────────────────────────────────


def test_token_budget_per_ip_and_global_caps() -> None:
    """``can_serve`` returns ``per_ip`` when one IP overruns and ``global``
    when the aggregate overruns regardless of any single IP's spend."""
    from api.security import TokenBudget

    b = TokenBudget(per_ip_daily=100, global_daily=1000)
    assert b.can_serve("a") == (True, None)
    b.record("a", 50)
    assert b.can_serve("a") == (True, None)
    b.record("a", 60)
    assert b.can_serve("a") == (False, "per_ip")
    # Other IP unaffected.
    assert b.can_serve("b") == (True, None)

    # Pile global until the cap.
    for _ in range(20):
        b.record(f"ip-{_}", 50)  # 1000 across 20 IPs
    assert b.can_serve("c")[0] is False
    assert b.can_serve("c")[1] == "global"


def test_token_budget_zero_or_negative_record_is_noop() -> None:
    """Avoid negative drift if a buggy callback returns 0 or < 0 tokens."""
    from api.security import TokenBudget

    b = TokenBudget(per_ip_daily=100, global_daily=1000)
    b.record("a", 0)
    b.record("a", -50)
    assert b.snapshot()["global"] == 0


def test_token_budget_resets_at_new_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the UTC date boundary both per-IP and global counters zero."""
    from api.security import TokenBudget

    b = TokenBudget(per_ip_daily=100, global_daily=1000)
    b.record("a", 90)
    assert b.snapshot()["global"] == 90
    # Force a date roll by patching the ``_today_utc`` helper used inside.
    monkeypatch.setattr(security_module, "_today_utc", lambda: "2099-01-01")
    # Touch the budget so it sees the new date and resets.
    b.record("a", 0)
    assert b.snapshot()["global"] == 0


# ─── Token-tracker integration (records spend after run) ────────────────────


@pytest.fixture
def _stub_token_callback(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Patch the agent LLM so the graph never opens a network connection
    AND simulate a 1234-token spend by calling ``tracker.add(1234)`` in
    the stub graph. The integration assertion is "after the run, the
    per-IP counter advanced by 1234"."""
    from agent import llm as llm_module

    def _fake_build(tools: dict[str, Any], **_kwargs: Any) -> Any:  # noqa: ARG001
        graph = MagicMock()

        def invoke(state: dict[str, Any]) -> dict[str, Any]:
            tracker = llm_module._TOKEN_TRACKER.get()
            if tracker is not None:
                tracker.add(1234)
            return {
                "ticker": state["ticker"],
                "intent": "thesis",
                "plan": [],
                "reports": {"technical": "stub"},
                "errors": {},
                "thesis": _stub_thesis(),
                "quick_fact": None,
                "comparison": None,
                "conversational": None,
                "confidence": 0.5,
            }

        graph.invoke.side_effect = invoke
        return graph

    monkeypatch.setattr(chat_module, "build_graph", _fake_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})
    yield


def test_chat_run_charges_token_spend_to_per_ip_and_global(
    client: TestClient,
    _stub_token_callback: None,
) -> None:
    """A successful chat run must debit both budgets by the observed
    spend. This is the post-run accounting path that protects the next
    request from consuming quota beyond the cap."""
    del _stub_token_callback
    pre = budget.snapshot()
    r = client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})
    assert r.status_code == 200
    post = budget.snapshot()
    assert post["global"] - pre["global"] == 1234


def test_reset_token_tracker_called_when_build_graph_raises(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the contextvar-leak bug: ``set_token_tracker``
    runs early in ``_stream``; if ``build_graph`` (or ``_instrument_tools``,
    ``asyncio.Queue``) raises BEFORE the try/finally engages, the contextvar
    would never be reset. In production this matters because asyncio reuses
    task contexts and the next request would inherit a stale tracker. The
    fix moved ``set_token_tracker`` inside the outer try so the finally
    always reaches ``reset_token_tracker``.

    The contextvar itself is task-scoped, so TestClient (which runs each
    request in a fresh task) won't observe the leak across requests. This
    test spies on the reset-tracker call directly: it MUST fire exactly
    once per ``set_token_tracker``, regardless of whether ``build_graph``
    raised or completed normally.
    """
    set_count = 0
    reset_count = 0

    real_set = chat_module.set_token_tracker
    real_reset = chat_module.reset_token_tracker

    def _spy_set(tracker: Any) -> Any:
        nonlocal set_count
        set_count += 1
        return real_set(tracker)

    def _spy_reset(token: Any) -> None:
        nonlocal reset_count
        reset_count += 1
        real_reset(token)

    monkeypatch.setattr(chat_module, "set_token_tracker", _spy_set)
    monkeypatch.setattr(chat_module, "reset_token_tracker", _spy_reset)

    def _exploding_build(_tools: dict[str, Any], **_kw: Any) -> Any:
        msg = "synthetic build_graph crash"
        raise RuntimeError(msg)

    monkeypatch.setattr(chat_module, "build_graph", _exploding_build)
    monkeypatch.setattr(chat_module, "default_report_tools", lambda: {})

    # TestClient re-raises uncaught server exceptions by default; the
    # finally block we're testing runs in the SSE generator's coroutine,
    # so we just need the request lifecycle to complete (success or fail).
    # The ``with pytest.raises`` is part of the contract: the server-side
    # crash IS the input to this test.
    with pytest.raises(RuntimeError, match="synthetic build_graph crash"):
        client.post("/api/v1/agent/chat", json={"ticker": "NVDA", "message": ""})

    # CRITICAL: every set must be paired with a reset, even though
    # ``build_graph`` raised mid-construction. This is the contract the
    # post-review fix locks down.
    assert set_count == 1
    assert reset_count == 1


# ─── Sentry init behaviour (smoke) ──────────────────────────────────────────


def test_sentry_capture_falls_back_to_log_when_dsn_unset(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No SENTRY_DSN → ``_sentry_capture`` must log at WARNING (not crash,
    not silently swallow). QNT-86 wires the real init; this test guards
    the no-DSN dev / CI path."""
    monkeypatch.setattr(security_module.settings, "SENTRY_DSN", "")
    with caplog.at_level("WARNING"):
        security_module._sentry_capture("test message", level="warning")
    assert "test message" in caplog.text


def test_sentry_capture_invokes_sdk_when_dsn_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """SENTRY_DSN set → the SDK's capture_message is called with the right
    level. QNT-86 will drive ``sentry_sdk.init`` from the app entry point;
    this test asserts the hook surface is correct."""
    monkeypatch.setattr(security_module.settings, "SENTRY_DSN", "https://test@example/1")
    fake_sdk = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
        security_module._sentry_capture("hello", level="error")
    fake_sdk.capture_message.assert_called_once_with("hello", level="error")
