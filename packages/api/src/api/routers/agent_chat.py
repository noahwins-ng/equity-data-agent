"""Agent chat SSE endpoint — ``POST /api/v1/agent/chat`` (QNT-74, QNT-149, QNT-156).

The endpoint streams Server-Sent Events while a LangGraph run executes against
the requested ticker. The frontend right-rail chat panel consumes the same
contract; the CLI / eval harness can hit it too (no SDK lock-in — see ADR-008).

Event contract
--------------

``tool_call``     — ``{name, label, args, started_at}`` emitted as the agent
                    invokes a report tool. ``label`` is the human-readable
                    string the panel surfaces ("Reading price history") so the
                    UI never has to map function names itself.

``tool_result``   — ``{name, label, latency_ms, summary}`` emitted when the
                    tool returns. ``summary`` is a short real string derived
                    from the report body (e.g. ``"38 lines"`` for technical)
                    so the row can render without further parsing.

``prose_chunk``   — ``{delta}`` markdown deltas for the agent prose surface.
                    For now the prose is the structured thesis ``setup``
                    paragraph chunked by clause; a future revision can splice
                    explicit narrative output from the synthesize node.

``thesis``        — full :class:`~agent.thesis.Thesis` model dumped to JSON.
                    Renders the Setup / Bull / Bear / Verdict card. Emitted
                    only when intent == "thesis".

``quick_fact``    — full :class:`~agent.quick_fact.QuickFactAnswer` model
                    dumped to JSON (QNT-149). Emitted only when
                    intent == "quick_fact"; the panel renders a compact
                    answer + cited value chip and skips the thesis card.

``comparison``    — full :class:`~agent.comparison.ComparisonAnswer` model
                    dumped to JSON (QNT-156). Emitted only when
                    intent == "comparison"; the panel renders a side-by-side
                    card and skips the thesis card.

``conversational``— full :class:`~agent.conversational.ConversationalAnswer`
                    dumped to JSON (QNT-156). Emitted when intent ==
                    "conversational" OR when ANY synthesize path falls back
                    to a deterministic domain redirect. The panel renders a
                    short prose answer + suggestion list.

``focused``       — full :class:`~agent.focused.FocusedAnalysis` dumped to
                    JSON (QNT-176). Emitted only when intent ∈
                    {"fundamental", "technical", "news_sentiment"}; the
                    panel renders a focused-analysis card and skips the
                    thesis card.

``intent``        — ``{intent}`` one-shot event emitted right after the
                    classify node decides which shape will be produced
                    (QNT-149). Lets the panel preempt its layout while
                    tools are still running.

``done``          — ``{tools_count, citations_count, confidence}``;
                    ``intent`` is added once the classify node has run.
                    Pre-classify failures (unknown ticker, agent crash before
                    routing, run-budget timeout) emit ``done`` without
                    ``intent`` — matches the ``intent?: Intent`` optional in
                    the TypeScript ``DoneEvent``. ``citations_count`` is the
                    number of inline ``(source: …)`` cites the structured
                    thesis carries. Per-tool errors are surfaced as separate
                    ``error`` events earlier in the stream, not bundled into
                    ``done``.

``error``         — ``{detail, code}`` terminal failure event. ``detail`` is a
                    stable user-facing string; raw exception details are
                    logged server-side only so internal LiteLLM auth errors,
                    URLs, and stack context don't leak to the SSE client.
                    Frontend should surface ``detail`` and stop reading.

The graph itself is synchronous (Python LangGraph, sync ``invoke``). To stream
incrementally we wrap each tool with an instrumented adapter that posts
events to an :class:`asyncio.Queue` from a worker thread, run the graph in
``asyncio.to_thread``, and yield queued events to the SSE client as they
arrive. After the graph completes, the post-run events (``prose_chunk``,
``thesis``, ``done``) are emitted from the main coroutine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

from agent.comparison import ComparisonAnswer
from agent.conversational import ConversationalAnswer, domain_redirect
from agent.focused import FocusedAnalysis
from agent.graph import OPTIONAL_TOOLS, build_graph
from agent.llm import (
    TokenUsageTracker,
    reset_token_tracker,
    set_token_tracker,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tools import default_report_tools
from agent.tracing import observe
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from shared.config import settings
from shared.tickers import TICKERS

from api.security import (
    budget,
    client_ip,
    limiter,
    record_breaker_trip,
    sentry_capture_exception,
    validate_chat_message,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


# ─── Public request shape ───────────────────────────────────────────────────

# Cap user-supplied prompt length defensively. The graph passes ``message``
# verbatim into the synthesis prompt; a 100k-char prompt would burn quota and
# wreck output quality. 4000 is generous for a research question.
_MESSAGE_MAX_LEN = 4000


class ChatRequest(BaseModel):
    """POST body for ``/api/v1/agent/chat``."""

    ticker: str = Field(min_length=1, max_length=8)
    message: str = Field(default="", max_length=_MESSAGE_MAX_LEN)

    @field_validator("message")
    @classmethod
    def _filter_message(cls, v: str) -> str:
        """QNT-161: reject control chars + overlong tokens BEFORE the prompt
        ever reaches the LLM. The 4000-char Field cap handles bulk; this
        catches the narrow exfil-shaped inputs (zero-width joins, base64
        blobs, embedded shell control sequences) that pass length but
        shouldn't be rendered into the synthesize prompt verbatim."""
        return validate_chat_message(v)


# ─── Tool labels (human-friendly names for the UI) ──────────────────────────

# Canonical mapping — the UI never sees raw function names. Keep aligned with
# ``agent.prompts.REPORT_TOOLS`` (sweep would surface a missing entry).
_TOOL_LABELS: dict[str, str] = {
    "company": "Reviewing company",
    "technical": "Reading technicals",
    "fundamental": "Checking fundamentals",
    "news": "Scanning news",
}


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, name)


def _summarise_report(name: str, body: str) -> str:
    """Short, real summary for the ``tool_result`` row.

    Uses observed structural cues per report kind so the count matches what a
    reader of the report would notice (line count, headline count). The
    fallback is ``"<N> lines"`` so a new report kind never breaks the UI.
    """
    if body.startswith("[error]"):
        # Error string from agent.tools — surface verbatim so the tool row
        # can show the failure mode rather than a fake "0 lines".
        first_line = body.splitlines()[0] if body else "[error]"
        return first_line[:120]

    line_count = sum(1 for line in body.splitlines() if line.strip())
    if name == "news":
        # News report bullets each begin with "- ". Counting them is a real
        # surface that maps to "N headlines".
        headline_count = sum(1 for line in body.splitlines() if line.startswith("- "))
        if headline_count:
            return f"{headline_count} headlines"
    return f"{line_count} lines"


# ─── SSE helpers ────────────────────────────────────────────────────────────


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one SSE frame. Each frame is ``event: <name>\\ndata: <json>\\n\\n``."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


# ─── Citation parsing ───────────────────────────────────────────────────────

# Inline citations the synthesis prompt produces look like ``(source: technical)``,
# ``(source: technical|fundamental)``, or ``(Publisher, Date)`` for news.
# We count ``(source: …)`` as the canonical "cited claim" because the prompt
# enforces that shape across the four sections.
_CITATION_PATTERN = re.compile(r"\(source:\s*[A-Za-z|\s]+\)")


def _count_citations(thesis: Thesis | None) -> int:
    if thesis is None:
        return 0
    fields = [thesis.setup, thesis.verdict_action, *thesis.bull_case, *thesis.bear_case]
    return sum(len(_CITATION_PATTERN.findall(text or "")) for text in fields)


def _count_quick_fact_citations(quick_fact: QuickFactAnswer | None) -> int:
    """Citations for the quick-fact path. The structured ``source`` field
    counts as one citation when populated; we also pick up any inline
    ``(source: …)`` parens in the prose answer so the count matches what
    the panel renders.
    """
    if quick_fact is None:
        return 0
    inline = len(_CITATION_PATTERN.findall(quick_fact.answer or ""))
    structured = 1 if quick_fact.source and quick_fact.cited_value else 0
    # Avoid double-counting when the prose already cites the same source —
    # if there's any inline citation we trust that count over the structured
    # one (the chip renders from the inline match).
    return inline if inline else structured


def _count_comparison_citations(comparison: ComparisonAnswer | None) -> int:
    """Citations for the comparison path.

    Each ``ComparisonValue`` counts as one citation (the structured
    ``source`` field is required), plus any inline ``(source: …)`` parens
    in the per-section summaries or the differences paragraph.
    """
    if comparison is None:
        return 0
    structured = sum(len(s.key_values) for s in comparison.sections)
    inline_texts = [s.summary for s in comparison.sections] + [comparison.differences]
    inline = sum(len(_CITATION_PATTERN.findall(text or "")) for text in inline_texts)
    return structured + inline


def _count_focused_citations(focused: FocusedAnalysis | None) -> int:
    """Citations for the focused-analysis path (QNT-176).

    Each ``FocusedValue`` carries a structured source; each inline
    ``(source: …)`` parens in the summary or key_points adds one. Mirrors
    :func:`_count_comparison_citations` so the panel's "N cited" badge
    counts the same surface across all card shapes.
    """
    if focused is None:
        return 0
    structured = len(focused.cited_values)
    inline_texts = [focused.summary, *focused.key_points]
    inline = sum(len(_CITATION_PATTERN.findall(text or "")) for text in inline_texts)
    return structured + inline


# ─── Prose chunking ─────────────────────────────────────────────────────────

# Split the ``setup`` paragraph into clause-sized chunks so the panel can
# render it progressively. A real token-stream would emit one chunk per LLM
# token; the structured output runnable doesn't expose that, so clause-level
# is the next-best granularity. Punctuation is kept on the chunk that ended
# the clause.
_PROSE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_prose(text: str) -> list[str]:
    if not text:
        return []
    chunks = [c.strip() for c in _PROSE_SPLIT.split(text) if c.strip()]
    return chunks or [text.strip()]


# ─── Instrumented tool wrappers ─────────────────────────────────────────────


def _instrument_tools(
    tools: dict[str, Any],
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    loop: asyncio.AbstractEventLoop,
    ticker: str,
) -> dict[str, Any]:
    """Return a tool map that emits ``tool_call`` / ``tool_result`` events.

    Each wrapped tool: posts a ``tool_call`` event before executing, runs the
    real tool, posts a ``tool_result`` with measured latency + a real
    ``summary``. Posting from the worker thread back onto the asyncio queue
    uses ``loop.call_soon_threadsafe`` because the queue lives on the main
    event loop.
    """

    def _post(event: str, data: dict[str, Any]) -> None:
        # ``put_nowait`` cannot be called from a thread without
        # ``call_soon_threadsafe``; the queue is unbounded so the put never
        # blocks once it lands on the loop.
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    wrapped: dict[str, Any] = {}
    for name, fn in tools.items():

        def make_wrapper(name: str, fn: Any) -> Any:
            def wrapper(t: str) -> str:
                started_at = time.time()
                _post(
                    "tool_call",
                    {
                        "name": name,
                        "label": _tool_label(name),
                        "args": {"ticker": t},
                        "started_at": started_at,
                    },
                )
                try:
                    result = fn(t)
                except Exception as exc:  # noqa: BLE001 — record + re-raise
                    latency_ms = int((time.time() - started_at) * 1000)
                    _post(
                        "tool_result",
                        {
                            "name": name,
                            "label": _tool_label(name),
                            "latency_ms": latency_ms,
                            "summary": f"[error] {type(exc).__name__}: {exc}"[:120],
                            "ok": False,
                        },
                    )
                    raise
                latency_ms = int((time.time() - started_at) * 1000)
                _post(
                    "tool_result",
                    {
                        "name": name,
                        "label": _tool_label(name),
                        "latency_ms": latency_ms,
                        "summary": _summarise_report(name, result),
                        "ok": not result.startswith("[error]"),
                    },
                )
                return result

            return wrapper

        wrapped[name] = make_wrapper(name, fn)
        # Tag the wrapper with the original ticker so we can sanity-check
        # the inner call in tests if needed.
        wrapped[name].__wrapped_ticker__ = ticker  # type: ignore[attr-defined]
    return wrapped


# ─── Streaming generator ────────────────────────────────────────────────────


_DONE_FAILURE_PAYLOAD = {
    "tools_count": 0,
    "citations_count": 0,
    "confidence": 0.0,
}


# QNT-150: stable user-facing strings for SSE error events. Raw exception
# detail is logged server-side; the client only ever sees these.
_ERROR_DETAIL_AGENT_FAILED = "Agent run failed. Try again or rephrase."
_ERROR_DETAIL_AGENT_TIMEOUT = "Agent run timed out. Try again in a moment."

# QNT-161: friendly redirect copy when the per-IP daily token budget OR the
# global daily Groq TPD breaker has been exhausted. The conversational card
# renders this prose + the same suggestion list ``domain_redirect`` produces,
# so the panel surface is identical to "I don't know what you asked" — no
# scary error text, no exposed internals. Each reason is digit-free so the
# QNT-156 ``has_numeric_claims`` guardrail accepts it.
_BUDGET_REDIRECT_PER_IP = (
    "You've hit today's per-visitor demo limit. This portfolio site runs on a "
    "free LLM tier; the cap protects daily uptime for other visitors. Try "
    "again tomorrow, or fork the repo to run the agent against your own keys."
)
_BUDGET_REDIRECT_GLOBAL = (
    "Today's shared demo budget is exhausted. This portfolio site runs on a "
    "free LLM tier with a hard daily ceiling; the cap resets overnight. Try "
    "again tomorrow, or fork the repo to run the agent against your own keys."
)

# QNT-161: track whether we've fired the breaker-trip Sentry alert for the
# current UTC day so a stream of post-trip requests doesn't produce a stream
# of duplicate Sentry events. Reset semantics: the value is the date string
# the trip was alerted for; a new day naturally re-arms the alert.
_BREAKER_ALERTED_DATE: str | None = None
_BREAKER_LOCK = asyncio.Lock()


async def _maybe_alert_breaker_once() -> None:
    """Fire ``record_breaker_trip`` at most once per UTC day.

    Called from the global-budget redirect path. The dedup is per-process —
    on a multi-worker uvicorn each worker may fire once, which is fine: the
    Sentry payload carries enough context (timestamp, server hostname) to
    coalesce visually.
    """
    global _BREAKER_ALERTED_DATE
    from datetime import datetime

    today = datetime.now(UTC).date().isoformat()
    async with _BREAKER_LOCK:
        if _BREAKER_ALERTED_DATE == today:
            return
        _BREAKER_ALERTED_DATE = today
    record_breaker_trip("global TPD ceiling reached")


async def _stream(request: ChatRequest, client_ip: str) -> AsyncIterator[str]:
    """Yield SSE frames for one chat request.

    Validation failures yield a single ``error`` event followed by ``done``;
    success yields the canonical ``tool_call`` → ``tool_result`` → ``prose_chunk``
    → ``thesis`` → ``done`` sequence.

    QNT-161: ``client_ip`` drives the per-IP daily token budget. Both the
    per-IP and global TPD breakers are checked BEFORE the graph runs; on
    exhaustion we emit a deterministic conversational redirect (no graph
    invocation, no LLM cost) and the panel renders the same redirect card it
    uses for off-domain questions. The agent never reaches a paid provider
    in either branch — see ADR-017.
    """
    ticker = request.ticker.upper()
    if ticker not in TICKERS:
        yield _sse(
            "error",
            {"detail": f"Unknown ticker: {ticker}", "code": "unknown-ticker"},
        )
        yield _sse("done", _DONE_FAILURE_PAYLOAD)
        return

    # QNT-161: budget gate. Cheap O(1) check before we instantiate any
    # graph machinery; on exhaustion the client gets the friendly redirect
    # card instead of an HTTP error. ``conversational`` shape keeps the
    # event sequence identical to a normal off-domain run, so the panel
    # never branches on a special "budget" state.
    allowed, reason = budget.can_serve(client_ip)
    if not allowed:
        if reason == "global":
            await _maybe_alert_breaker_once()
        redirect_reason = _BUDGET_REDIRECT_GLOBAL if reason == "global" else _BUDGET_REDIRECT_PER_IP
        fallback = domain_redirect(
            reason=redirect_reason,
            tickers=TICKERS,
            hint="thesis",
        )
        yield _sse("intent", {"intent": "conversational"})
        for chunk in _split_prose(fallback.answer):
            yield _sse("prose_chunk", {"delta": chunk + " "})
            await asyncio.sleep(0)
        yield _sse("conversational", fallback.model_dump())
        yield _sse(
            "done",
            {
                "tools_count": 0,
                "citations_count": 0,
                "confidence": 0.0,
                "intent": "conversational",
            },
        )
        return

    # QNT-161: bind a per-request token tracker so every LLM call inside the
    # graph (classify / plan / synthesize) charges into the same accumulator.
    # Read once at the end and debit the per-IP + global budgets in one shot.
    # contextvars propagates through ``asyncio.to_thread`` automatically, so
    # the worker thread sees the same tracker.
    #
    # The contextvar must be reset on EVERY exit path — including failures
    # in graph construction (build_graph / _instrument_tools / asyncio.Queue
    # creation). Set it inside the try so the outer finally always reaches
    # ``reset_token_tracker``; otherwise a build_graph crash leaves a stale
    # tracker bound to the request task and the next request inherits it.
    tracker = TokenUsageTracker()
    tracker_token = set_token_tracker(tracker)
    runner_task: asyncio.Task[Any] | None = None
    final_state_holder: dict[str, Any] = {}
    timed_out = False
    spent = 0
    try:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        # QNT-159: hand the graph an event emitter so the classify node can post
        # the ``intent`` event onto our queue as soon as it resolves — BEFORE the
        # plan/gather LLM calls fire and BEFORE any tool_call event lands. Same
        # mechanism the tool wrappers use (worker thread → loop.call_soon_threadsafe
        # → asyncio queue → drained by the SSE generator). Without this, the
        # streaming label said "streaming thesis…" for the entire tool-gathering
        # phase regardless of which intent the classifier actually picked.
        def _emit(event: str, data: dict[str, object]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (event, dict(data)))

        base_tools = default_report_tools()
        instrumented = _instrument_tools(base_tools, queue, loop, ticker)
        graph = build_graph(instrumented, event_emitter=_emit)

        @observe(name="agent-chat")
        def _runner() -> None:
            # Run the graph in a worker thread; emitted events have already been
            # routed onto the queue via ``call_soon_threadsafe``. The final state
            # is captured for post-run prose / thesis / done events.
            #
            # ``@observe(name="agent-chat")`` opens a parent observation so the
            # four node-level @observe spans (classify / plan / gather /
            # synthesize) and the per-tool spans nest under one trace in
            # Langfuse — matches the CLI's ``agent.__main__.analyze`` topology.
            # Without this wrapper, langfuse-python's contextvar-based parent
            # tracking can't see a parent and each node opens its own root
            # trace.
            try:
                final_state_holder["state"] = graph.invoke(
                    {"ticker": ticker, "question": request.message}
                )
            except Exception as exc:  # noqa: BLE001 — surfaced as SSE error
                logger.exception("agent graph failed for %s", ticker)
                final_state_holder["error"] = exc

        runner_task = asyncio.create_task(asyncio.to_thread(_runner))
        run_deadline = loop.time() + settings.CHAT_RUN_TIMEOUT

        # QNT-150: wrap the entire drain + post-graph phase in try/finally so a
        # client disconnect (FastAPI raises GeneratorExit into this coroutine)
        # always cancels the runner task and shields its teardown. Without this
        # the worker keeps running, burning LLM quota and a thread-pool slot
        # until the graph naturally finishes. asyncio.to_thread can't actually
        # kill the thread, but cancelling the asyncio task releases the queue
        # and stops further emit callbacks.
        # Drain queue while the graph is running. ``runner_task.done()``
        # flips only after the worker thread completes; until then we await
        # the next queued event with a short timeout so the loop stays
        # responsive. ``run_deadline`` enforces the top-level CHAT_RUN_TIMEOUT
        # budget regardless of whether any single LLM call exceeded its own
        # per-call timeout — protects against retry loops in the proxy.
        while not runner_task.done() or not queue.empty():
            remaining = run_deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=min(0.1, remaining))
            except TimeoutError:
                continue
            yield _sse(event, data)

        if timed_out:
            logger.warning(
                "agent run exceeded CHAT_RUN_TIMEOUT (%.1fs) for %s",
                settings.CHAT_RUN_TIMEOUT,
                ticker,
            )
            # QNT-86: surface timeout patterns in the same Sentry dashboard
            # as graph crashes by capturing a synthetic ``TimeoutError``. The
            # frame attached is just this callsite — the worker-thread stack
            # is gone by the time we get here (asyncio.wait_for abandoned the
            # graph rather than killing it with an exception). That's
            # acceptable: timeouts cluster on the budget breach, not on the
            # internal LLM call that happened to be running, so a single
            # frame keyed by ticker + budget is the right grouping.
            try:
                raise TimeoutError(
                    f"agent run exceeded CHAT_RUN_TIMEOUT "
                    f"({settings.CHAT_RUN_TIMEOUT:.1f}s) for ticker={ticker}"
                )
            except TimeoutError as exc:
                sentry_capture_exception(exc)
            yield _sse(
                "error",
                {"detail": _ERROR_DETAIL_AGENT_TIMEOUT, "code": "agent-timeout"},
            )
            yield _sse("done", _DONE_FAILURE_PAYLOAD)
            return

        if "error" in final_state_holder:
            # Internal exception detail is already logged via ``logger.exception``
            # in ``_runner``. Surface only a stable, user-facing string here so
            # internal LiteLLM auth errors, URLs, and stack context never leak
            # to the SSE client.
            #
            # QNT-86: graph crashes happen in a worker thread (asyncio.to_thread),
            # so the FastAPI integration's auto-capture middleware never sees
            # them. Forward the original exception explicitly — Sentry's
            # fingerprint preserves the in-thread stack so the dashboard shows
            # where the graph actually broke.
            sentry_capture_exception(final_state_holder["error"])
            yield _sse(
                "error",
                {"detail": _ERROR_DETAIL_AGENT_FAILED, "code": "agent-failed"},
            )
            yield _sse("done", _DONE_FAILURE_PAYLOAD)
            return

        state = final_state_holder.get("state") or {}
        thesis = state.get("thesis") if isinstance(state, dict) else None
        quick_fact = state.get("quick_fact") if isinstance(state, dict) else None
        comparison = state.get("comparison") if isinstance(state, dict) else None
        conversational = state.get("conversational") if isinstance(state, dict) else None
        focused = state.get("focused") if isinstance(state, dict) else None
        intent = state.get("intent", "thesis") if isinstance(state, dict) else "thesis"
        confidence = float(state.get("confidence", 0.0)) if isinstance(state, dict) else 0.0
        errors = state.get("errors") or {} if isinstance(state, dict) else {}
        reports = state.get("reports") or {} if isinstance(state, dict) else {}

        # QNT-159: classify_node already streamed the ``intent`` event via the
        # queue-based emitter (so the panel saw it before the first tool_call).
        # This post-graph yield is now an idempotent safety net for two cases:
        # (1) the emitter raised silently and the early frame never made it onto
        # the queue, and (2) stubbed test graphs that bypass classify_node and
        # return a canned final state directly. The panel's ``updateRun(intent)``
        # is idempotent — duplicate frames overwrite ``run.intent`` with the
        # same value, so the cost of leaving this safety net in is one extra
        # SSE frame per real request.
        if isinstance(state, dict) and "intent" in state:
            yield _sse("intent", {"intent": intent})

        # Surface required-tool failures the graph recorded so the panel can
        # indicate gaps (optional tools like ``news`` are silently dropped per
        # OPTIONAL_TOOLS contract — those don't reach this dict). For comparison
        # runs the keys are namespaced ``ticker.tool``; strip the prefix so the
        # tool-name check still works.
        #
        # QNT-150: ``detail`` is graph-recorded and can contain raw upstream
        # error strings (HTTP error bodies, internal URLs) from agent.tools'
        # ``[error] kind: detail`` format. Don't surface that to the SSE
        # client — the panel only needs to know which tool failed. The raw
        # detail is logged at WARNING for server-side debuggability.
        for name, detail in errors.items():
            bare_tool = name.rsplit(".", 1)[-1]
            if bare_tool in OPTIONAL_TOOLS:
                continue
            logger.warning(
                "required tool failure surfaced to SSE client: tool=%s detail=%s",
                bare_tool,
                detail,
            )
            yield _sse(
                "error",
                {
                    "detail": f"{_tool_label(bare_tool)} failed.",
                    "code": "tool-failed",
                },
            )

        # QNT-156: branch on intent. Each shape emits its own structured event;
        # the deterministic conversational redirect (any synthesize-path
        # failure) is also delivered through the conversational event so the
        # panel always renders SOMETHING in-domain.
        if intent == "conversational" and isinstance(conversational, ConversationalAnswer):
            for chunk in _split_prose(conversational.answer):
                yield _sse("prose_chunk", {"delta": chunk + " "})
                await asyncio.sleep(0)
            yield _sse("conversational", conversational.model_dump())
        elif intent == "comparison" and isinstance(comparison, ComparisonAnswer):
            # Stream the differences paragraph as prose so the panel has
            # something to show before the full card lands.
            for chunk in _split_prose(comparison.differences):
                yield _sse("prose_chunk", {"delta": chunk + " "})
                await asyncio.sleep(0)
            yield _sse("comparison", comparison.model_dump())
        elif intent == "quick_fact" and isinstance(quick_fact, QuickFactAnswer):
            for chunk in _split_prose(quick_fact.answer):
                yield _sse("prose_chunk", {"delta": chunk + " "})
                await asyncio.sleep(0)
            yield _sse("quick_fact", quick_fact.model_dump())
        elif intent in {"fundamental", "technical", "news_sentiment"} and isinstance(
            focused, FocusedAnalysis
        ):
            # QNT-176: focused-analysis card. Stream the summary as prose
            # so the panel has something to render before the full payload
            # lands, then emit the structured event.
            for chunk in _split_prose(focused.summary):
                yield _sse("prose_chunk", {"delta": chunk + " "})
                await asyncio.sleep(0)
            yield _sse("focused", focused.model_dump())
        elif isinstance(thesis, Thesis):
            # Stream prose. The structured-output runnable returns the entire
            # setup paragraph at once, so we re-chunk it client-side. A future
            # revision could thread an LLM token stream into this slot.
            for chunk in _split_prose(thesis.setup):
                yield _sse("prose_chunk", {"delta": chunk + " "})
                await asyncio.sleep(0)  # cooperative yield so the body flushes
            yield _sse("thesis", thesis.model_dump())
        elif isinstance(conversational, ConversationalAnswer):
            # Fallback redirect from a non-conversational intent that failed
            # mid-synthesize (no reports gathered, structured-output crash, etc).
            # The graph already populated ``conversational`` with a
            # ``domain_redirect`` payload — surface it the same way the
            # conversational intent does, so the panel renders the suggestion
            # card instead of an error.
            for chunk in _split_prose(conversational.answer):
                yield _sse("prose_chunk", {"delta": chunk + " "})
                await asyncio.sleep(0)
            yield _sse("conversational", conversational.model_dump())

        if intent == "comparison":
            citations_count = _count_comparison_citations(
                comparison if isinstance(comparison, ComparisonAnswer) else None
            )
        elif intent == "quick_fact":
            citations_count = _count_quick_fact_citations(
                quick_fact if isinstance(quick_fact, QuickFactAnswer) else None
            )
        elif intent in {"fundamental", "technical", "news_sentiment"}:
            citations_count = _count_focused_citations(
                focused if isinstance(focused, FocusedAnalysis) else None
            )
        elif intent == "conversational":
            # Conversational answers carry no citations by design — the
            # hallucination scorer rejects any digit, and there are no reports
            # to cite either.
            citations_count = 0
        else:
            citations_count = _count_citations(thesis if isinstance(thesis, Thesis) else None)

        yield _sse(
            "done",
            {
                "tools_count": len(reports),
                "citations_count": citations_count,
                "confidence": confidence,
                "intent": intent,
            },
        )
    finally:
        # QNT-150 + QNT-161: cleanup must run on EVERY exit path —
        #   client disconnect (GeneratorExit), timeout, normal completion,
        #   AND the new failure mode where build_graph / _instrument_tools
        #   crashed BEFORE runner_task was assigned. Hence the None guards
        #   on runner_task and the inner try/finally so reset_token_tracker
        #   always runs even if the budget bookkeeping itself raises.
        if runner_task is not None:
            if not runner_task.done():
                runner_task.cancel()
            # ``_runner`` already swallows ``Exception`` internally and stores
            # the failure in ``final_state_holder``, so the only thing the
            # await raises here is ``CancelledError`` (when we cancelled it
            # above, or when an outer cancellation reaches the shield).
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(runner_task)
        # QNT-161: charge the per-IP + global daily token budgets with what
        # the LangChain callback observed during this run. Done in finally
        # so a graph crash, a client disconnect, or a timeout still books
        # the spend (the LLM calls already happened upstream of the
        # exception). Fires the breaker-tripped Sentry alert when this
        # request is the one that crossed the global ceiling so the alert
        # lands at the moment of trip, not on subsequent requests.
        try:
            spent = tracker.total
            budget.record(client_ip, spent)
            if spent > 0:
                snap = budget.snapshot()
                if snap["global"] >= snap["global_cap"]:
                    await _maybe_alert_breaker_once()
                logger.info(
                    "chat token spend: ip=%s tokens=%d global=%d/%d",
                    client_ip,
                    spent,
                    snap["global"],
                    snap["global_cap"],
                )
        finally:
            reset_token_tracker(tracker_token)


@router.post("/chat")
@limiter.limit(settings.CHAT_RATE_LIMIT)
async def agent_chat(request: Request, body: ChatRequest) -> StreamingResponse:
    """Stream the agent's response to ``body`` as Server-Sent Events.

    Response media type is ``text/event-stream``; the body is a sequence of
    ``event: <name>\\ndata: <json>\\n\\n`` frames per the contract above.

    ``X-Accel-Buffering: no`` is set so any reverse proxy (Caddy, nginx)
    relays the body without buffering — without it the client wouldn't see a
    single event until the full agent run finished, defeating the streaming.

    QNT-161: ``request`` (the bare FastAPI Request) is required by SlowAPI
    to extract the client IP for the rate-limit key; the parsed body lives
    in ``body``. The ``@limiter.limit`` decorator enforces the per-IP
    request quota; per-IP / global token budgets are enforced inside
    ``_stream`` so the friendly redirect is delivered as a normal SSE
    stream rather than an HTTP error.
    """
    return StreamingResponse(
        _stream(body, client_ip(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = ["ChatRequest", "router"]
