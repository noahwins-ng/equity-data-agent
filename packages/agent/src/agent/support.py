"""QNT-294 (AC1): pure node-shared helpers extracted from graph.py.

Planning/parsing, confidence, tool gather, semantic-search formatting + folding
(RetrievalSpec), lean-comparison building, ambiguity detection, ticker/history
resolution, transcript surfacing, and follow-up suggestions. All pure over their
inputs -- none call the ``get_llm`` seam (structured calls stay in graph.py), so
this module has no runtime dependency on graph.py (``AgentState`` is a
type-only import).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError
from shared.retrieval import NEWS_BODY_SNIPPET_CHARS
from shared.tickers import TICKERS

from agent.comparison import ComparisonAnswer, LeanComparisonAnswer, LeanComparisonRow
from agent.conversational import (
    ConversationalAnswer,
    coerce_suggestions,
    is_answerable_suggestion,
)
from agent.disclaimer import DISCLAIMER
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.intent import (
    _EXPLORATION_TRIGGERS,
    Intent,
    extract_tickers,
    underspecified_gesture,
)
from agent.policy import (
    _MAX_COMPARISON_TICKERS,
    _MIN_COMPARISON_TICKERS,
    _SHORT_CIRCUIT_INTENTS,
    _TICKER_REQUIRING_INTENTS,
    INTENT_POLICIES,
    OPTIONAL_TOOLS,
    AmbiguityKind,
    SearchToolFn,
    ToolFn,
    _intent_reads_corpus,
)
from agent.prompts import (
    HISTORY_TURN_LIMIT,
    RETRIEVED_EARNINGS_HEADING,
    RETRIEVED_NEWS_HEADING,
    ConversationMessage,
    trim_message_history,
)
from agent.quick_fact import QuickFactAnswer
from agent.structured import ThesisPlan
from agent.thesis import Thesis

logger = logging.getLogger("agent.graph")

if TYPE_CHECKING:
    from agent.graph import AgentState


_MAX_TOOL_ATTEMPTS = 2  # first try + one retry
# QNT-300 (B-6): each report endpoint is a serial HTTP round trip measured at
# ~0.2-1.0s (news ~0.2s, company/technical/fundamental ~0.6-1.1s), so a 4-tool
# thesis gather ran ~2.9s and a rich 2-ticker comparison ~5.9s serially -- both
# far above ADR-007's "parallelise where possible" motivation. Fetch the planned
# tools concurrently on a bounded pool (<= plan size, capped) so the round trips
# overlap; the cap keeps concurrent connections against the report endpoints
# modest even on the widest plan.
_MAX_GATHER_WORKERS = 4
_EXPLORATION_EXCLUSIONS: tuple[str, ...] = (
    # These are named lens or warm-follow-up requests. Let the existing
    # focused/followup paths handle them so exploration only owns broad scans.
    "news angle",
    "fundamental angle",
    "technical angle",
    "valuation angle",
    "chart angle",
    "headline",
    "catalyst",
    "drill into",
    "dig into",
    "go deeper",
)
_EXPLORATION_NAMED_LENS_TERMS: tuple[str, ...] = (
    "technically",
    "technical",
    "fundamentally",
    "fundamental",
    "valuation",
    "chart",
    "news angle",
    "headline",
    "headlines",
    "catalyst",
    "catalysts",
)


def _is_exploratory_question(question: str) -> bool:
    """Return True for the narrow QNT-215 exploration trigger set."""
    lowered = question.lower()
    return any(trigger in lowered for trigger in _EXPLORATION_TRIGGERS) and not any(
        trigger in lowered for trigger in _EXPLORATION_EXCLUSIONS
    )


def _has_exploration_anchor(
    question: str,
    *,
    has_prior_turn: bool,
) -> bool:
    """Exploration needs an explicit ticker in the question or prior context."""
    return bool(extract_tickers(question) or has_prior_turn)


def _has_named_exploration_lens(question: str) -> bool:
    """Return True when an exploratory phrase also names a specific lens."""
    lowered = question.lower()
    return any(term in lowered for term in _EXPLORATION_NAMED_LENS_TERMS)


def _should_route_exploration(
    intent: Intent,
    question: str,
    *,
    has_prior_turn: bool,
) -> bool:
    """Return True when classify should commit to the exploration shape."""
    if intent in _SHORT_CIRCUIT_INTENTS or intent in {"quick_fact", "comparison"}:
        return False
    return (
        _is_exploratory_question(question)
        and not _has_named_exploration_lens(question)
        and _has_exploration_anchor(question, has_prior_turn=has_prior_turn)
    )


def _minimum_exploration_tools(question: str, available: list[str]) -> int:
    """Broad exploratory asks need a second lens before synthesis."""
    if not available:
        return 0
    return min(2, len(available))


def _is_news_led_exploration(question: str) -> bool:
    """Timely broad scans should start from recent developments."""
    lowered = question.lower()
    return any(
        phrase in lowered
        for phrase in (
            "what's interesting",
            "what is interesting",
            "interesting about",
            "what should i watch",
            "this week",
            "next week",
            "watch",
        )
    )


def _deterministic_exploration_plan(question: str, available: list[str]) -> list[str]:
    """QNT-220 (#4): deterministic broad-exploration tool plan (0 LLM calls).

    The QNT-215 supervisor looped the LLM for one tool decision at a time but was
    content-blind -- ``_build_exploration_prompt`` only ever passed the tool
    *names* gathered so far, never the report *bodies* -- so the surrounding
    deterministic guardrail (min-two-lenses, news-first-when-timely, dedup) is
    what actually shaped the plan. This encodes that guardrail directly: a broad
    scan pulls the minimum complementary lenses, news-first when the ask is
    timely. It reproduces the loop's plans on the exploration goldens while
    cutting up to three LLM calls off the most expensive turn type.
    """
    if not available:
        return []
    if _is_news_led_exploration(question):
        preferred = ("news", "technical", "fundamental", "company")
    else:
        preferred = ("company", "news", "technical", "fundamental")
    ordered = [name for name in preferred if name in available]
    ordered += [name for name in available if name not in ordered]
    return ordered[: _minimum_exploration_tools(question, available)]


def _exploration_rationale(question: str, plan: list[str]) -> str | None:
    """One analyst-voice sentence describing a deterministic exploration scan."""
    if not plan:
        return None
    lenses = ", ".join(plan)
    if _is_news_led_exploration(question):
        return f"Timely broad scan, news-first across {lenses}."
    return f"Broad exploratory scan across {lenses}."


def _tools_from_thesis_plan(thesis_plan: ThesisPlan, available: list[str]) -> list[str]:
    """Filter a structured thesis plan to registered tools, preserving registry order."""
    chosen = set(thesis_plan.tools)
    plan = [tool for tool in available if tool in chosen]
    if "company" in available and "company" not in plan:
        plan.insert(0, "company")
    return plan if len(plan) >= 2 else list(available)


def _tools_from_folded_picks(picks: list[str], available: list[str]) -> list[str] | None:
    """QNT-327 (v3 G-6): filter classify's folded ``report_picks`` to a valid plan.

    Mirrors :func:`_tools_from_thesis_plan` (registry-order, force ``company``) but
    is deliberately STRICTER on the empty-hand case: it returns ``None`` -- not the
    over-fetch-everything fallback -- when the picks can't form a valid >=2-tool
    plan, so plan_node falls back to the dedicated ThesisPlan call rather than
    silently fetching all reports off a degenerate classify pick. The picks arrive
    raw from the classify LLM (permissive :class:`agent.intent.IntentDecision`
    field), so off-list tokens are dropped here, the same way search_query is
    sanitized downstream of the classifier.
    """
    chosen = {p for p in picks if p in available}
    plan = [tool for tool in available if tool in chosen]
    if "company" in available and "company" not in plan:
        plan.insert(0, "company")
    return plan if len(plan) >= 2 else None


def _parse_plan(raw: str, available: list[str], intent: Intent = "thesis") -> list[str]:
    """Return the subset of ``available`` named in ``raw``, preserving the
    order in ``available``. Falls back to the full list if parsing yields
    nothing — we'd rather over-fetch than strand the synthesize node.

    QNT-175: enforces the ``company`` rule from the plan prompt as code, not
    just as a textual bias the LLM can ignore. ``thesis`` and ``comparison``
    paths always pull ``company`` when it's available (the static profile
    grounds qualitative claims); ``quick_fact`` always drops it (a one-metric
    answer never reaches for the description / competitor list).
    """
    tokens = {t.strip().lower() for t in raw.replace("\n", ",").split(",") if t.strip()}
    chosen = [t for t in available if t in tokens]
    if not chosen:
        chosen = list(available)
    if "company" in available:
        if intent in ("thesis", "comparison") and "company" not in chosen:
            chosen = [t for t in available if t == "company" or t in chosen]
        elif intent == "quick_fact":
            chosen = [t for t in chosen if t != "company"]
    return chosen


def _confidence_from_reports(reports: dict[str, str], plan: list[str]) -> float:
    """Coverage factor = fraction of planned reports actually gathered."""
    if not plan:
        return 0.0
    present = sum(1 for name in plan if name in reports and not _is_tool_error(reports[name]))
    return round(present / len(plan), 2)


def _is_tool_error(result: str) -> bool:
    """Stable agent.tools error prefix."""
    return result.startswith("[error]")


def _call_with_retry(tool: ToolFn, ticker: str, name: str) -> tuple[str | None, str | None]:
    """Return (result, error). Retries up to ``_MAX_TOOL_ATTEMPTS`` on exception."""
    last_error: str | None = None
    for attempt in range(1, _MAX_TOOL_ATTEMPTS + 1):
        try:
            return tool(ticker), None
        except Exception as exc:  # noqa: BLE001 — tool errors must not crash the graph
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "gather %s: tool=%s attempt=%d/%d failed: %s",
                ticker,
                name,
                attempt,
                _MAX_TOOL_ATTEMPTS,
                last_error,
            )
    return None, last_error


def _gather_reports(
    ticker: str, plan: list[str], tools: dict[str, ToolFn]
) -> tuple[dict[str, str], dict[str, str]]:
    """Drive the planned tools and return ``(reports, errors)``.

    Optional tools (``OPTIONAL_TOOLS``) are dropped silently on both the
    missing-from-map and retry-exhaustion paths so a routine news outage
    doesn't make the synthesize prompt apologise. Required tools surface in
    ``errors`` either way. Factored out of the gather node closure so the
    branching can be unit-tested without compiling a graph.

    QNT-300 (B-6): the planned tools are fetched concurrently on a bounded
    ThreadPoolExecutor (workers <= plan size, capped at ``_MAX_GATHER_WORKERS``)
    because each is an independent HTTP round trip. This collapses ONE ticker's
    gather (e.g. a 4-tool thesis, ~2.9s serial -> ~0.9s). The comparison caller
    still loops tickers sequentially, so a 2-ticker gather is two parallel
    batches (~1.7s), not one -- capping concurrent connections per the ticket's
    "workers = plan size, cap 4" note rather than fanning all 8 calls at once.
    The retry / optional-drop / error-map contract is unchanged: results are
    assembled in ``plan`` order in a second pass, so ``reports`` and ``errors``
    are byte-for-byte identical to the old serial loop -- only the wall-clock
    time to gather them differs. ``_call_with_retry`` swallows every tool
    exception, so no future raises.
    """
    # Kick off every callable planned tool at once; a tool absent from the map
    # needs no I/O and is resolved in the plan-order pass below.
    runnable: list[tuple[str, ToolFn]] = [
        (name, tools[name]) for name in plan if tools.get(name) is not None
    ]
    results: dict[str, tuple[str | None, str | None]] = {}
    if runnable:
        with ThreadPoolExecutor(max_workers=min(len(runnable), _MAX_GATHER_WORKERS)) as pool:
            futures = {
                pool.submit(_call_with_retry, tool, ticker, name): name for name, tool in runnable
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

    reports: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in plan:
        optional = name in OPTIONAL_TOOLS
        if name not in results:  # tool was not registered in the map
            if not optional:
                errors[name] = "tool-not-registered"
            continue
        result, error = results[name]
        if result is None:
            if not optional:
                errors[name] = error or "failed-after-retries"
            continue
        if _is_tool_error(result):
            if not optional:
                errors[name] = result
            continue
        reports[name] = result
    return reports, errors


def _gather_reports_multi(
    tickers: list[str], plan: list[str], tools: dict[str, ToolFn]
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """QNT-321 (G-3): gather the same ``plan`` for MULTIPLE tickers on ONE
    shared bounded pool, and return ``(reports_by_ticker, errors)``.

    The comparison caller used to loop tickers and call :func:`_gather_reports`
    once per ticker -- N sequential parallel batches, so a rich 2-ticker gather
    ran as two ~0.9s batches (~1.7s). The QNT-300 worker cap is about concurrent
    connections, not batches, so fanning every ``(ticker, tool)`` pair onto one
    pool capped at ``_MAX_GATHER_WORKERS`` holds the same max-4-in-flight bound
    while overlapping across tickers, collapsing the 2-ticker gather to ~0.9s.

    ``reports_by_ticker`` and the ticker-prefixed ``errors`` map are byte-for-
    byte identical to the serial per-ticker loop: results are assembled in
    ``tickers`` x ``plan`` order in a second pass, and the same retry /
    optional-drop / error-map contract as :func:`_gather_reports` applies per
    ticker (error keys are ``f"{ticker}.{tool}"``). ``_call_with_retry`` swallows
    every tool exception, so no future raises.
    """
    # Kick off every (ticker, planned-tool) pair at once; a tool absent from the
    # map needs no I/O and is resolved in the plan-order pass below.
    runnable: list[tuple[str, str, ToolFn]] = [
        (ticker, name, tools[name])
        for ticker in tickers
        for name in plan
        if tools.get(name) is not None
    ]
    results: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    if runnable:
        with ThreadPoolExecutor(max_workers=min(len(runnable), _MAX_GATHER_WORKERS)) as pool:
            futures = {
                pool.submit(_call_with_retry, tool, ticker, name): (ticker, name)
                for ticker, name, tool in runnable
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

    reports_by_ticker: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    for ticker in tickers:
        reports: dict[str, str] = {}
        for name in plan:
            optional = name in OPTIONAL_TOOLS
            key = (ticker, name)
            if key not in results:  # tool was not registered in the map
                if not optional:
                    errors[f"{ticker}.{name}"] = "tool-not-registered"
                continue
            result, error = results[key]
            if result is None:
                if not optional:
                    errors[f"{ticker}.{name}"] = error or "failed-after-retries"
                continue
            if _is_tool_error(result):
                if not optional:
                    errors[f"{ticker}.{name}"] = result
                continue
            reports[name] = result
        reports_by_ticker[ticker] = reports
    return reports_by_ticker, errors


# QNT-225/276: per-corpus body budget for a folded retrieved hit. A news body is
# a short Finnhub summary -- 280 chars disambiguates an event from a name-drop
# while bounding the added prompt cost (~5 hits/turn). An earnings chunk is up to
# 900 chars (edgar_feeds._CHUNK_MAX_CHARS) of 8-K guidance prose, and the chunk
# itself is the whole reason the earnings corpus exists; the old single 280 cap
# discarded ~two thirds of every retrieved chunk before the LLM saw it, so
# earnings preserves the full chunk. Whole sentences aren't guaranteed; we cut on
# a word boundary and add an ellipsis.
_NEWS_BODY_MAX_CHARS = NEWS_BODY_SNIPPET_CHARS  # QNT-356: pin to the shared budget
_EARNINGS_BODY_MAX_CHARS = 900


def _truncate_body(body: str, max_chars: int = _NEWS_BODY_MAX_CHARS) -> str:
    """Trim a folded hit's body to ``max_chars`` on a word boundary.

    QNT-290: collapses internal whitespace (including embedded blank lines --
    plausible in raw 8-K filing prose, less so in a Finnhub summary) to a
    single space. A retrieved block with an internal blank line would break
    :func:`_strip_retrieved_block`'s boundary heuristic, which relies on the
    first blank line marking the end of the retrieved section -- enforcing
    "no blank line inside a hit" here is what makes that heuristic safe.
    """
    body = " ".join(body.split())
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars].rsplit(" ", 1)[0].rstrip()
    return f"{cut}..."


def _format_search_hits(raw: str, start_id: int = 1) -> str:
    """QNT-222/225: render ``search_news`` JSON rows into a news-report-shaped block.

    ``search_news`` returns ``json.dumps([{headline, source, date, score, url,
    body}, ...])`` on a hit and ``"[]"`` on every degraded path (Qdrant outage,
    HTTP error, empty match set, invalid ticker/query). We render headline +
    date + source as ``"- "`` bullets so the block reads like the canned news
    report the focused-news prompt already consumes (the SSE tool_result
    summary also counts ``"- "`` headline lines). QNT-225: when a row carries ``body`` (the Finnhub
    summary), a truncated copy is indented under the headline so the synthesis
    reads the story, not just the title -- empty for points embedded before
    QNT-225 until they roll out of the 7-day window. Returns ``""`` when there
    is nothing usable so the caller can skip the merge entirely.

    QNT-301: each kept bullet is stamped with a stable ``[Rn]`` tag (n counts
    from ``start_id`` in retrieved_sources order) so the synthesis can anchor a
    claim to a specific retrieved row -- ``(source: news R1)``. The digit is
    glued to the ``R`` so the hallucination detector's left-boundary lookbehind
    never reads it as a numeric claim (see :mod:`agent.evals.hallucination`).
    The tag stays aligned with :func:`_parse_search_sources` because both iterate
    the same rows with the same skip logic and the same ``start_id``.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    lines: list[str] = []
    idx = start_id
    for row in rows:
        if not isinstance(row, dict):
            continue
        # QNT-301: use the same ``or ""`` extraction as _parse_search_sources so a
        # null/missing headline is skipped IDENTICALLY here and there. ``row.get(k,
        # "")`` returns None for an explicit JSON null (default only fires on an
        # absent key), and ``str(None)`` is the truthy "None" -- which would keep a
        # row the parser skips, drifting every subsequent [Rn] tag out of sync with
        # its source id.
        headline = str(row.get("headline") or "").strip()
        if not headline:
            continue
        date = str(row.get("date") or "").strip()
        source = str(row.get("source") or "").strip()
        meta = ", ".join(part for part in (source, date) if part)
        lines.append(f"- [R{idx}] {headline}" + (f" ({meta})" if meta else ""))
        body = _truncate_body(str(row.get("body", "")))
        if body:
            lines.append(f"  {body}")
        idx += 1
    if not lines:
        return ""
    return f"## {RETRIEVED_NEWS_HEADING}\n" + "\n".join(lines)


def _parse_search_sources(raw: str, start_id: int = 1) -> list[dict[str, str]]:
    """QNT-226: extract ``{headline, source, date, url}`` rows from ``search_news`` JSON.

    Mirrors :func:`_format_search_hits` parsing but keeps the structured fields
    (not a markdown block) so the SSE wrapper can surface them as a clickable
    provenance list. ``search_news`` returns ``"[]"`` on every degraded path, so
    a bad/empty payload yields ``[]`` and the caller surfaces no sources. Rows
    with no headline are skipped (nothing to render).

    QNT-301: each row carries the same ``id`` (``R{n}``, from ``start_id``) the
    folded block stamps on its matching bullet, so a claim citing ``(source:
    news R1)`` links to this row in the frontend provenance list.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    sources: list[dict[str, str]] = []
    idx = start_id
    for row in rows:
        if not isinstance(row, dict):
            continue
        headline = str(row.get("headline") or "").strip()
        if not headline:
            continue
        sources.append(
            {
                # QNT-301: stable claim-anchor id, aligned with the [Rn] tag in
                # the folded prompt block.
                "id": f"R{idx}",
                "headline": headline,
                "source": str(row.get("source") or "").strip(),
                "date": str(row.get("date") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                # QNT-263: stamp the corpus so the provenance list distinguishes a
                # news hit from an earnings-release hit (AC2).
                "corpus": "news",
            }
        )
        idx += 1
    return sources


def _format_earnings_hits(raw: str, start_id: int = 1) -> str:
    """QNT-263: render ``search_earnings`` JSON rows into a report-shaped block.

    ``search_earnings`` returns ``json.dumps([{title, section, date, score, url,
    text}, ...])`` on a hit and ``"[]"`` on every degraded path. We render each
    chunk as a ``"- "`` bullet (title + section + date) with the truncated chunk
    text indented under it, mirroring :func:`_format_search_hits` so the block
    folds cleanly into the fundamental report the synthesis already consumes.
    Returns ``""`` when there is nothing usable so the caller can skip the merge.

    QNT-301: each kept bullet carries a ``[Rn]`` tag (from ``start_id``) aligned
    with :func:`_parse_earnings_sources` -- see :func:`_format_search_hits`. The
    earnings fold runs after the news fold, so ``start_id`` is offset past the
    news hits and the combined retrieved_sources list stays ``R1..Rn`` gap-free.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    lines: list[str] = []
    idx = start_id
    for row in rows:
        if not isinstance(row, dict):
            continue
        # QNT-301: ``or ""`` extraction mirrors _parse_earnings_sources so a
        # null/missing title+section is skipped identically -- otherwise
        # ``str(None)`` ("None") keeps a row the parser drops and drifts the ids.
        title = str(row.get("title") or "").strip()
        section = str(row.get("section") or "").strip()
        if not title and not section:
            continue
        date = str(row.get("date") or "").strip()
        head = title or section
        meta = ", ".join(part for part in (section if title else "", date) if part)
        lines.append(f"- [R{idx}] {head}" + (f" ({meta})" if meta else ""))
        # QNT-276: earnings preserves close to the full ~900-char chunk (vs the
        # 280-char news budget) so the 8-K guidance paragraph reaches the LLM.
        text = _truncate_body(str(row.get("text", "")), _EARNINGS_BODY_MAX_CHARS)
        if text:
            lines.append(f"  {text}")
        idx += 1
    if not lines:
        return ""
    return f"## {RETRIEVED_EARNINGS_HEADING}\n" + "\n".join(lines)


def _parse_earnings_sources(raw: str, start_id: int = 1) -> list[dict[str, str]]:
    """QNT-263: extract corpus-tagged provenance rows from ``search_earnings`` JSON.

    Mirrors :func:`_parse_search_sources` but maps the earnings-chunk shape onto
    the same ``{headline, source, date, url, corpus}`` provenance dict the SSE
    wrapper already surfaces — ``title`` -> headline, section -> source — and
    tags ``corpus="earnings"`` so the frontend can label which corpus a cited
    hit came from (AC2). ``search_earnings`` degrades to ``"[]"``, so a bad/empty
    payload yields ``[]``.

    QNT-301: carries the same ``id`` (``R{n}``, from ``start_id``) the folded
    earnings block stamps, so a claim citing ``(source: fundamental R3)`` links
    to this row.
    """
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    sources: list[dict[str, str]] = []
    idx = start_id
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        section = str(row.get("section") or "").strip()
        headline = title or section
        if not headline:
            continue
        sources.append(
            {
                "id": f"R{idx}",
                "headline": headline,
                "source": section if title else "8-K earnings release",
                "date": str(row.get("date") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                "corpus": "earnings",
            }
        )
        idx += 1
    return sources


def _strip_retrieved_block(text: str | None, heading: str) -> str:
    """Remove a previously-folded ``## {heading}`` block from persisted report text.

    QNT-290 AC2: a flagged followup folds onto the checkpointer-hydrated
    ``reports`` dict, not a freshly-fetched one, so a prior turn's fold can
    already be sitting at the front of the text. Without stripping it first,
    each chained followup would stack a new retrieved block on top of the
    last one instead of replacing it. The retrieved block is always folded
    as the prefix (``f"{hits}\n\n{existing}"``) and never contains a blank
    line internally (one bullet per hit, no separator row), so the first
    blank line marks the boundary back to the original report text.
    """
    if not text:
        return ""
    marker = f"## {heading}\n"
    if not text.startswith(marker):
        return text
    split_at = text.find("\n\n", len(marker))
    return text[split_at + 2 :] if split_at != -1 else ""


def _fold_news_hits(
    reports: dict[str, str], raw: str, start_id: int = 1
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Fold ``search_news`` JSON hits into ``reports["news"]``.

    Shared by the cold-turn gather path and the QNT-290 followup RAG branch.
    Replaces (not stacks onto) any block left by an earlier fold on the same
    key -- see :func:`_strip_retrieved_block` -- so chained followups can't
    duplicate the retrieved section (AC2). Returns ``reports`` unchanged and
    ``[]`` sources when there is nothing usable to fold.

    QNT-301: ``start_id`` seeds the ``[Rn]`` claim-anchor ids; news folds first
    (``start_id=1``), earnings offsets past the news hit count.
    """
    hits = _format_search_hits(raw, start_id)
    if not hits:
        return reports, []
    base = _strip_retrieved_block(reports.get("news"), RETRIEVED_NEWS_HEADING)
    updated = {**reports, "news": f"{hits}\n\n{base}" if base else hits}
    return updated, _parse_search_sources(raw, start_id)


def _fold_earnings_hits(
    reports: dict[str, str], raw: str, start_id: int = 1
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Fold ``search_earnings`` JSON hits into ``reports["fundamental"]``.

    Sibling of :func:`_fold_news_hits` for the equity_earnings corpus.
    """
    hits = _format_earnings_hits(raw, start_id)
    if not hits:
        return reports, []
    base = _strip_retrieved_block(reports.get("fundamental"), RETRIEVED_EARNINGS_HEADING)
    updated = {**reports, "fundamental": f"{hits}\n\n{base}" if base else hits}
    return updated, _parse_earnings_sources(raw, start_id)


# QNT-291: fold signature every retrieval tool shares -- (reports, raw JSON,
# start_id) -> (reports with the hit block folded in, provenance rows). Both
# ``_fold_news_hits`` and ``_fold_earnings_hits`` already match it.
RetrievalFold = Callable[[dict[str, str], str, int], tuple[dict[str, str], list[dict[str, str]]]]


@dataclass(frozen=True)
class RetrievalSpec:
    """Declarative contract for a semantic-retrieval fold tool (QNT-291).

    Ends the per-tool side channel that ``search_news`` (QNT-222) and
    ``search_earnings`` (QNT-263) each had to grow: a typed alias, a
    ``build_graph`` kwarg, a ``needs_*_search`` state flag, a hardcoded
    gather branch (in BOTH the cold and followup paths), and an SSE
    instrumentation seam. ``gather_node`` now iterates ``RETRIEVAL_SPECS``
    instead of hand-writing one branch per corpus, so a third retrieval
    corpus (filings, transcripts, ...) is one ``RETRIEVAL_SPECS`` entry plus
    wiring its callable into ``build_graph`` -- no new gather branch, no new
    kwarg, no new instrument call.

    Fields:

    * ``name`` -- stable id, also the SSE ``tool_name`` (panel label +
      trace) the instrumentation wrapper stamps.
    * ``flag`` -- the ``AgentState`` key the classifier sets to request this
      corpus (``needs_news_search`` / ``needs_earnings_search``). The gate
      fires only when it is truthy.
    * ``corpus`` -- the ``rag_corpora`` member gated via
      :func:`_intent_reads_corpus` against the QNT-288 policy table, so only
      intents whose synthesis reads this corpus fire the search.
    * ``fold`` -- how a raw JSON payload merges into ``reports`` and yields
      provenance rows (:data:`RetrievalFold`).
    * ``hit_noun`` -- unit word for the SSE result summary ("headlines" /
      "excerpts") so each corpus reads distinctly in the trace.

    Registry ORDER is fold order: ``RETRIEVAL_SPECS`` lists news before
    earnings so the ``[Rn]`` provenance ids stay news-first, R1..Rn gap-free
    (QNT-301), exactly as the old hardcoded sequence produced them.
    """

    name: str
    flag: str
    corpus: str
    fold: RetrievalFold
    hit_noun: str

    def fires(self, tool: SearchToolFn | None, state: AgentState) -> bool:
        """True when this corpus should be searched on the current turn.

        Reproduces the old per-corpus gate byte-for-byte:
        ``state[flag] and intent in _<CORPUS>_SEARCH_INTENTS and tool is not
        None`` -- with ``intent in _<CORPUS>_SEARCH_INTENTS`` expressed as
        :func:`_intent_reads_corpus`. Works for the followup path too:
        ``intent`` is ``"followup"`` there and followup's policy lists both
        corpora, matching the old hardcoded ``"followup" in
        _<CORPUS>_SEARCH_INTENTS`` check.
        """
        intent = state.get("intent", "thesis")
        return bool(
            tool is not None and state.get(self.flag) and _intent_reads_corpus(intent, self.corpus)
        )


NEWS_RETRIEVAL = RetrievalSpec(
    name="news_search",
    flag="needs_news_search",
    corpus="news",
    fold=_fold_news_hits,
    hit_noun="headlines",
)
EARNINGS_RETRIEVAL = RetrievalSpec(
    name="earnings_search",
    flag="needs_earnings_search",
    corpus="earnings",
    fold=_fold_earnings_hits,
    hit_noun="excerpts",
)
# QNT-291: the retrieval registry. News before earnings preserves the
# provenance-id order (QNT-301). Extend this tuple to add a corpus.
RETRIEVAL_SPECS: tuple[RetrievalSpec, ...] = (NEWS_RETRIEVAL, EARNINGS_RETRIEVAL)


def _build_lean_comparison(
    metrics_json: str | None, tickers: list[str]
) -> LeanComparisonAnswer | None:
    """QNT-224: parse the lean comparison-metrics JSON into a structured answer.

    ``metrics_json`` is the ``{"rows": [...]}`` text gather stashed from the API
    (already in requested-ticker order). Returns None on a missing / malformed /
    empty payload so synthesize can redirect. No arithmetic, no LLM — each row
    is a pre-formatted metrics row copied straight from the API (ADR-003).
    """
    if not metrics_json:
        return None
    try:
        payload = json.loads(metrics_json)
    except (ValueError, TypeError):
        logger.warning("lean comparison: metrics JSON not parseable")
        return None
    raw_rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    rows: list[LeanComparisonRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        try:
            rows.append(LeanComparisonRow.model_validate(raw))
        except ValidationError:
            logger.warning("lean comparison: row failed validation: %r", raw)
            return None
    if len(rows) < _MIN_COMPARISON_TICKERS:
        return None
    return LeanComparisonAnswer(rows=rows)


def _with_coerced_suggestions(
    answer: ConversationalAnswer, *, hint: str | None
) -> ConversationalAnswer:
    """Return ``answer`` with its suggestions normalised to the QNT-244 contract.

    Keeps the LLM-generated prose untouched; only the clickable suggestions are
    validated/replaced. Returns the same object when nothing changed so the
    common (already-valid) path avoids a needless copy.
    """
    coerced = coerce_suggestions(answer.suggestions, hint=hint)
    if coerced == answer.suggestions:
        return answer
    return answer.model_copy(update={"suggestions": coerced})


def _detect_ambiguity(
    intent: Intent,
    question: str,
    *,
    has_prior_turn: bool,
    has_context_ticker: bool = False,
    context_ticker: str | None = None,
    history: list[ConversationMessage] | None = None,
) -> AmbiguityKind | None:
    """Return the kind of ambiguity in ``question`` for ``intent``, or None.

    QNT-212: heuristic-only check fired by classify_node right after the
    intent resolves. The three triggers map directly to the AC1 scenarios:

    * comparison + no named ticker ⇒ ``needs_second_ticker``. One named
      ticker is enough when a URL-context ticker or prior turn can supply
      the other side, e.g. /ticker/NVDA + "compare to AAPL". This reverses
      the earlier QNT-212 pin by product decision in QNT-233.
    * thesis / focused / quick_fact + no ticker named + no prior turn ⇒
      ``needs_ticker``. Today this would route to a thesis built around
      whatever placeholder state.ticker the request carries and fabricate
      an answer; the new clarify path asks the user to anchor instead.
    * followup + no prior turn ⇒ ``needs_prior_turn``. The followup
      heuristic already requires has_prior_turn=True so this only fires
      when the LLM classifier returns followup on a cold thread — a
      defensive belt-and-braces against a misbehaving classifier.

    Returns None when the question is unambiguous. The conditional edge in
    build_graph routes None ⇒ plan/synthesize (existing behavior), non-None
    ⇒ clarify.
    """
    question_tickers = extract_tickers(question)
    # QNT-214 follow-up: a bare analysis/compare gesture that names no ticker
    # and has no prior turn is ambiguous regardless of the intent label the
    # classifier returned. The LLM frequently mislabels "what do you think?" /
    # "compare them" as conversational, which would skip the clarify path
    # QNT-212 built for exactly this. Mirror QNT-212: ask back rather than
    # answer on the placeholder ``state.ticker``. A named ticker (handled by
    # the branches below) still gets answered; warm threads keep their prior
    # turn via the ``has_prior_turn`` guard.
    if not question_tickers and not has_prior_turn:
        gesture = underspecified_gesture(question)
        if gesture == "compare":
            return "needs_second_ticker"
        if gesture == "view":
            return "needs_ticker"
    if intent == "comparison":
        # QNT-325 (G-12): the gate defers exactly when the resolver can assemble
        # a pair from the same inputs it will use downstream -- named tickers, the
        # URL-context ticker, and (new) the transcript history. Delegating to the
        # resolver keeps the gate and plan_node from ever disagreeing: a warm
        # thread that named the pair earlier ("compare those two" after discussing
        # NVDA and AMD) resolves and proceeds; a cold "compare them" or a one-ticker
        # thread still yields fewer than two and clarifies.
        resolved = _resolve_comparison_tickers(
            (context_ticker or "") if has_context_ticker else "", question, history
        )
        if len(resolved) < _MIN_COMPARISON_TICKERS:
            return "needs_second_ticker"
    if intent in _TICKER_REQUIRING_INTENTS and not question_tickers and not has_prior_turn:
        return "needs_ticker"
    if intent == "followup" and not has_prior_turn:
        return "needs_prior_turn"
    return None


def _history_tickers(history: list[ConversationMessage] | None) -> list[str]:
    """Distinct tickers the USER named across ``history``, most-recent first (QNT-325).

    Iterates the compact transcript newest turn first and runs
    :func:`extract_tickers` over each USER turn's content, keeping the first
    occurrence of each symbol. "Most recent wins" so a warm-thread gesture
    ("compare those two") fills from the tickers the thread most recently
    discussed. Only user turns count: an assistant answer about AMD that names
    NVDA as valuation colour ("AMD trades at a discount to NVDA") must not seed
    NVDA as a comparison side the user never asked for -- a false pairing is
    worse than a clarify, so the fill draws only on what the user actually typed.
    """
    if not history:
        return []
    seen: list[str] = []
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        for ticker in extract_tickers(str(message.get("content", ""))):
            if ticker not in seen:
                seen.append(ticker)
    return seen


def _resolve_comparison_tickers(
    primary: str,
    question: str,
    history: list[ConversationMessage] | None = None,
) -> list[str]:
    """Return up to 4 tickers to compare, in user-named order (QNT-224).

    Ticker symbols mentioned in ``question`` come first (in the order the
    user wrote them). ``primary`` (the URL-derived ticker the chat panel
    sends) is appended ONLY to reach the two-ticker minimum -- so a question
    like "compare to AAPL" fired from /ticker/NVDA still works -- and never to
    inflate a request the user already filled. Without the ``< _MIN`` guard,
    a 2-named compare from /ticker/NVDA ("compare AAPL and MSFT") would gain a
    third (NVDA) and silently flip from the rich 2-ticker card to a lean 3-way
    that includes a ticker the user never named. The list is capped at
    ``_MAX_COMPARISON_TICKERS`` (4): 2 takes the rich four-aspect bundle,
    3-4 the lean metrics table. Five or more named tickers are handled
    upstream (plan_node) as a conversational redirect, so they never reach
    this cap.

    QNT-325 (G-12): when the question and URL context still leave the pair
    short, fill the remaining gap from transcript ``history`` (most-recent
    distinct tickers, deduped against what is already chosen), mirroring how
    :func:`_resolve_single_ticker_context` inherits the prior analysis ticker
    for followups. A bare gesture ("compare those two") names no ticker, so the
    URL ``primary`` is NOT one of "those two" unless it was actually discussed
    (then it is already in ``history``); history is therefore the only fill
    source for a gesture, and clarify stays the fallback when history yields
    fewer than two. A false pairing is worse than a clarify, so this never
    fabricates a second side the transcript does not support.
    """
    chosen: list[str] = list(extract_tickers(question))
    primary_upper = primary.upper()
    if (
        chosen
        and primary_upper in TICKERS
        and primary_upper not in chosen
        and len(chosen) < _MIN_COMPARISON_TICKERS
    ):
        chosen.append(primary_upper)
    if len(chosen) < _MIN_COMPARISON_TICKERS:
        for ticker in _history_tickers(history):
            if ticker not in chosen:
                chosen.append(ticker)
                if len(chosen) >= _MIN_COMPARISON_TICKERS:
                    break
    return chosen[:_MAX_COMPARISON_TICKERS]


def _followup_is_metric_ask(question: str) -> bool:
    """Return True if a followup question targets a specific metric.

    Reuses the same quick-fact token list the classifier heuristic uses
    (RSI, P/E, EPS, volume, etc.). A hit means the followup should still
    produce a QuickFactAnswer card; a miss routes to the narrative-only
    path so narrate owns the response and no quick_fact event fires.
    """
    from agent.intent import _QUICK_FACT_TOKENS, _matches_any

    return _matches_any(question.lower(), _QUICK_FACT_TOKENS) is not None


def _history_before_current(
    messages: list[ConversationMessage] | None,
    question: str,
    *,
    max_turns: int = HISTORY_TURN_LIMIT,
) -> list[ConversationMessage]:
    """Return prior transcript, excluding the current user turn if appended.

    ``max_turns`` bounds how many prior user/assistant turns reach the prompt
    prefix; callers pass an intent-aware value via :func:`_history_budget`
    (QNT-232 #13). The routing-only callsite keeps the full default so prior-turn
    detection stays accurate.
    """
    history = trim_message_history(messages, max_turns=max_turns)
    if (
        history
        and history[-1].get("role") == "user"
        and history[-1].get("content") == question.strip()
    ):
        return history[:-1]
    return history


def _prior_turn_context(state: AgentState, question: str) -> tuple[list[ConversationMessage], bool]:
    """Return transcript context plus the canonical prior-turn boolean.

    QNT-307: the ``answer`` union is the prior-turn signal (was the legacy
    ``thesis`` slot). Runs at classify time, before ``prior_answer`` is set, so it
    reads the checkpointer-hydrated ``answer`` from the earlier turn directly.
    """
    history = _history_before_current(state.get("messages"), question)
    return history, bool(history or state.get("reports") or state.get("answer"))


def _append_user_message(
    messages: list[ConversationMessage] | None,
    question: str,
) -> list[ConversationMessage]:
    """Append the current user turn to the compact transcript."""
    content = question.strip()
    if not content:
        return trim_message_history(messages)
    return trim_message_history(
        [*trim_message_history(messages), {"role": "user", "content": content}]
    )


def _resolve_single_ticker_context(
    *,
    current_ticker: str,
    question: str,
    intent: Intent,
    prior_ticker: str | None,
) -> str:
    """Return the ticker single-name analytical paths should use.

    A question-named ticker beats the URL-context ticker for single-ticker
    intents. Comparison keeps its separate resolver because two-or-more names
    have distinct semantics there. Bare followups inherit the last analytical
    ticker stored in the checkpoint so a rebased turn stays coherent.

    QNT-245 boundary (older-turn re-gather): a bare followup inherits ONLY the
    MOST-RECENT analysis_ticker. Within one ticker-agnostic conversation thread,
    a followup that gestures at an EARLIER turn's ticker ("go back to NVDA"
    after the subject moved to AMZN) names NVDA, so it routes as a fresh NVDA
    ask and RE-GATHERS — it does not reuse NVDA's prior reports. ``reports`` /
    ``reports_by_ticker`` / ``answer`` are last-write-wins in the checkpoint
    (gather overwrites them on each non-followup turn; only the followup branch
    in plan_node deliberately preserves them), so the older ticker's reports are
    gone once a newer single-ticker turn lands.
    This is accepted by design: we re-gather rather than maintain a per-ticker
    report cache. Cross-ticker continuity is provided by the shared thread +
    transcript, not by cached per-ticker reports.
    """
    current = current_ticker.upper()
    named = extract_tickers(question)
    if intent != "comparison" and len(named) == 1:
        return named[0]
    prior = (prior_ticker or "").upper()
    if intent == "followup" and not named and prior in TICKERS:
        return prior
    return current


def _strip_disclaimer(markdown: str) -> str:
    """Remove the rendered footer before narrate treats markdown as substrate."""
    return markdown.replace(DISCLAIMER, "").strip()


def _pick_payload(state: AgentState) -> object | None:
    """Select the structured payload a turn's surfaces reason over (QNT-309).

    Shared by ``narrate_node`` (the substrate the analyst voice narrates over)
    and :func:`_assistant_surface` (the transcript anchor) so the two sibling
    idioms can never silently diverge again -- before QNT-309 narrate picked
    ``prior_answer`` first while ``_assistant_surface`` picked ``answer`` first,
    disagreeing only on a followup metric turn.

    Followup is the one intent that reacts to the PRIOR turn's answer rather than
    this turn's: classify snapshots the earlier Thesis into ``prior_answer``, a
    narrative-only followup carries ``answer=None``, and a metric-ask followup
    narrates over the earlier thesis (not its own compact QuickFactAnswer card).
    So ``prior_answer`` wins for followup; every other intent reads THIS turn's
    ``answer``. The guard is on the intent, not on ``prior_answer`` being set,
    because classify also carries a hydrated Thesis in ``prior_answer`` on a
    non-followup turn -- gating on the intent keeps those turns on ``answer``.

    Aligning ``_assistant_surface`` onto this precedence intentionally collapses a
    SECOND divergence beyond the followup-metric turn: a non-followup turn whose
    ``answer`` is None -- the focused news/fundamental RAG-drop path
    (``synthesize._synthesize_payload`` returns ``{"answer": None}`` when a search
    fired and hit) -- following a thesis turn. There the OLD ``_assistant_surface``
    (``answer or prior_answer``) fell back to the stale prior Thesis for the
    transcript anchor, while narrate spoke from the dropped report body, not the
    Thesis. Returning None here makes the transcript anchor track what narrate
    actually narrated (the report / narrative), which is the whole point of the
    shared precedence. Pinned by ``test_non_followup_answer_none_does_not_borrow_prior``.
    """
    intent = state.get("intent", "thesis")
    return (state.get("prior_answer") if intent == "followup" else None) or state.get("answer")


def _assistant_surface(state: AgentState, narrative: str | None) -> str | None:
    """Compact assistant transcript entry for the completed turn.

    QNT-294 / QNT-307: dispatches on the single ``answer`` union instead of the
    old seven-branch slot ladder. QNT-309: the substrate is picked by the shared
    :func:`_pick_payload` so the transcript anchor tracks whatever narrate spoke
    over (a followup narrative-only turn carries ``answer=None`` and reuses the
    prior turn's ``prior_answer``). The isinstance order preserves the old
    ladder's priority (conversational first, thesis last).
    """
    prefix = narrative.strip() if narrative else ""

    payload = _pick_payload(state)
    if isinstance(payload, ConversationalAnswer):
        return (prefix or str(getattr(payload, "answer", ""))).strip() or None
    if isinstance(payload, QuickFactAnswer):
        answer = getattr(payload, "answer", "")
        ref = "Structured payload: quick_fact"
        return "\n".join(part for part in (prefix or str(answer), ref) if part).strip()
    if isinstance(payload, FocusedAnalysis):
        focus = getattr(payload, "focus", "focused")
        summary = getattr(payload, "summary", "")
        ref = f"Structured payload: focused {focus}"
        return "\n".join(part for part in (prefix or str(summary), ref) if part).strip()
    if isinstance(payload, ExplorationAnswer):
        headline = getattr(payload, "headline", "")
        ref = "Structured payload: exploration"
        return "\n".join(part for part in (prefix or str(headline), ref) if part).strip()
    if isinstance(payload, ComparisonAnswer):
        differences = getattr(payload, "differences", "")
        return "\n".join(
            part for part in (prefix or str(differences), "Structured payload: comparison") if part
        ).strip()
    if isinstance(payload, LeanComparisonAnswer):
        # QNT-224: the lean shape has no differences field — the spoken
        # contrast is the narrative. Carry it (or a payload marker) so a
        # followup turn has a transcript anchor.
        ref = "Structured payload: comparison_lean"
        return "\n".join(part for part in (prefix, ref) if part).strip()
    if isinstance(payload, Thesis):
        verdict = getattr(payload, "verdict", "thesis")
        rationale = getattr(payload, "verdict_rationale", "")
        ref = f"Structured payload: thesis verdict={verdict}"
        return "\n".join(part for part in (prefix or str(rationale), ref) if part).strip()

    return prefix or None


def _append_assistant_message(
    state: AgentState,
    narrative: str | None,
) -> list[ConversationMessage]:
    """Append the assistant surface for this turn and trim to the history limit."""
    surface = _assistant_surface(state, narrative)
    if not surface:
        return trim_message_history(state.get("messages"))
    return trim_message_history(
        [*trim_message_history(state.get("messages")), {"role": "assistant", "content": surface}]
    )


def _hint_from_intent(intent: Intent) -> str | None:
    """Bucket the intent into a hint label for ``domain_redirect``.

    QNT-288: reads ``IntentPolicy.suggestion_hint`` from ``INTENT_POLICIES``.
    The redirect's suggestion picker uses the hint to bias toward questions
    matching the user's evident shape. Hints must match a label in
    :data:`agent.conversational._SUGGESTION_BANK` — the bank is keyed by
    report-type / shape (``technical``, ``fundamental``, ``news``,
    ``thesis``, ``comparison``), not by intent name.
    """
    return INTENT_POLICIES[intent].suggestion_hint


# QNT-298: static "compare against" pick for the ``{partner}`` slot in a
# single-ticker intent's follow-up templates. Not a scored comparability
# metric -- a fixed, deterministic second ticker (sector/competitive line an
# analyst would reach for first) so a "Compare X vs Y" chip always names two
# covered symbols with zero LLM calls. Every ``TICKERS`` entry has a key and
# every value is itself a distinct covered ticker (asserted in tests).
_COMPARISON_PARTNER: dict[str, str] = {
    "NVDA": "AMD",
    "AMD": "NVDA",
    "INTC": "AMD",
    "MU": "NVDA",
    "AAPL": "MSFT",
    "MSFT": "GOOGL",
    "GOOGL": "META",
    "META": "GOOGL",
    "AMZN": "MSFT",
    "TSLA": "NVDA",
}


def analytical_followup_suggestions(
    intent: Intent,
    ticker: str,
    comparison_tickers: list[str] | None = None,
) -> list[str]:
    """QNT-298: 2-3 deterministic follow-up chips for an analytical card.

    Zero LLM calls -- ``IntentPolicy.followup_templates`` are filled from the
    resolved analysis ``ticker`` and (for the comparison shape) the second
    compared ticker, or (for every other shape) the static
    ``_COMPARISON_PARTNER`` pick. Every filled chip is re-checked against the
    QNT-244 ``is_answerable_suggestion`` guardrail so a malformed template can
    never ship an unanswerable/clarify-routing chip. Returns ``[]`` for
    intents with no ``followup_templates`` entry (conversational, followup).
    """
    templates = INTENT_POLICIES[intent].followup_templates
    if not templates:
        return []
    if intent == "comparison" and comparison_tickers and len(comparison_tickers) >= 2:
        primary, partner = comparison_tickers[0], comparison_tickers[1]
    else:
        primary = ticker
        partner = _COMPARISON_PARTNER.get(ticker, ticker)
    filled = [template.format(ticker=primary, partner=partner) for _, template in templates]
    return [chip for chip in filled if is_answerable_suggestion(chip)]
