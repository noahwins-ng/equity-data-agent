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

``tool_result``   — ``{name, label, latency_ms, summary, ok, started_at}``
                    emitted when the tool returns. ``summary`` is a short real
                    string derived from the report body (e.g. ``"38 lines"``
                    for technical) so the row can render without further
                    parsing. ``started_at`` echoes the matching ``tool_call``
                    clock so the panel binds the result to the exact call row
                    (QNT-252).

``prose_chunk``   — ``{delta}`` markdown deltas for the agent prose surface.
                    QNT-229 #5: emitted ONLY for the paths that skip the narrate
                    node -- ``conversational`` intent, the synthesize
                    fallback-redirect, and the budget/rate-limit redirect.
                    These have no ``narrative_chunk`` surface, so prose_chunk is
                    their only pre-card streaming text. The narrate-streaming
                    card shapes (thesis / comparison / focused / exploration) no
                    longer replay prose_chunk -- the narrative bubble is their
                    prose surface and the card lands directly. QNT-232 #3:
                    quick_fact also skips narrate now, but it emits no prose_chunk
                    either -- its early-emitted card answer IS the surface.

``narrative_chunk`` — ``{delta}`` token-level deltas from the QNT-211 narrate
                    node. Streamed AS the graph runs, so the frontend renders a
                    1-4 sentence analyst-voice prose bubble ABOVE the structured
                    card. QNT-229 #2b: the structured card event (below) is now
                    emitted at the END of synthesize_node -- i.e. BEFORE narrate
                    streams -- so the card renders while the bubble is still
                    streaming above it. Absent when the intent is
                    ``conversational`` (that path's answer is already prose),
                    when the intent is ``quick_fact`` (QNT-232 #3: its card
                    answer is already analyst-voice prose, so narrate is skipped),
                    or when the narrate LLM call failed (the structured card
                    still renders; bubble degrades).

The structured card events below (``thesis`` / ``quick_fact`` / ``comparison`` /
``comparison_lean`` / ``focused`` / ``exploration``) are emitted TWICE by
contract (QNT-229 #2b): once early, from ``synthesize_node`` via the
event_emitter the instant the payload is ready (before narrate streams), and
again post-graph from this module as an idempotent safety net (covers a silent
emitter failure + stubbed test graphs that bypass synthesize). The panel's
``updateRun`` is idempotent -- the duplicate frame overwrites the same payload.

``thesis``        — full :class:`~agent.thesis.Thesis` model dumped to JSON.
                    Renders the Setup / Bull / Bear / Verdict card. Emitted
                    only when intent == "thesis".

``quick_fact``    — full :class:`~agent.quick_fact.QuickFactAnswer` model
                    dumped to JSON (QNT-149). Emitted only when
                    intent == "quick_fact"; the panel renders a compact
                    answer + cited value chip and skips the thesis card.
                    QNT-232 #3: narrate is skipped for this shape, so the card
                    answer is the lone prose surface (no narrative bubble above).

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
                    {"fundamental", "technical", "news"}; the
                    panel renders a focused-analysis card and skips the
                    thesis card.

``exploration``   — full :class:`~agent.exploration.ExplorationAnswer` dumped
                    to JSON (QNT-220 follow-up). Emitted only when intent ==
                    "exploration" (set by explore_supervisor, never by the
                    classifier); the panel renders a verdict-free, multi-lens
                    scan card and skips the thesis card.

``retrieved_sources`` — ``{sources: [{headline, source, date, url, corpus}, ...]}``
                    emitted (QNT-226) when the agent's semantic search surfaced
                    hits this turn. Each source carries ``corpus`` (QNT-263:
                    ``"news"`` or ``"earnings"``) so the panel labels which
                    corpus a citation came from. The panel renders a compact
                    clickable provenance list. Only fires when ``gather`` ran
                    (a followup turn reuses hydrated state and re-emits nothing).

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
arrive (this is how the early card + narrative_chunk events reach the client
mid-run). After the graph completes, the post-run events (the idempotent card
re-emit, any ``prose_chunk`` for redirect paths, ``done``) are emitted from the
main coroutine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC
from typing import Any

from agent.comparison import ComparisonAnswer, LeanComparisonAnswer
from agent.conversational import ConversationalAnswer, domain_redirect
from agent.eval_scores import push_to_trace_id as push_eval_scores
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.graph import OPTIONAL_TOOLS, build_graph
from agent.llm import (
    ServedModelTracker,
    TokenUsageTracker,
    current_model_info,
    reset_served_model_tracker,
    reset_token_tracker,
    resolve_trace_model_tag,
    set_served_model_tracker,
    set_token_tracker,
)
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis
from agent.tools import (
    default_report_tools,
    get_company_report_compact,
    get_comparison_metrics,
    search_earnings,
    search_news,
)
from agent.tracing import langfuse, make_callback_handler, propagate_attributes
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
    # QNT-209/245: opaque conversation identifier supplied by the frontend
    # (one per ChatPanel mount, ticker-agnostic; lifetime = component state,
    # lost on refresh). The answered subject ticker is a per-turn property
    # (QNT-228 rebase), so one thread spans turns about different tickers.
    # When present the backend persists the run via SqliteSaver so a follow-up
    # on the same thread_id can reuse the prior turn's reports. When absent
    # (curl, tests, non-frontend callers) the request runs against an
    # ephemeral compile with no checkpointer.
    thread_id: str | None = Field(default=None, max_length=128)

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
    # QNT-222: semantic news search fired on targeted news asks.
    "news_search": "Searching news",
    # QNT-263: semantic earnings-release search fired on earnings-narrative asks.
    "earnings_search": "Searching earnings releases",
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
    """Citations for the v2 four-aspect thesis (QNT-208).

    Scans every aspect's summary + supports + challenges plus the verdict
    rationale for ``(source: ...)`` parens -- same chip vocabulary the
    panel renders.
    """
    if thesis is None:
        return 0
    texts: list[str] = [thesis.verdict_rationale]
    for aspect in (thesis.company, thesis.fundamental, thesis.technical, thesis.news):
        texts.append(aspect.summary)
        texts.extend(aspect.supports)
        texts.extend(aspect.challenges)
    return sum(len(_CITATION_PATTERN.findall(text or "")) for text in texts)


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
    """Citations for the comparison path (QNT-208 four-aspect shape).

    Scans every aspect block across both per-ticker sections plus the
    differences paragraph for ``(source: ...)`` parens.
    """
    if comparison is None:
        return 0
    texts: list[str] = [comparison.differences]
    for section in comparison.sections:
        for aspect in (section.company, section.fundamental, section.technical, section.news):
            texts.append(aspect.summary)
            texts.extend(aspect.supports)
            texts.extend(aspect.challenges)
    return sum(len(_CITATION_PATTERN.findall(text or "")) for text in texts)


def _count_lean_comparison_citations(comparison_lean: LeanComparisonAnswer | None) -> int:
    """Citations for the lean N-way comparison (QNT-224).

    The lean shape carries no prose / ``(source: ...)`` parens — every metric
    cell IS a verbatim cited value (copied straight from the API, ADR-003). So
    the count is the number of populated cells across all rows; an ``N/M`` cell
    has no underlying value and does not count.
    """
    if comparison_lean is None:
        return 0
    count = 0
    for row in comparison_lean.rows:
        for cell in (row.pe, row.rsi, row.net_margin, row.price):
            if cell and not cell.startswith("N/M"):
                count += 1
    return count


def _count_focused_citations(focused: FocusedAnalysis | None) -> int:
    """Citations for the focused-analysis path (QNT-176, QNT-208).

    Each ``FocusedValue`` carries a structured source; each inline
    ``(source: ...)`` parens in the summary / key_points / news catalyst
    lists adds one.
    """
    if focused is None:
        return 0
    structured = len(focused.cited_values)
    inline_texts: list[str] = [focused.summary, *focused.key_points]
    if focused.existing_development:
        inline_texts.append(focused.existing_development)
    inline_texts.extend(focused.positive_catalysts)
    inline_texts.extend(focused.negative_catalysts)
    inline = sum(len(_CITATION_PATTERN.findall(text or "")) for text in inline_texts)
    return structured + inline


def _count_exploration_citations(exploration: ExplorationAnswer | None) -> int:
    """Citations for the exploration-scan path (QNT-220 follow-up).

    Each ``ExplorationValue`` carries a structured source; each inline
    ``(source: ...)`` parens in the headline / observations adds one.
    """
    if exploration is None:
        return 0
    structured = len(exploration.cited_values)
    inline_texts: list[str] = [exploration.headline, *exploration.observations]
    inline = sum(len(_CITATION_PATTERN.findall(text or "")) for text in inline_texts)
    return structured + inline


# ─── Prose chunking ─────────────────────────────────────────────────────────

# Split a paragraph into clause-sized chunks so the panel can render it
# progressively. Used by the thesis path (company-aspect summary) and by
# the focused / comparison paths (focused summary, comparison differences).
# A real token-stream would emit one chunk per LLM token; the structured
# output runnable doesn't expose that, so clause-level is the next-best
# granularity. Punctuation is kept on the chunk that ended the clause.
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
                            "started_at": started_at,
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
                        "started_at": started_at,
                    },
                )
                return result

            return wrapper

        wrapped[name] = make_wrapper(name, fn)
        # Tag the wrapper with the original ticker so we can sanity-check
        # the inner call in tests if needed.
        wrapped[name].__wrapped_ticker__ = ticker  # type: ignore[attr-defined]
    return wrapped


def _instrument_search_tool(
    fn: Callable[[str, str], str],
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    loop: asyncio.AbstractEventLoop,
    *,
    tool_name: str,
    hit_noun: str,
) -> Callable[[str, str], str]:
    """QNT-222/263: ``tool_call`` / ``tool_result`` instrumentation for a two-arg
    semantic-search tool (``search_news`` / ``search_earnings``).

    ``_instrument_tools`` wraps single-arg report tools; a semantic search tool
    carries the user's question as a second arg, so it gets its own wrapper.
    The ``tool_call`` event surfaces the search path on the panel; the result
    summary reports the retrieved-hit count. ``tool_name`` selects the panel
    label (``news_search`` / ``earnings_search``) and ``hit_noun`` the summary
    unit so the two corpora read distinctly in the trace.
    """

    def _post(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def wrapper(ticker: str, query: str) -> str:
        started_at = time.time()
        _post(
            "tool_call",
            {
                "name": tool_name,
                "label": _tool_label(tool_name),
                "args": {"ticker": ticker, "query": query},
                "started_at": started_at,
            },
        )
        result = fn(ticker, query)
        latency_ms = int((time.time() - started_at) * 1000)
        try:
            hit_count = len(json.loads(result)) if result else 0
        except (ValueError, TypeError):
            hit_count = 0
        _post(
            "tool_result",
            {
                "name": tool_name,
                "label": _tool_label(tool_name),
                "latency_ms": latency_ms,
                "summary": f"{hit_count} {hit_noun}" if hit_count else "no matches",
                "ok": True,
                "started_at": started_at,
            },
        )
        return result

    return wrapper


# ─── Streaming generator ────────────────────────────────────────────────────


_DONE_FAILURE_PAYLOAD: dict[str, Any] = {
    "tools_count": 0,
    "citations_count": 0,
    "confidence": 0.0,
}


def _done_failure_payload(
    thread_id: str | None,
    intent_path: list[str] | None = None,
) -> dict[str, Any]:
    """QNT-209/212: failure-path done payload carries ``thread_id`` and
    ``intent_path`` so the frontend can confirm both what the backend used
    and which nodes ran even when the run errored or timed out before any
    payload was produced. ``intent_path`` defaults to ``[]`` when no graph
    node ran (unknown ticker, budget-exhausted redirect, etc.)."""
    return {
        **_DONE_FAILURE_PAYLOAD,
        "thread_id": thread_id,
        "intent_path": list(intent_path) if intent_path else [],
    }


# QNT-209: process-wide SqliteSaver singleton. Built lazily on the first chat
# request that supplies a ``thread_id`` (the FastAPI lifespan in main.py also
# eagerly initialises it on startup so the prune loop has something to scan).
# A single connection with check_same_thread=False is the documented usage:
# SqliteSaver guards its OWN cursor with an internal lock, and the underlying
# sqlite3 module compiles with threadsafety=3 (serialized) so direct
# ``conn.execute(...)`` calls from ``touch_thread`` (event loop) interleaved
# with saver writes (asyncio.to_thread worker) are safe at the sqlite layer.
# The saver lock guarantees nothing about access bypassing it -- the
# serialized-mode guarantee from the sqlite module is what makes this safe.
_CHECKPOINTER_SINGLETON: object | None = None
_CHECKPOINTER_CONN: object | None = None


def get_checkpointer() -> object | None:
    """Return the lazily-built ``SqliteSaver`` instance, or None on failure.

    Failure modes: SQLite file unwriteable, sqlite3 module unavailable,
    langgraph_checkpoint_sqlite import error. On any of these we log and
    return None so the chat endpoint degrades to the ephemeral path rather
    than 500'ing.
    """
    global _CHECKPOINTER_SINGLETON, _CHECKPOINTER_CONN
    if _CHECKPOINTER_SINGLETON is not None:
        return _CHECKPOINTER_SINGLETON
    try:
        import os
        import sqlite3

        from agent.memory import init_thread_metadata
        from langgraph.checkpoint.sqlite import SqliteSaver

        os.makedirs(os.path.dirname(settings.AGENT_DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(settings.AGENT_DB_PATH, check_same_thread=False)
        saver = SqliteSaver(conn)
        # Force the LangGraph tables into existence before the sidecar so the
        # prune deletes have something to reference when the loop fires.
        saver.setup()
        init_thread_metadata(conn)
        _CHECKPOINTER_CONN = conn
        _CHECKPOINTER_SINGLETON = saver
        return saver
    except Exception as exc:  # noqa: BLE001 — checkpointer must not block the API
        logger.warning("agent SqliteSaver unavailable, falling back to ephemeral: %s", exc)
        return None


def get_checkpointer_conn() -> object | None:
    """Return the raw sqlite3.Connection backing the checkpointer (or None).

    The prune loop in ``api.main`` uses this to call ``prune_stale_threads``
    against the same database file the saver is writing to.
    """
    if _CHECKPOINTER_SINGLETON is None:
        get_checkpointer()
    return _CHECKPOINTER_CONN


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


async def _stream(request: ChatRequest, client_ip: str) -> AsyncIterator[str]:  # noqa: C901, PLR0915 — orchestration coroutine, refactor candidate for QNT-209 follow-up
    """Yield SSE frames for one chat request.

    Validation failures yield a single ``error`` event followed by ``done``;
    a typical narrate-shape success yields ``intent`` → ``tool_call`` →
    ``tool_result`` → the structured card (e.g. ``thesis``, emitted early from
    synthesize) → ``narrative_chunk`` stream → ``done``. The prose-reply paths
    (conversational / redirect) substitute a ``prose_chunk`` stream for the
    card+narrate pair. See the module docstring for the full event contract.

    QNT-161: ``client_ip`` drives the per-IP daily token budget. Both the
    per-IP and global TPD breakers are checked BEFORE the graph runs; on
    exhaustion we emit a deterministic conversational redirect (no graph
    invocation, no LLM cost) and the panel renders the same redirect card it
    uses for off-domain questions. The agent never reaches a paid provider
    in either branch — see ADR-017.

    QNT-209/245: ``request.thread_id`` is the per-conversation memory key the
    frontend supplies (one per ChatPanel mount, ticker-agnostic — the subject
    ticker is per-turn, QNT-228). None = ephemeral (curl, tests, no-frontend)
    → no checkpointer at compile, no sidecar touch, nothing persists.
    """
    thread_id = request.thread_id
    use_memory = thread_id is not None
    ticker = request.ticker.upper()
    if ticker not in TICKERS:
        yield _sse(
            "error",
            {"detail": f"Unknown ticker: {ticker}", "code": "unknown-ticker"},
        )
        yield _sse("done", _done_failure_payload(thread_id))
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
                "thread_id": thread_id,
                # QNT-212: budget-exhausted redirect never reaches the graph,
                # so no nodes ran -- empty path so consumers can rely on the
                # field being present on every done event.
                "intent_path": [],
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
    # QNT-230 #14: capture the model LiteLLM actually serves so a fallback fire
    # (e.g. default -> fallback-llama4scout on Groq TPD exhaustion) tags the
    # trace with the real model instead of the static-map alias. Same
    # contextvar-through-to_thread propagation as the token tracker.
    served_tracker = ServedModelTracker()
    served_tracker_token = set_served_model_tracker(served_tracker)
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
        # QNT-220 (#8): compact company variant for the thesis/comparison hot
        # path. Instrumented under the "company" key so the tool_call event +
        # panel label stay "company"; only the payload (full vs compact) differs.
        compact_company = _instrument_tools(
            {"company": get_company_report_compact}, queue, loop, ticker
        )["company"]
        # QNT-222: semantic news search, wired as its own (ticker, query) tool
        # outside the single-arg plan-surface map. Fired by the graph only on
        # targeted news asks (litigation, CEO, buyback, lawsuit, recall, ...).
        search_news_tool = _instrument_search_tool(
            search_news, queue, loop, tool_name="news_search", hit_noun="headlines"
        )
        # QNT-263: semantic earnings-release search, the second RAG corpus. Fired
        # by the graph only on earnings-narrative asks (guidance, outlook,
        # management framing).
        search_earnings_tool = _instrument_search_tool(
            search_earnings, queue, loop, tool_name="earnings_search", hit_noun="excerpts"
        )
        # QNT-209: attach the SqliteSaver only when the request named a
        # thread_id. The ephemeral compile path keeps the existing behavior
        # for curl / tests / non-frontend callers — no checkpoint rows, no
        # sidecar touch.
        checkpointer = get_checkpointer() if use_memory else None
        if use_memory and checkpointer is not None and thread_id is not None:
            try:
                from agent.memory import touch_thread

                conn = get_checkpointer_conn()
                if conn is not None:
                    # type: ignore[arg-type] — runtime sqlite3.Connection
                    touch_thread(conn, thread_id)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 — sidecar touch must not block the run
                logger.warning("touch_thread failed for %s: %s", thread_id, exc)
        graph = build_graph(
            instrumented,
            event_emitter=_emit,
            checkpointer=checkpointer,  # type: ignore[arg-type]
            compact_company_tool=compact_company,
            search_news_tool=search_news_tool,
            search_earnings_tool=search_earnings_tool,
            comparison_metrics_tool=get_comparison_metrics,
        )

        # QNT-181: tag every Langfuse trace with a per-request session_id
        # (uuid4) and a per-IP user_id (truncated sha256 of client_ip). The
        # hash is for stable filter cardinality in the Langfuse UI -- "all
        # traces from this IP" is one filter click -- not for privacy.
        # ADR-017 already established this as an open public-demo with no
        # auth and no PII; the IP is not sensitive (city-level geo at most,
        # which any HTTP server already logs). If login/multi-tenant ever
        # lands, swap to HMAC + a SOPS-encrypted pepper before user_id maps
        # to a real identity.
        session_id = str(uuid.uuid4())
        user_id = hashlib.sha256(client_ip.encode()).hexdigest()[:12]

        def _runner() -> None:
            # Run the graph in a worker thread; emitted events have already
            # been routed onto the queue via ``call_soon_threadsafe``. The
            # final state is captured for post-run prose / thesis / done
            # events.
            #
            # Tracing topology (Langfuse v4): the trace and its root
            # observation are always rendered as two separate rows in the v4
            # UI. We deliberately give them DIFFERENT names so the hierarchy
            # is self-documenting:
            #
            #   agent-chat        <- trace (the request boundary; carries
            #                       session_id, user_id, intent/model tags,
            #                       hallucination_ok / plan_adherence scores)
            #     langgraph-run   <- root span (the LangGraph runnable; the
            #                       parent of classify/plan/gather/synthesize
            #                       and the per-LLM ChatOpenAI generations)
            #
            # The trace name comes from ``propagate_attributes(trace_name=...)``;
            # the root-span name comes from ``run_name`` in RunnableConfig.
            # session_id / user_id / model are passed via the langfuse-langchain
            # metadata keys (``langfuse_session_id`` / ``langfuse_user_id``)
            # so the handler attaches them at trace level.
            handler = make_callback_handler()
            # QNT-182 follow-up: stamp the resolved upstream model on every
            # observation in the trace via metadata. LangChain only sees the
            # LiteLLM alias (we pass ``model=alias`` to ChatOpenAI), so without
            # this the trace is unanswerable on "which model served this run".
            # Static map -- doesn't catch fallback fires (separate ticket).
            model_info = current_model_info()
            graph_config: dict[str, object] = {}
            if handler is not None:
                graph_config = {
                    "callbacks": [handler],
                    "run_name": "langgraph-run",
                    "metadata": {
                        "langfuse_session_id": session_id,
                        "langfuse_user_id": user_id,
                        **model_info,
                    },
                }
            # QNT-209: a compiled-with-checkpointer graph REQUIRES
            # configurable.thread_id on every invoke. Setting it for the
            # ephemeral compile too is harmless (no checkpointer reads it).
            if thread_id is not None:
                existing_configurable = graph_config.get("configurable") or {}
                if not isinstance(existing_configurable, dict):
                    existing_configurable = {}
                graph_config["configurable"] = {
                    **existing_configurable,
                    "thread_id": thread_id,
                }
            try:
                with propagate_attributes(trace_name="agent-chat"):
                    final_state_holder["state"] = graph.invoke(
                        {"ticker": ticker, "question": request.message},
                        config=graph_config,  # type: ignore[arg-type]
                    )
            except Exception as exc:  # noqa: BLE001 — surfaced as SSE error
                logger.exception("agent graph failed for %s", ticker)
                final_state_holder["error"] = exc
                return

            state_obj = final_state_holder.get("state")
            if not isinstance(state_obj, dict):
                return
            # The handler exposes the trace it just created via
            # ``last_trace_id``. ``None`` when tracing is disabled or no LLM
            # call fired inside the graph (deterministic redirect path).
            trace_id = getattr(handler, "last_trace_id", None) if handler is not None else None
            # QNT-182: push deterministic eval scores onto this trace.
            # Safe no-op when Langfuse keys are unset OR ``trace_id`` is None.
            push_eval_scores(state_obj, trace_id)
            # QNT-182 follow-up: tag the trace with the resolved intent so
            # the Tracing list is filterable by shape ("show me only
            # conversational redirects" / "thesis-shape only") without
            # parsing the SSE event stream. Belongs here, after graph.invoke,
            # because the intent isn't decided until classify_node runs.
            # Langfuse v4 has no public ``update_current_trace`` for tags;
            # the ingestion path with the resolved trace_id is the only way
            # to add a tag post-hoc. Swallowed on failure -- the trace just
            # won't carry the intent tag, which is benign vs. crashing the
            # request.
            intent = state_obj.get("intent")
            if isinstance(intent, str) and intent and langfuse is not None and trace_id:
                try:
                    # Pair the intent tag with the resolved upstream model.
                    # Metadata is filterable by key but tags surface in the
                    # Tags column with one "any of" operator across both
                    # axes ("conversational redirects on llama-3.3-70b" is
                    # two tag chips).
                    tags = [f"intent:{intent}"]
                    # QNT-230 #14: prefer the model LiteLLM actually served this
                    # run over the static alias map, so a fallback fire tags the
                    # real model. Falls back to the static resolution when no
                    # fallback fired, keeping existing model: filters valid.
                    model_tag_value, fallback_fired = resolve_trace_model_tag(
                        alias=model_info.get("alias", ""),
                        static_resolved=model_info.get("resolved_model", "unknown"),
                        served_info=served_tracker.info(),
                    )
                    if model_tag_value and model_tag_value != "unknown":
                        tags.append(f"model:{model_tag_value}")
                    if fallback_fired:
                        tags.append("model_fallback:fired")
                    classifier_source = state_obj.get("classifier_source")
                    if isinstance(classifier_source, str) and classifier_source:
                        tags.append(f"classifier_source:{classifier_source}")
                    grounding_rate = state_obj.get("grounding_rate")
                    if isinstance(grounding_rate, int | float):
                        rate = max(0.0, min(1.0, float(grounding_rate)))
                        tags.append(
                            "runtime_grounding:clean" if rate >= 1.0 else "runtime_grounding:miss"
                        )
                        tags.append(f"runtime_grounding_rate:{rate:.2f}")
                    langfuse._create_trace_tags_via_ingestion(  # noqa: SLF001 — no public v4 equivalent
                        trace_id=trace_id,
                        tags=tags,
                    )
                except Exception as exc:  # noqa: BLE001 — telemetry must not crash
                    logger.warning("eval-tag push failed: %s", exc)

        runner_task = asyncio.create_task(asyncio.to_thread(_runner))
        run_deadline = loop.time() + settings.CHAT_RUN_TIMEOUT
        partial_tool_results: list[str] = []
        answer_surface_events = {
            "thesis",
            "quick_fact",
            "comparison",
            "comparison_lean",
            "focused",
            "exploration",
            "conversational",
            "narrative_chunk",
            "prose_chunk",
        }
        answer_surface_streamed = False
        intent_event_streamed = False

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
            if event == "intent":
                intent_event_streamed = True
            if event == "tool_result" and isinstance(data, dict):
                name = data.get("name")
                ok = data.get("ok")
                if ok is True and isinstance(name, str) and name not in partial_tool_results:
                    partial_tool_results.append(name)
            elif event in answer_surface_events:
                answer_surface_streamed = True
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
            if partial_tool_results and not answer_surface_streamed:
                labels = [_tool_label(name).lower() for name in partial_tool_results]
                if len(labels) == 1:
                    read_list = labels[0]
                else:
                    read_list = f"{', '.join(labels[:-1])}, and {labels[-1]}"
                yield _sse(
                    "prose_chunk",
                    {
                        "delta": (
                            "I timed out before finishing the answer, but I did "
                            f"finish {read_list}. "
                        )
                    },
                )
            yield _sse(
                "error",
                {"detail": _ERROR_DETAIL_AGENT_TIMEOUT, "code": "agent-timeout"},
            )
            yield _sse("done", _done_failure_payload(thread_id))
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
            yield _sse("done", _done_failure_payload(thread_id))
            return

        state = final_state_holder.get("state") or {}
        thesis = state.get("thesis") if isinstance(state, dict) else None
        quick_fact = state.get("quick_fact") if isinstance(state, dict) else None
        comparison = state.get("comparison") if isinstance(state, dict) else None
        comparison_lean = state.get("comparison_lean") if isinstance(state, dict) else None
        conversational = state.get("conversational") if isinstance(state, dict) else None
        focused = state.get("focused") if isinstance(state, dict) else None
        exploration = state.get("exploration") if isinstance(state, dict) else None
        intent = state.get("intent", "thesis") if isinstance(state, dict) else "thesis"
        confidence = float(state.get("confidence", 0.0)) if isinstance(state, dict) else 0.0
        grounding_rate = float(state.get("grounding_rate", 1.0)) if isinstance(state, dict) else 1.0
        errors = state.get("errors") or {} if isinstance(state, dict) else {}
        reports = state.get("reports") or {} if isinstance(state, dict) else {}
        plan = list(state.get("plan") or []) if isinstance(state, dict) else []
        supervisor_iterations = (
            int(state.get("supervisor_iterations", 0)) if isinstance(state, dict) else 0
        )
        # QNT-212: ordered list of nodes that actually fired this turn.
        # Reads off the AgentState reducer field; defaults to ``[]`` for
        # stubbed test graphs that don't populate it. Surfaced on every
        # done event so the frontend (and Langfuse filters, ad-hoc grep)
        # can see "did this turn skip plan + gather?" without inspecting
        # the whole event stream.
        intent_path = list(state.get("intent_path") or []) if isinstance(state, dict) else []
        # QNT-226: provenance for the semantic news search. Gather stores the
        # retrieved hits; we surface them as a clickable "Retrieved sources"
        # list. Gated on ``gather`` having run this turn (mirrors the
        # tools_count guard below) so a followup turn -- which skips gather and
        # reuses checkpointer-hydrated state -- does not re-emit the prior
        # turn's sources.
        retrieved_sources = (
            list(state.get("retrieved_sources") or []) if isinstance(state, dict) else []
        )

        # QNT-159: classify_node already streamed the ``intent`` event via the
        # queue-based emitter (so the panel saw it before the first tool_call).
        # This post-graph yield is now an idempotent safety net for two cases:
        # (1) the emitter raised silently and the early frame never made it onto
        # the queue, and (2) stubbed test graphs that bypass classify_node and
        # return a canned final state directly. The panel's ``updateRun(intent)``
        # is idempotent — duplicate frames overwrite ``run.intent`` with the
        # same value, so the cost of leaving this safety net in is one extra
        # SSE frame per real request.
        if isinstance(state, dict) and "intent" in state and not intent_event_streamed:
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
            # QNT-229 #2b/#5: card emitted early from synthesize_node; this
            # post-graph yield is the idempotent safety net. narrate streams
            # the qualitative contrast as the prose surface, so no prose_chunk.
            yield _sse("comparison", comparison.model_dump())
        elif intent == "comparison" and isinstance(comparison_lean, LeanComparisonAnswer):
            # QNT-224: lean 3-4 way metrics table. No differences field — the
            # qualitative contrast streams as the narrate node's narrative
            # bubble (narrative_chunk), so here we only emit the structured
            # metrics rows the panel renders as a table.
            yield _sse("comparison_lean", comparison_lean.model_dump())
        elif intent in {"quick_fact", "followup"} and isinstance(quick_fact, QuickFactAnswer):
            # QNT-209: followup reuses the QuickFactAnswer schema (the panel
            # already renders it). The intent event still carries "followup"
            # so Langfuse/UI can tell the two apart; the rendered card is
            # the same compact answer + cited value chip.
            # QNT-229 #2b/#5: card emitted early from synthesize_node; this is
            # the idempotent net. QNT-232 #3: quick_fact skips narrate, so the
            # card answer is the surface -- no narrative bubble, no prose_chunk.
            # (followup reuses this branch and DOES narrate; intent tells apart.)
            yield _sse("quick_fact", quick_fact.model_dump())
        elif intent in {"fundamental", "technical", "news"} and isinstance(
            focused, FocusedAnalysis
        ):
            # QNT-176: focused-analysis card.
            # QNT-229 #2b/#5: card emitted early from synthesize_node; this is
            # the idempotent net. narrate owns the prose surface -> no prose_chunk.
            yield _sse("focused", focused.model_dump())
        elif intent == "exploration" and isinstance(exploration, ExplorationAnswer):
            # QNT-220 follow-up: exploration-scan card.
            # QNT-229 #2b/#5: card emitted early from synthesize_node; this is
            # the idempotent net. narrate owns the prose surface -> no prose_chunk.
            yield _sse("exploration", exploration.model_dump())
        elif intent == "thesis" and isinstance(thesis, Thesis):
            # QNT-211: gate on intent so the followup narrative-only path
            # (intent=followup, quick_fact=None, thesis hydrated from the
            # prior turn) doesn't re-emit a thesis event from the cached
            # state. The bubble alone is the response.
            # QNT-229 #2b/#5: card emitted early from synthesize_node; this is
            # the idempotent net. narrate owns the prose surface -> no prose_chunk.
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

        # QNT-226: emit the retrieved-sources provenance list (headline / source
        # / date / url) the agent's semantic news search surfaced this turn.
        # Only when gather actually ran -- a followup turn reuses hydrated state
        # and must not re-show the prior turn's sources.
        if retrieved_sources and "gather" in intent_path:
            yield _sse("retrieved_sources", {"sources": retrieved_sources})

        if intent == "comparison":
            # QNT-224: rich (2-ticker) vs lean (3-4 ticker) carry citations
            # differently — paren-scan the aspect prose vs count metric cells.
            if isinstance(comparison, ComparisonAnswer):
                citations_count = _count_comparison_citations(comparison)
            else:
                citations_count = _count_lean_comparison_citations(
                    comparison_lean if isinstance(comparison_lean, LeanComparisonAnswer) else None
                )
        elif intent in {"quick_fact", "followup"}:
            citations_count = _count_quick_fact_citations(
                quick_fact if isinstance(quick_fact, QuickFactAnswer) else None
            )
        elif intent in {"fundamental", "technical", "news"}:
            citations_count = _count_focused_citations(
                focused if isinstance(focused, FocusedAnalysis) else None
            )
        elif intent == "exploration":
            citations_count = _count_exploration_citations(
                exploration if isinstance(exploration, ExplorationAnswer) else None
            )
        elif intent == "conversational":
            # Conversational answers carry no citations by design — the
            # hallucination scorer rejects any digit, and there are no reports
            # to cite either.
            citations_count = 0
        else:
            citations_count = _count_citations(thesis if isinstance(thesis, Thesis) else None)

        # QNT-209/212: tools_count reflects tools INVOKED this turn, not the
        # size of the hydrated report bundle. Any turn that skipped gather
        # (followup, conversational, and clarify all short-circuit past
        # plan+gather per QNT-212) reused or ignored the prior turn's
        # checkpointer-hydrated reports and fired zero tools, so the frontend
        # chip must read "0 sources" rather than the misleading hydrated
        # count. ``gather`` in intent_path is the authoritative signal; the
        # ``intent_path and`` guard preserves the old len(reports) fallback
        # for stubbed test graphs that don't populate intent_path, and the
        # explicit followup clause keeps the QNT-209 contract for those stubs
        # (which set intent="followup" with hydrated reports but no path).
        if "explore_supervisor" in intent_path:
            tools_count = len(plan)
        else:
            tools_count = (
                0
                if intent == "followup" or (intent_path and "gather" not in intent_path)
                else len(reports)
            )
        yield _sse(
            "done",
            {
                "tools_count": tools_count,
                "citations_count": citations_count,
                "confidence": confidence,
                "grounding_rate": grounding_rate,
                "grounding_unsupported": (
                    list(state.get("grounding_unsupported", [])) if isinstance(state, dict) else []
                ),
                "intent": intent,
                "thread_id": thread_id,
                "intent_path": intent_path,
                "supervisor_iterations": supervisor_iterations,
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
            reset_served_model_tracker(served_tracker_token)


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
