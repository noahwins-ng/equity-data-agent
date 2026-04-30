"""Agent chat SSE endpoint — ``POST /api/v1/agent/chat`` (QNT-74).

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
                    Renders the Setup / Bull / Bear / Verdict card.

``done``          — ``{tools_count, citations_count, confidence, errors}``
                    final stats. ``citations_count`` is the number of inline
                    ``(source: …)`` cites the structured thesis carries.

``error``         — ``{detail}`` terminal failure event (validation, agent
                    crash). Frontend should surface and stop reading.

The graph itself is synchronous (Python LangGraph, sync ``invoke``). To stream
incrementally we wrap each tool with an instrumented adapter that posts
events to an :class:`asyncio.Queue` from a worker thread, run the graph in
``asyncio.to_thread``, and yield queued events to the SSE client as they
arrive. After the graph completes, the post-run events (``prose_chunk``,
``thesis``, ``done``) are emitted from the main coroutine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from agent.graph import OPTIONAL_TOOLS, build_graph
from agent.thesis import Thesis
from agent.tools import default_report_tools
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shared.tickers import TICKERS

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
    tools_enabled: bool = True
    cite_sources: bool = True


# ─── Tool labels (human-friendly names for the UI) ──────────────────────────

# Canonical mapping — the UI never sees raw function names. Keep aligned with
# ``agent.prompts.REPORT_TOOLS`` (sweep would surface a missing entry).
_TOOL_LABELS: dict[str, str] = {
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


async def _stream(request: ChatRequest) -> AsyncIterator[str]:
    """Yield SSE frames for one chat request.

    Validation failures yield a single ``error`` event followed by ``done``;
    success yields the canonical ``tool_call`` → ``tool_result`` → ``prose_chunk``
    → ``thesis`` → ``done`` sequence.
    """
    ticker = request.ticker.upper()
    if ticker not in TICKERS:
        yield _sse(
            "error",
            {"detail": f"Unknown ticker: {ticker}", "code": "unknown-ticker"},
        )
        yield _sse("done", {"tools_count": 0, "citations_count": 0, "confidence": 0.0})
        return

    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    base_tools = default_report_tools() if request.tools_enabled else {}
    instrumented = _instrument_tools(base_tools, queue, loop, ticker)
    graph = build_graph(instrumented)

    final_state_holder: dict[str, Any] = {}

    def _runner() -> None:
        # Run the graph in a worker thread; emitted events have already been
        # routed onto the queue via ``call_soon_threadsafe``. The final state
        # is captured for post-run prose / thesis / done events.
        try:
            final_state_holder["state"] = graph.invoke(
                {"ticker": ticker, "question": request.message}
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as SSE error
            logger.exception("agent graph failed for %s", ticker)
            final_state_holder["error"] = exc

    runner_task = asyncio.create_task(asyncio.to_thread(_runner))

    # Drain queue while the graph is running. ``runner_task.done()`` flips
    # only after the worker thread completes; until then we await the next
    # queued event with a short timeout so the loop stays responsive.
    while not runner_task.done() or not queue.empty():
        try:
            event, data = await asyncio.wait_for(queue.get(), timeout=0.1)
        except TimeoutError:
            continue
        yield _sse(event, data)

    if "error" in final_state_holder:
        exc = final_state_holder["error"]
        yield _sse(
            "error",
            {
                "detail": f"agent error: {type(exc).__name__}: {exc}",
                "code": "agent-failed",
            },
        )
        yield _sse("done", {"tools_count": 0, "citations_count": 0, "confidence": 0.0})
        return

    state = final_state_holder.get("state") or {}
    thesis = state.get("thesis") if isinstance(state, dict) else None
    confidence = float(state.get("confidence", 0.0)) if isinstance(state, dict) else 0.0
    errors = state.get("errors") or {} if isinstance(state, dict) else {}
    reports = state.get("reports") or {} if isinstance(state, dict) else {}

    # Surface required-tool failures the graph recorded so the panel can
    # indicate gaps (optional tools like ``news`` are silently dropped per
    # OPTIONAL_TOOLS contract — those don't reach this dict).
    for name, detail in errors.items():
        if name in OPTIONAL_TOOLS:
            continue
        yield _sse(
            "error",
            {"detail": f"{_tool_label(name)} failed: {detail}", "code": "tool-failed"},
        )

    # Stream prose. The structured-output runnable returns the entire setup
    # paragraph at once, so we re-chunk it client-side. A future revision
    # could thread an LLM token stream into this slot.
    if isinstance(thesis, Thesis):
        for chunk in _split_prose(thesis.setup):
            yield _sse("prose_chunk", {"delta": chunk + " "})
            await asyncio.sleep(0)  # cooperative yield so the body flushes

        yield _sse("thesis", thesis.model_dump())
    elif reports:
        # Graph reached synthesize but the LLM returned a malformed thesis
        # (rare — see graph.py "_coerce_thesis"). Emit an explicit error so
        # the panel doesn't render a half-state thesis card.
        yield _sse(
            "error",
            {
                "detail": "Thesis unavailable — model returned no structured output.",
                "code": "thesis-empty",
            },
        )
    # No reports + no thesis: the graph short-circuited (every required tool
    # failed). The error events above already surface the cause.

    yield _sse(
        "done",
        {
            "tools_count": len(reports),
            "citations_count": _count_citations(thesis if isinstance(thesis, Thesis) else None),
            "confidence": confidence,
        },
    )


@router.post("/chat")
async def agent_chat(request: ChatRequest) -> StreamingResponse:
    """Stream the agent's response to ``request`` as Server-Sent Events.

    Response media type is ``text/event-stream``; the body is a sequence of
    ``event: <name>\\ndata: <json>\\n\\n`` frames per the contract above.

    ``X-Accel-Buffering: no`` is set so any reverse proxy (Caddy, nginx)
    relays the body without buffering — without it the client wouldn't see a
    single event until the full agent run finished, defeating the streaming.
    """
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = ["ChatRequest", "router"]
