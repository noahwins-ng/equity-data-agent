"""LangGraph research agent: classify -> plan -> gather -> synthesize (ADR-007, QNT-149, QNT-156).

The graph is the executive layer of the three-role architecture: it reasons
over pre-computed report strings returned by FastAPI tools and never does
arithmetic or touches the database.

Tools are injected at build time via a ``{name: callable}`` mapping. Tests
pass mock callables; production wiring (QNT-60) passes real HTTP tools.
Keeping tools outside the module makes the graph unit-testable offline.

Pipeline:

1. ``classify`` — pick a response shape from the user's question. Four
   shapes are supported: ``thesis`` (Setup / Bull / Bear / Verdict),
   ``quick_fact`` (short prose + single cited value), ``comparison``
   (per-ticker sections + differences paragraph), and ``conversational``
   (greetings / capability asks / off-domain redirect). Defaults to
   ``thesis`` on any classifier failure so existing eval contracts
   (QNT-67, QNT-128) cannot regress.
2. ``plan`` — pick which report tools to fetch. Bias depends on intent:
   thesis over-fetches, quick_fact narrows, comparison reuses the thesis
   bias for both tickers, conversational skips entirely (no tools needed).
3. ``gather`` — drive the planned tools, retry transient failures, drop
   optional-tool failures silently. For comparison, gathers reports for
   each of the (capped) two tickers.
4. ``synthesize`` — branch on intent. Each path produces its structured
   answer; ANY synthesize-path failure (empty payload, no reports gathered,
   structured-output crash) falls back to a deterministic conversational
   redirect via :func:`agent.conversational.domain_redirect` so the panel
   never sees a stack trace or a blank state.

The synthesized shape lives in a single discriminated-union ``state['answer']``
field (QNT-294) -- a single slot holds exactly one payload per run, so the
"exactly one of" contract is enforced by the type rather than convention.
QNT-307 retired the seven legacy read-compat slots QNT-294 kept during the
migration; the followup path now reads a dedicated ``prior_answer`` channel.
Nodes are module-level functions in ``agent.nodes`` (bound to build-time
``GraphDeps`` here); pure helpers live in ``agent.policy`` / ``agent.structured``
/ ``agent.support`` and are re-exported from this module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError
from shared.tickers import TICKERS  # noqa: F401

from agent.answer import (  # noqa: F401
    ANALYTICAL_ANSWER_TYPES,
    AnswerPayload,
    answer_slot,
    project_answer,
)
from agent.citations import strip_bad_anchors_in_obj  # noqa: F401
from agent.comparison import ComparisonAnswer, LeanComparisonAnswer  # noqa: F401
from agent.conversational import (  # noqa: F401
    ConversationalAnswer,
    domain_redirect,
)
from agent.evals.hallucination import HallucinationResult, extract_numbers
from agent.evals.hallucination import check as check_grounding
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.intent import (  # noqa: F401
    ClassifierSource,
    Intent,
    classify_intent_with_source,
    comparison_axis,
    extract_tickers,
    has_comparison_phrase,
)
from agent.llm import SMALL_NODE_ALIAS, get_llm  # noqa: F401

# QNT-294 (AC1): helpers relocated out of graph.py. Re-imported here so the
# node closures in build_graph resolve them and so external callers/tests that
# ``from agent.graph import <helper>`` keep working (re-export compat). QNT-307
# (AC3) weighed decoupling this + the ``graph_module`` monkeypatch seam and chose
# to defer it -- see ADR-024 (a working test seam is not automatically debt).
from agent.policy import (  # noqa: F401
    _CLARIFY_FALLBACK_REASON,
    _CLARIFY_LEAD_IN,
    _CLARIFY_LEAD_IN_DEFAULT,
    _CLARIFY_SUGGESTION_HINT,
    _COMPACT_COMPANY_INTENTS,
    _FOCUSED_REPORT,
    _FRESH_ANALYTICAL_HISTORY_TURNS,
    _MAX_COMPARISON_TICKERS,
    _MIN_COMPARISON_TICKERS,
    _SHORT_CIRCUIT_INTENTS,
    _TICKER_REQUIRING_INTENTS,
    INTENT_POLICIES,
    OPTIONAL_TOOLS,
    AmbiguityKind,
    ComparisonMetricsToolFn,
    EventEmitter,
    IntentPolicy,
    ReportToolName,
    SearchToolFn,
    ToolFn,
    _history_budget,
    _intent_reads_corpus,
)
from agent.prompts import (  # noqa: F401
    REPORT_TOOLS,
    ConversationMessage,
    build_clarify_prompt,
    build_comparison_prompt,
    build_conversational_prompt,
    build_exploration_prompt,
    build_focused_prompt,
    build_followup_prompt,
    build_narrate_prompt,
    build_quick_fact_prompt,
    build_synthesis_prompt,
)
from agent.quick_fact import QuickFactAnswer
from agent.structured import (  # noqa: F401
    _PROMPT_VERSION,
    ThesisPlan,
    _build_plan_prompt,
    _build_thesis_plan_prompt,
    _coerce,
    _linked_invoke,
    _prompt_version,
)
from agent.support import (  # noqa: F401
    _COMPARISON_PARTNER,
    EARNINGS_RETRIEVAL,
    NEWS_RETRIEVAL,
    RETRIEVAL_SPECS,
    RetrievalFold,
    RetrievalSpec,
    _append_assistant_message,
    _append_user_message,
    _assistant_surface,
    _build_lean_comparison,
    _call_with_retry,
    _confidence_from_reports,
    _detect_ambiguity,
    _deterministic_exploration_plan,
    _exploration_rationale,
    _fold_earnings_hits,
    _fold_news_hits,
    _followup_is_metric_ask,
    _format_earnings_hits,
    _format_search_hits,
    _gather_reports,
    _gather_reports_multi,
    _has_exploration_anchor,
    _has_named_exploration_lens,
    _hint_from_intent,
    _history_before_current,
    _is_exploratory_question,
    _is_news_led_exploration,
    _is_tool_error,
    _minimum_exploration_tools,
    _parse_earnings_sources,
    _parse_plan,
    _parse_search_sources,
    _pick_payload,
    _prior_turn_context,
    _resolve_comparison_tickers,
    _resolve_single_ticker_context,
    _should_route_exploration,
    _strip_disclaimer,
    _strip_retrieved_block,
    _tools_from_folded_picks,
    _tools_from_thesis_plan,
    _truncate_body,
    _with_coerced_suggestions,
    analytical_followup_suggestions,
)
from agent.thesis import Thesis

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

# QNT-383: the single per-shape output budget for the structured synthesize
# calls, consulted by ``_structured_call`` below. Replaces the scattered per-call
# ``max_tokens`` constants (``_COMPARISON_MAX_TOKENS`` / ``_THESIS_MAX_TOKENS``)
# that three tickets (QNT-351, QNT-358, QNT-370) each had to ratchet one at a
# time -- the reactive-sizing-trap where an output cap rots as report content
# grows. Now every answer shape declares its ceiling in one place, and the guard
# test (test_every_answer_shape_has_output_budget) fails if a new union member
# ships without an entry, so a shape cannot silently ride the config default into
# a truncation cliff.
#
# ``max_tokens`` is a CEILING, not a target: a call bills only its actual output,
# and the QNT-351 deterministic fallback still catches a genuine runaway past the
# ceiling (bounded blast radius). Values are ~1.7-2x each shape's live output
# p-high -- reasoning is disabled (litellm_config.yaml), so this is pure
# completion -- measured on Langfuse default-alias generations over
# 2026-07-02..07-18, AFTER QNT-353/354 grew the reports:
#   thesis         med 1304 / p90 1549 / max 1791 (n=72) -> 2500  (QNT-370)
#   comparison     med 1314 / p90 2427 / max 2925 (n=14) -> 3000  (QNT-358, 2-ticker)
#   focused        med  489 / p90  615 / max  709 (n=43) -> 1500
#   exploration    med  450 / p90  465 / max  536 (n= 6) -> 1500
#   quick_fact     max   96                               -> 1500
#   conversational max  204                               -> 1500
# QNT-383 correction: the review-finding premise that focused/exploration sat at
# the 1500 truncation cliff did NOT hold -- both top out well under 1500 with 2x
# headroom, because the cards are structurally bounded (<=5 bullets + <=4 verbatim
# values) and quote rather than expand as reports grow. They keep the 1500
# default, now stated explicitly so this table is the single source of truth.
# LeanComparisonAnswer is built deterministically (ADR-003, no LLM call), so it
# carries no budget (None); it is a key only so the guard test covers every
# registered shape. Re-derive from the live distribution if a shape starts
# pressing its ceiling (Langfuse default-alias generations by intent).
_OUTPUT_BUDGET: dict[type, int | None] = {
    Thesis: 2500,
    QuickFactAnswer: 1500,
    ComparisonAnswer: 3000,
    LeanComparisonAnswer: None,
    ConversationalAnswer: 1500,
    FocusedAnalysis: 1500,
    ExplorationAnswer: 1500,
}


def _structured_call[T: BaseModel](
    schema: type[T],
    prompt: list[Any] | str,
    config: RunnableConfig,
    prompt_name: str,
    *,
    llm: Any | None = None,
    linked: bool = True,
    method: Literal["function_calling", "json_mode", "json_schema"] | None = None,
) -> T | None:
    """Run one structured-output LLM call with the shared retry/coerce ladder (AC5).

    QNT-294: the single owner of the ``with_structured_output(schema) +
    with_retry(stop_after_attempt=2, ValidationError|OutputParserException) ->
    BLE001 -> coerce`` ladder that was duplicated across every ``_synthesize_payload``
    branch plus ``clarify_node`` (and, with a small-alias variant, the thesis
    planner). The retry policy lives here exactly once, so a future branch cannot
    silently drift its ``stop_after_attempt`` or exception tuple.

    Returns the coerced model, or ``None`` on any failure (LLM exception or a
    response that could not be coerced) -- the caller owns the fallback. ``llm``
    defaults to :func:`get_llm`; the planner passes the small-alias LLM. ``linked``
    routes through :func:`_linked_invoke` (prompt-version + native Langfuse prompt
    link) for the prompt-registered synthesize/clarify calls; the planner passes
    ``linked=False`` for a plain ``invoke`` (no registered prompt).

    ``method`` overrides LangChain's default structured-output method. QNT-258
    follow-up: the paid DeepSeek V4 Flash primary occasionally returns bare prose
    instead of the JSON envelope on the conversational ``ConversationalAnswer``
    schema (an inherently conversational task), tripping a json_invalid
    ValidationError that the ladder recovers via the deterministic fallback but
    that Sentry still captures. The conversational/clarify calls pass
    ``method="function_calling"`` so the structured data comes back as tool-call
    args -- a channel the model cannot fill with prose -- eliminating the failure
    mode at the source. ``None`` keeps LangChain's default (json_schema), so the
    validated ``Thesis``/synthesize path is unchanged.
    """
    # QNT-383: size the output ceiling from the per-shape ``_OUTPUT_BUDGET`` table
    # (the default ``llm=None`` path). A caller that passes its own ``llm`` -- the
    # small-alias planner -- owns its budget; its schema is not an answer shape and
    # carries no table entry, so the table never touches it.
    base = llm if llm is not None else get_llm(max_tokens=_OUTPUT_BUDGET.get(schema))
    structured = (
        base.with_structured_output(schema, method=method)
        if method is not None
        else base.with_structured_output(schema)
    )
    structured_llm = structured.with_retry(
        stop_after_attempt=2,
        retry_if_exception_type=(ValidationError, OutputParserException),
    )
    try:
        if linked:
            response = _linked_invoke(structured_llm, prompt, config, prompt_name)
        else:
            response = structured_llm.invoke(prompt, config=config)
    except Exception as exc:  # noqa: BLE001 — every caller degrades to a deterministic fallback
        logger.warning(
            "%s: structured output failed: %s: %s",
            prompt_name,
            type(exc).__name__,
            exc,
        )
        return None
    return _coerce(response, schema)


class AgentState(TypedDict):
    """State carried through the graph.

    ``ticker`` is required at invocation; everything else is filled in by
    nodes as the graph runs. ``intent`` is set by the classify node and
    decides which synthesis branch fires. ``reports`` holds raw report
    strings keyed by tool name (for the primary ticker); ``errors`` records
    tool-name -> error message for any tool that failed after retries.

    Comparison runs add ``comparison_tickers`` (the 2 tickers the user
    asked to contrast, in order) and ``reports_by_ticker`` (per-ticker
    report bundle). The single-ticker ``reports`` dict is still populated
    with the primary ticker's reports so existing consumers (CLI confidence
    line, eval hallucination scorer) keep working.

    The synthesized answer lives in the single ``answer`` discriminated union,
    matching ``intent`` (QNT-294 / QNT-307).
    """

    ticker: str
    analysis_ticker: NotRequired[str]
    question: NotRequired[str]
    intent: NotRequired[Intent]
    classifier_source: NotRequired[ClassifierSource]
    plan: NotRequired[list[str]]
    plan_rationale: NotRequired[str | None]
    reports: NotRequired[dict[str, str]]
    comparison_tickers: NotRequired[list[str]]
    reports_by_ticker: NotRequired[dict[str, dict[str, str]]]
    errors: NotRequired[dict[str, str]]
    # QNT-294 / QNT-307: the single discriminated-union answer payload -- the sole
    # answer write channel (populated only via ``agent.answer.project_answer``, so
    # a node cannot hand-set two shapes; the union enforces exactly-one).
    # ``_assistant_surface``, synthesize's early card emit, narrate, the eval
    # scorers, and the SSE wrapper all read this. QNT-307 retired the seven legacy
    # read-compat slots QNT-294 kept during the migration; old checkpoints that
    # still carry them hydrate harmlessly (unknown channels ignored, missing
    # ``answer`` reads as None).
    answer: NotRequired[AnswerPayload | None]
    # QNT-307: the prior turn's Thesis, snapshotted at the turn boundary by
    # ``classify_node``. Replaces the retired ``thesis`` slot the followup path
    # leaned on and reproduces its lifetime exactly: only a Thesis is carried
    # (non-thesis intents nulled the old slot), and a narrative-only followup
    # preserves the earlier Thesis across the chain. The followup path (synthesize,
    # narrate, ``_assistant_surface``) reads THIS to reason over the earlier turn
    # while it overwrites ``answer`` mid-run. None on a cold thread or after a
    # non-thesis analytical turn. Typed as the union for assignment flexibility,
    # but only ever a Thesis or None in practice.
    prior_answer: NotRequired[AnswerPayload | None]
    # QNT-320 (G-1): synthesize's decision about which substrate narrate speaks
    # from on the paths it returns ``answer=None``. "news" / "fundamental" name
    # the folded report on the focused RAG-drop path; "prior_answer" marks the
    # narrative-only followup (narrate reaches the earlier turn via _pick_payload);
    # None on a card-bearing turn. Written ONLY by synthesize, read ONLY by narrate
    # -- replaces the needs_news_search / needs_earnings_search re-derivation narrate
    # used to mirror off the synthesize-side drop condition.
    narrative_substrate: NotRequired[str | None]
    # QNT-320 (G-2): the routing decision classify_node computed for this turn
    # (clarify / plan / synthesize / explore_supervisor). ``_classify_router`` is a
    # pure read of this key -- the predicate calls that used to live in the router
    # (followup_fires_search + a dead _should_route_exploration re-check) now happen
    # once in classify_node, so the router can never drift from the intent classify
    # actually committed to.
    route: NotRequired[str]
    confidence: NotRequired[float]
    grounding_rate: NotRequired[float]
    grounding_unsupported: NotRequired[list[str]]
    supervisor_iterations: NotRequired[int]
    # QNT-211: streaming analyst-voice paragraph produced by the narrate node
    # AFTER synthesize. Persisted into the checkpointer so a follow-up turn
    # can reference the prior narrative if it wants to. None when narrate
    # was skipped (conversational intent) or when the LLM call failed --
    # the structured card still renders, the bubble degrades.
    narrative: NotRequired[str | None]
    # QNT-216: compact, append-only transcript persisted by the checkpointer.
    # Stores user turns and assistant surface text only; structured payloads
    # are referenced compactly so full card JSON does not bloat prompt prefix.
    messages: NotRequired[list[ConversationMessage]]
    # QNT-212: ambiguity classification produced by classify_node. When set,
    # the conditional edge from classify routes to clarify rather than the
    # normal plan/synthesize path. None ⇒ question was unambiguous.
    ambiguity_kind: NotRequired[AmbiguityKind | None]
    # QNT-222 follow-up: classifier signal for whether to fire the semantic news
    # search (search_news / RAG). Set by classify_node from
    # classify_intent_with_source; read by gather_node. Intent-independent -- a
    # targeted-event ask ("what did the CEO say about the buyback?") gets this
    # even when its intent is quick_fact or thesis rather than news.
    needs_news_search: NotRequired[bool]
    # QNT-226: provenance for the semantic news search. When gather fires
    # search_news (targeted news ask), it stores the retrieved hits here as
    # ``{headline, source, date, url}`` dicts so the SSE wrapper can surface a
    # clickable "Retrieved sources" list to the frontend. Absent when no search
    # ran or it returned nothing. Set by gather_node, read by the SSE endpoint
    # (gated on gather having run so a followup turn doesn't re-emit prior hits).
    retrieved_sources: NotRequired[list[dict[str, str]]]
    # QNT-263 / QNT-280: earnings-corpus routing signal. Set by classify_node
    # from the classify LLM's semantic ``needs_earnings_search`` flag (mirrors
    # needs_news_search; _is_earnings_search keyword decider is the recall
    # floor). When set
    # and the intent reads the fundamental report (_intent_reads_corpus,
    # earnings), gather fires search_earnings over the equity_earnings corpus
    # and folds the
    # hits into reports["fundamental"], tagging each retrieved source corpus=
    # "earnings" so provenance distinguishes the corpus a hit came from.
    needs_earnings_search: NotRequired[bool]
    # QNT-289: self-contained retrieval query produced by the classify LLM
    # alongside needs_news_search/needs_earnings_search -- resolves pronouns/
    # ellipses from history ("what about the buyback?" -> "NVDA buyback") so a
    # warm-thread targeted ask doesn't reach Qdrant as a bare, topic-less
    # string. Guardrailed by intent.sanitize_search_query (length cap +
    # hallucinated-entity rejection) before it lands in state. "" means no
    # rewrite was produced/survived the guardrail; gather falls back to the
    # raw question, which is today's behaviour, so this can only add recall.
    search_query: NotRequired[str]
    # QNT-327 (v3 G-6, spike): the thesis plan pick, folded out of the classify
    # call so a thesis turn drops from four sequential LLM calls to three. Written
    # by classify_node from classify_intent_with_source's ``report_picks`` /
    # ``plan_rationale`` (non-empty ONLY on the llm path for a thesis intent), read
    # by plan_node's thesis branch. When the picks form a valid >=2-tool plan,
    # plan_node consumes them and skips the dedicated ThesisPlan LLM call;
    # otherwise (heuristic/fallback classify, non-thesis intent, or a degenerate
    # pick) it falls back to that call. Per-turn SCRATCH: reset at the turn
    # boundary so a warm-thread turn that classifies via heuristic can't inherit
    # the prior turn's picks out of the checkpointer.
    folded_report_picks: NotRequired[list[str]]
    folded_plan_rationale: NotRequired[str | None]
    # QNT-326 (G-14): demand detector for a future per-ticker comparison fold.
    # comparison's IntentPolicy.rag_corpora is empty (RetrievalSpec.fold cannot
    # address reports_by_ticker yet), so a targeted-event comparison ("compare
    # NVDA and AMD on their antitrust exposure") silently discards the
    # classifier's needs_news_search / needs_earnings_search flags. gather_node
    # records the '+'-joined corpora a comparison turn asked for but couldn't use
    # ("news" / "earnings" / "news+earnings") here; the SSE handler stamps it as
    # a comparison_rag_demand trace tag. Detector only -- no fold is built; the
    # tag is the demand signal for whether one is worth building. classify_node
    # resets it to "" at the turn boundary so a prior comparison turn's demand
    # doesn't bleed across the checkpointer into a later turn's tag.
    comparison_rag_demand: NotRequired[str]
    # QNT-212: ordered list of node names actually visited this turn.
    # Each node writes the FULL accumulated list (not a single element with
    # a reducer) -- a reducer would also accumulate across turns out of the
    # checkpointer, so turn 2 of a followup chain would see turn 1's path
    # prepended. ``_wrap_path`` does the read+append; classify resets at
    # the turn boundary. Surfaces on the SSE done event for latency-path
    # debugging + AC validation.
    intent_path: NotRequired[list[str]]


# QNT-323 (G-4): the per-turn scratch/durable split, declared once as data.
#
# The checkpointer persists AgentState across turns on a warm thread, so a key
# left unwritten this turn hydrates with the PRIOR turn's value. Every key is
# therefore either per-turn SCRATCH (must be cleared at the turn boundary or a
# stale value leaks in) or durable thread MEMORY (must survive). Before this the
# split was a convention scattered across node return dicts and enforced by
# comments: plan_node's followup branch had to return ONLY ``{"plan": []}`` or it
# clobbered the hydrated reports; its conversational/no-tools branches had to
# return the empty resets or a warm thread's stale reports leaked into a fresh
# turn; gather_node and explore_supervisor_node repeated the dance per branch.
# classify_node -- the turn-boundary node (it already resets ``intent_path`` via
# _wrap_path, snapshots ``prior_answer``, appends the user message) -- now owns
# the WHOLE reset via ``_turn_boundary_reset``. Downstream nodes carry only the
# keys they actually produce; a meta-test (tests/agent/test_turn_boundary.py)
# pins that no node other than classify writes a reset for a key it never
# populates.
#
# SCRATCH (cleared every turn, intent-independent): a producer that runs
# overwrites the reset with its real value; on a path that skips the producer,
# the reset value is what the turn ends with -- never the prior turn's. This set
# includes the single-writer keys that leak most quietly: ``plan_rationale``
# (only plan / explore_supervisor write it, and plan's followup branch returns
# ONLY ``{"plan": []}`` -- so a warm-thread followup would otherwise narrate the
# prior turn's rationale, since build_narrate_prompt reads it unconditionally)
# and ``supervisor_iterations`` (only explore_supervisor writes it, and the SSE
# done event reports it every turn).
_SCRATCH_RESET_BASE: dict[str, object] = {
    "plan": [],
    "plan_rationale": None,
    "errors": {},
    "comparison_tickers": [],
    "retrieved_sources": [],
    "confidence": 0.0,
    "grounding_rate": 1.0,
    "grounding_unsupported": [],
    "supervisor_iterations": 0,
    "comparison_rag_demand": "",
    # QNT-327 (v3 G-6): the folded thesis plan pick classify_node forwards to
    # plan_node. Reset here so a heuristic-classified warm-thread turn (no LLM,
    # no picks) can't consume the prior turn's picks out of the checkpointer.
    "folded_report_picks": [],
    "folded_plan_rationale": None,
}
# QNT-349 (R-1/R-2): ``reports`` and ``reports_by_ticker`` are the intent/route-
# CONDITIONAL scratch members -- the durable thread SUBSTRATE. A followup reuses
# the prior turn's checkpointer-hydrated bundle verbatim (the whole point of
# QNT-209/QNT-324), and a non-analytical INTERLUDE (a clarify or conversational-
# short-circuit turn) skips plan/gather, so nothing would repopulate a wiped
# bundle -- a later followup would then find the thread empty and wrongly redirect
# (the R-1 regression). They are therefore DURABLE on a followup/interlude and
# SCRATCH on every fresh analytical turn. (``reports_by_ticker`` was previously an
# always-reset base member, which false-flagged the second ticker's numbers on a
# comparison->followup once QNT-324 made the ComparisonAnswer the narrate
# substrate -- R-2.)
_SCRATCH_RESET_SUBSTRATE: dict[str, object] = {"reports": {}, "reports_by_ticker": {}}
# DURABLE (never reset here): ``messages`` (QNT-216 transcript, append-only),
# ``prior_answer`` (QNT-307, snapshotted from the prior turn's ``answer``), the
# substrate on an interlude/followup, and the turn-scoped keys classify itself
# writes (ticker/intent/route/needs_*_search/...). The full scratch key set, for
# the meta-test:
SCRATCH_RESET_KEYS: frozenset[str] = frozenset(_SCRATCH_RESET_BASE) | frozenset(
    _SCRATCH_RESET_SUBSTRATE
)


def _preserves_thread_substrate(intent: str, route: str) -> bool:
    """QNT-349 (R-1/R-2): does this turn preserve the durable thread substrate
    (``reports`` + ``reports_by_ticker``) across the boundary reset?

    True for a followup (reuses the hydrated bundle verbatim -- QNT-209/QNT-324)
    and for a non-analytical INTERLUDE: a clarify turn (route "clarify") or a
    conversational short-circuit (route "synthesize") skips plan/gather, so
    nothing would repopulate a wiped bundle and a later followup would find the
    thread empty and wrongly emit the no-context redirect (the R-1 regression).
    A fresh analytical turn (thesis / quick_fact / comparison / focused /
    exploration -> route "plan" or "explore_supervisor") returns False so its
    stale bundle is cleared. A followup that fires a targeted RAG search routes
    to "plan" rather than short-circuiting, so the ``intent == "followup"`` arm
    (not the route arm) carries it.
    """
    return intent == "followup" or route in {"clarify", "synthesize"}


def _turn_boundary_reset(intent: str, route: str) -> dict[str, object]:
    """QNT-323 (G-4) / QNT-349 (R-1/R-2): the per-turn scratch reset classify_node
    applies at the turn boundary, given the resolved intent and route.

    Returns a fresh dict (never the shared literal) so a caller merging it can't
    mutate the module-level default. The durable substrate (``reports`` +
    ``reports_by_ticker``) is cleared only on a fresh analytical turn; an
    interlude or followup preserves it (see ``_preserves_thread_substrate`` and
    ``_SCRATCH_RESET_SUBSTRATE``).
    """
    reset = dict(_SCRATCH_RESET_BASE)
    if not _preserves_thread_substrate(intent, route):
        reset.update(_SCRATCH_RESET_SUBSTRATE)
    return reset


def _grounding_rate(result: HallucinationResult) -> float:
    """Fraction of answer numbers traceable to a fetched report."""
    total = len(result.thesis_numbers)
    if total == 0:
        return 1.0
    supported = total - len(result.unsupported)
    return round(supported / total, 2)


# QNT-355 (H-1): the AS_OF footer parser. QNT-299 appends a uniform
# ``AS_OF: YYYY-MM-DD`` line (see api.formatters.format_as_of_footer) as the LAST
# line of every report body; this is its single reader. Anchored to end-of-body
# (rather than a bare search) so a folded RAG hit or headline snippet earlier in
# the body can never be misread as the report's own footer. Also consumed by the
# SSE data_as_of surface (api.routers.agent_chat), which imports it from here --
# so AS_OF has exactly two touch-points: the formatter (writer) and this parser.
_AS_OF_PATTERN = re.compile(r"AS_OF: (\d{4}-\d{2}-\d{2})\s*$")


def parse_as_of(body: str) -> date | None:
    """Return the report body's AS_OF footer date, or None when it has no
    parseable footer (a stubbed test graph, an ``N/M`` no-data footer, or a
    comparison_metrics JSON blob that carries no footer line).

    Total by contract: a footer whose digits match the pattern but form an
    invalid calendar date (``2026-13-45``) yields None rather than raising, so
    the two un-guarded call sites (the narrate freshness tail and the SSE
    data_as_of surface) can never crash a turn on a malformed body.
    """
    m = _AS_OF_PATTERN.search(body)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


# QNT-355 (H-1): per-report-type "fresh horizon" in days -- the age within which
# a report is fully fresh (freshness 1.0). Technical/news refresh daily (a few
# days' tolerance covers a weekend or holiday gap); the fundamental snapshot and
# the static company context refresh at ~quarterly cadence, so a month-old
# fundamental is still fresh while a month-old daily technical is not.
_FRESHNESS_HORIZON_DAYS: dict[str, int] = {
    "technical": 5,
    "news": 5,
    "fundamental": 100,
    "company": 100,
}
_DEFAULT_FRESHNESS_HORIZON_DAYS = 100


def _freshness_factor(report_key: str, as_of: date, today: date) -> float:
    """Freshness in (0, 1] for one report given its AS_OF date.

    Neutral (1.0) within the report type's expected refresh cadence, then a
    reciprocal decay beyond it (``horizon / age``): a report twice its horizon
    old scores 0.5, three times 0.33, and so on.
    """
    horizon = _FRESHNESS_HORIZON_DAYS.get(report_key, _DEFAULT_FRESHNESS_HORIZON_DAYS)
    age_days = (today - as_of).days
    if age_days <= horizon:
        return 1.0
    return round(horizon / age_days, 2)


def _reports_freshness(state: AgentState) -> float:
    """QNT-355 (H-1): freshness factor for :func:`_composite_confidence`, read
    from the AS_OF footer of every report gathered this turn.

    The stalest report caps the whole answer (mirrors the SSE data_as_of
    oldest-date rule -- a thesis is only as fresh as its weakest report, a
    comparison only as fresh as its stalest side). 1.0 when no gathered report
    carries a parseable footer, so a stubbed test graph or a canned digest is
    never penalised for staleness it can't express.
    """
    reports_by_ticker = state.get("reports_by_ticker") or {}
    bundles = (
        list(reports_by_ticker.values()) if reports_by_ticker else [state.get("reports") or {}]
    )
    today = date.today()
    factors = [
        _freshness_factor(str(name), as_of, today)
        for bundle in bundles
        if isinstance(bundle, dict)
        for name, body in bundle.items()
        if (as_of := parse_as_of(str(body))) is not None
    ]
    return min(factors) if factors else 1.0


def _composite_confidence(
    coverage: float,
    grounding_rate: float = 1.0,
    freshness: float = 1.0,
) -> float:
    """Answer-groundedness score = coverage x grounding x freshness.

    ``coverage`` is report availability, ``grounding_rate`` is numeric support,
    and ``freshness`` (QNT-355) decays each gathered report's AS_OF footer past
    its expected refresh cadence -- daily for technical/news, ~quarterly for
    fundamental -- with the stalest report capping the factor (see
    :func:`_reports_freshness`). Callers that gathered no dated report pass the
    neutral 1.0.
    """
    return round(max(0.0, min(1.0, coverage * grounding_rate * freshness)), 2)


def _runtime_report_texts(state: AgentState) -> list[str]:
    """Return report bodies gathered for this run, including comparison reports.

    QNT-324: on a followup, the prior turn's card (``prior_answer``) is also a
    grounding source. The card was numerically grounded when it was produced, and
    the reports that backed a non-thesis card (e.g. a comparison's second-ticker
    ``reports_by_ticker``) are cleared by the turn-boundary reset -- so a followup
    that faithfully re-quotes the card must not read as unsupported. Numbers the
    narrator invents beyond the card + surviving reports still are.
    """
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        texts: list[str] = []
        for ticker_reports in reports_by_ticker.values():
            if isinstance(ticker_reports, dict):
                texts.extend(str(report) for report in ticker_reports.values())
    else:
        reports = state.get("reports") or {}
        texts = [str(report) for report in reports.values()] if isinstance(reports, dict) else []
    if state.get("intent") == "followup":
        prior_card_md = getattr(state.get("prior_answer"), "to_markdown", None)
        if callable(prior_card_md):
            try:
                texts = [*texts, str(prior_card_md())]
            except Exception:  # noqa: BLE001 — never let formatting break grounding
                pass
    return texts


# QNT-359 (fix B): the valuation labels a narrate payload can carry (Thesis /
# comparison ``**Label:**``, focused ``**Verdict:**``). A card shown one of these
# was shown the LABEL but not the peer-median-vs-ticker number that justifies it,
# so a spoken "X% above the sector median" gets fabricated. Anchored on the
# rendered label/verdict line so a bare "premium" adjective in prose does not
# trigger the fold.
_VALUATION_LABEL_RE = re.compile(r"\*\*(?:Label|Verdict):\*\*\s*(?:Premium|Inline|Discounted)\b")

_PEER_SECTION_HEADING = "## PEER CONTEXT"
# The own-history block: the per-multiple "range X-Y over last 5y, <position>"
# lines drive the Premium/Inline/Discounted label when peer coverage is thin
# (peer median N/A). The fundamental aspect label is quoted from the quarterly
# section, so its quarterly VALUATION block is the own-history line to fold.
_QUARTERLY_VALUATION_HEADING = "### QUARTERLY VALUATION"


def _payload_carries_valuation_label(payload_markdown: str) -> bool:
    """True when the narrate payload renders a fundamental valuation label."""
    return bool(_VALUATION_LABEL_RE.search(payload_markdown or ""))


def _fundamental_report_texts(state: AgentState) -> list[str]:
    """Every fundamental report body gathered this turn (single + comparison).

    Mirrors the source split :func:`_runtime_report_texts` grades against, but
    narrowed to the ``fundamental`` report -- the only one carrying a
    ``## PEER CONTEXT`` block.
    """
    texts: list[str] = []
    reports_by_ticker = state.get("reports_by_ticker") or {}
    if reports_by_ticker:
        for ticker_reports in reports_by_ticker.values():
            if isinstance(ticker_reports, dict) and ticker_reports.get("fundamental"):
                texts.append(str(ticker_reports["fundamental"]))
    reports = state.get("reports") or {}
    if isinstance(reports, dict) and reports.get("fundamental"):
        texts.append(str(reports["fundamental"]))
    return texts


def _slice_section(report_text: str, heading: str, stops: tuple[str, ...]) -> str | None:
    """Slice one section out of a report: from ``heading`` up to the first of
    ``stops`` after it (or end of report). None when ``heading`` is absent.

    ``stops`` are the heading prefixes that terminate the section -- e.g. a
    level-2 ``## PEER CONTEXT`` stops at the next ``\\n## `` (a ``### `` subsection
    does not close it); a level-3 ``### QUARTERLY VALUATION`` stops at the next
    ``\\n### `` sibling OR the next ``\\n## `` parent, whichever comes first.
    """
    start = report_text.find(heading)
    if start < 0:
        return None
    rest = report_text[start:]
    ends = [idx for idx in (rest.find(stop, 1) for stop in stops) if idx >= 0]
    section = rest if not ends else rest[: min(ends)]
    return section.strip() or None


def _peer_valuation_section(report_text: str) -> str | None:
    """Slice the peer-delta AND own-history valuation lines from a fundamental
    report (QNT-359).

    Two blocks justify a Premium / Inline / Discounted label, and AC1 names both:
    ``## PEER CONTEXT`` (the ``_peer_line`` sector-median-vs-ticker %) and
    ``### QUARTERLY VALUATION`` (the per-multiple "range X-Y over last 5y,
    <position>" own-history line -- which drives the label when peer coverage is
    thin). Fold both so any magnitude narrate speaks -- versus peers OR versus its
    own history -- is quotable-verbatim. Returns the blocks joined, or None when
    the report carries neither. Stays tight: the growth / profitability / cash
    subsections after the valuation block are not dragged in.
    """
    blocks = [
        block
        for block in (
            _slice_section(report_text, _PEER_SECTION_HEADING, ("\n## ",)),
            _slice_section(report_text, _QUARTERLY_VALUATION_HEADING, ("\n### ", "\n## ")),
        )
        if block
    ]
    if not blocks:
        return None
    return "\n\n".join(blocks)


def _narrate_peer_substrate(state: AgentState, payload_markdown: str) -> str | None:
    """QNT-359 (fix B): the peer/valuation section to fold into narrate's input.

    When the narrate payload carries a valuation label but was shown only the
    card, the peer-delta number that backs the label lives in the fundamental
    report -- which narrate is already GRADED against via
    :func:`_runtime_report_texts` but never SHOWN. Returning that section here
    lets narrate_node fold it into the substrate so a spoken comparison magnitude
    is quotable-verbatim from what the model saw. None when there is nothing to
    fold (no label, or no peer section).
    """
    if not _payload_carries_valuation_label(payload_markdown):
        return None
    sections = [
        section
        for text in _fundamental_report_texts(state)
        if (section := _peer_valuation_section(text))
    ]
    if not sections:
        return None
    # De-dupe identical sections (a single-ticker turn folds one) while keeping order.
    return "\n\n".join(dict.fromkeys(sections))


def _runtime_grounding_check(answer: str, reports: list[str]) -> tuple[HallucinationResult, float]:
    """Advisory runtime numeric grounding check for completed answer text."""
    result = check_grounding(answer, reports)
    return result, _grounding_rate(result)


def _quick_fact_cited_value_supported(quick_fact: QuickFactAnswer, state: AgentState) -> bool:
    """C-4: ``cited_value`` must be a verbatim substring of the report it
    names (the contract from ``quick_fact.py``'s module docstring). Empty
    ``cited_value`` is a no-op -- nothing was cited, nothing to check.

    Case-insensitive, word-boundary-aware match rather than plain ``in``:
    a naive substring test both false-positives on a garbled non-numeric
    citation that happens to be a substring of the right word (``"sold"``
    inside a report's ``"oversold"`` reads as supported) and false-positives
    on harmless case drift (``"Overbought"`` vs. the report's
    ``"overbought"``) — exactly the class of reformatted/invented citation
    this check exists to catch. ``(?<!\\w)`` / ``(?!\\w)`` rather than ``\\b``
    so a value with leading punctuation (``"$1,234.56"``) still boundary-checks
    correctly (``\\b`` is undefined between two non-word characters).
    """
    if not quick_fact.cited_value:
        return True
    if quick_fact.source is None:
        return False
    report = (state.get("reports") or {}).get(quick_fact.source, "")
    pattern = rf"(?<!\w){re.escape(quick_fact.cited_value)}(?!\w)"
    return re.search(pattern, report, re.IGNORECASE) is not None


def _quick_fact_grounding(state: AgentState, quick_fact: QuickFactAnswer) -> dict[str, object]:
    """QNT-296: runtime numeric grounding for quick_fact turns.

    quick_fact skips narrate's tail entirely (QNT-232 #3), so it never picks
    up ``_runtime_grounding_check`` / ``_composite_confidence`` the way every
    other analytical shape does. This mirrors that pair against the card's
    own markdown, then folds in the C-4 substring check on ``cited_value`` --
    stricter than the number-regex check, since it also catches a
    reformatted or invented non-numeric ``cited_value`` (e.g. a wrong regime
    word) that the regex would never flag.
    """
    coverage = float(state.get("confidence", 0.0))
    grounding_result, _ = _runtime_grounding_check(
        quick_fact.to_markdown(), _runtime_report_texts(state)
    )
    unsupported = list(grounding_result.unsupported)
    total = len(grounding_result.thesis_numbers)
    cited_value = quick_fact.cited_value
    if cited_value:
        # A numeric cited_value is embedded verbatim in to_markdown()'s
        # "**Value:**" line, so the regex check above already extracted and
        # scored it as one of ``thesis_numbers`` -- don't inflate the
        # denominator by counting the same claim twice. A non-numeric
        # cited_value (a regime word like "overbought") is invisible to the
        # regex check, so it IS a new claim and adds a denominator slot.
        cited_numbers = extract_numbers(cited_value)
        already_scored = bool(cited_numbers) and cited_numbers <= grounding_result.thesis_numbers
        if not already_scored:
            total += 1
        if not _quick_fact_cited_value_supported(quick_fact, state):
            # Flag using the SAME token form the claim was already counted
            # under: the canonicalised number(s) if already_scored (avoids
            # e.g. both "1234.56" and the raw "$1,234.56" landing in
            # ``unsupported`` for one claim, which would double-penalise it
            # and could push grounding_rate below 0); the raw string
            # otherwise, since a non-numeric cited_value has no canonical
            # form and was never added to ``unsupported`` by the regex pass.
            if already_scored:
                for canonical in cited_numbers:
                    if canonical not in unsupported:
                        unsupported.append(canonical)
            elif cited_value not in unsupported:
                unsupported.append(cited_value)
    grounding_rate = 1.0 if total == 0 else round((total - len(unsupported)) / total, 2)
    confidence = _composite_confidence(coverage, grounding_rate, _reports_freshness(state))
    combined = HallucinationResult(
        ok=not unsupported,
        unsupported=tuple(unsupported),
        thesis_numbers=grounding_result.thesis_numbers,
        report_numbers=grounding_result.report_numbers,
    )
    ticker = state["ticker"]
    if combined.ok:
        logger.info(
            "narrate %s: grounding_rate=%s confidence=%s", ticker, grounding_rate, confidence
        )
    else:
        logger.warning(
            "narrate %s: grounding miss rate=%s unsupported=%s confidence=%s",
            ticker,
            grounding_rate,
            combined.reason(),
            confidence,
        )
    return {
        "grounding_rate": grounding_rate,
        "grounding_unsupported": unsupported,
        "confidence": confidence,
    }


def build_graph(
    tools: dict[str, ToolFn],
    *,
    event_emitter: EventEmitter | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    compact_company_tool: ToolFn | None = None,
    retrieval_tools: dict[str, SearchToolFn] | None = None,
    comparison_metrics_tool: ComparisonMetricsToolFn | None = None,
    # QNT-291 deprecated shims: ``search_news_tool`` / ``search_earnings_tool``
    # fold into ``retrieval_tools`` keyed by RetrievalSpec.name below. Kept so
    # existing callers/tests that inject the two corpora by name keep passing
    # mock callables unchanged. A NEW corpus goes through ``retrieval_tools``,
    # not a new kwarg.
    search_news_tool: SearchToolFn | None = None,
    search_earnings_tool: SearchToolFn | None = None,
) -> CompiledStateGraph:
    """Compile the classify -> plan -> gather -> synthesize graph (QNT-149, QNT-159).

    Tools are a plain ``{name: callable}`` mapping. Callables take a ticker
    string and return a report string. Exceptions are caught and retried;
    optional tools (see ``OPTIONAL_TOOLS``) are silently dropped after retry
    exhaustion, required tools surface in ``state['errors']``.

    ``event_emitter`` is an optional callable for streaming intermediate
    state to a consumer while the graph runs. Today it's used by the SSE
    endpoint to surface the ``intent`` event from the classify node BEFORE
    the first tool_call lands — without it the panel's streaming label
    would say "streaming thesis…" for the entire tool-gathering phase
    regardless of which intent the classifier picked (the post-graph
    intent emission was too late). None is a no-op, used by the CLI and
    unit tests that read final state directly.

    QNT-220 (#8): ``compact_company_tool`` is an optional callable used for the
    ``company`` slot on the thesis/comparison/exploration hot path (where the
    company report is force-injected per QNT-175). When None, the full
    ``tools['company']`` is used everywhere (existing CLI / eval / test
    behavior). Focused fundamental/company asks always keep the full report.

    QNT-291: retrieval tools (semantic vector search over a Qdrant corpus) are
    injected via ``retrieval_tools`` -- a ``{RetrievalSpec.name: (ticker, query)
    -> str}`` mapping -- and dispatched by ``gather_node`` iterating
    ``RETRIEVAL_SPECS`` rather than a hand-written branch per corpus. Each
    travels outside the ``tools`` map because its two-arg signature does not fit
    the single-arg plan-surface dispatch (``_call_with_retry`` /
    ``_instrument_tools``). ``search_news_tool`` (QNT-222, equity_news, folds
    retrieved headlines into ``reports["news"]``) and ``search_earnings_tool``
    (QNT-263, equity_earnings, folds release excerpts into
    ``reports["fundamental"]`` tagged ``corpus="earnings"``) are DEPRECATED
    kwargs kept as shims -- each populates ``retrieval_tools`` under its spec
    name if that key is not already set. A corpus fires when the classifier sets
    its ``needs_*_search`` flag AND the intent's synthesis reads that corpus
    (``RetrievalSpec.fires`` / :func:`_intent_reads_corpus`, the QNT-288 policy
    table). Unset corpora leave the flag False and keep the canned digest. Empty
    ``retrieval_tools`` ⇒ canned-digest-only (CLI / eval / tests).

    QNT-224: ``comparison_metrics_tool`` is an optional ``(list[str]) -> str``
    callable hitting the lean comparison-metrics endpoint. It is NOT a retrieval
    registry entry: it fires only on the 3-4-ticker comparison topology (not a
    per-turn gated fold), takes the whole ticker list, and stashes its JSON into
    ``reports["comparison_metrics"]`` rather than folding provenance. The rich
    two-ticker path never touches it. None ⇒ a 3-4 way compare degrades to a
    conversational redirect (CLI / eval / non-wired tests); the rich two-ticker
    path is unaffected.
    """
    # QNT-291: bind the retrieval registry. Deprecated per-corpus kwargs fold
    # into the name-keyed mapping (setdefault so an explicit ``retrieval_tools``
    # entry wins); each spec is paired with its injected callable (or None).
    retrieval_tools = dict(retrieval_tools or {})
    if search_news_tool is not None:
        retrieval_tools.setdefault(NEWS_RETRIEVAL.name, search_news_tool)
    if search_earnings_tool is not None:
        retrieval_tools.setdefault(EARNINGS_RETRIEVAL.name, search_earnings_tool)
    active_retrievals: tuple[tuple[RetrievalSpec, SearchToolFn | None], ...] = tuple(
        (spec, retrieval_tools.get(spec.name)) for spec in RETRIEVAL_SPECS
    )

    # QNT-294 (AC1): the nodes are now module-level functions in ``agent.nodes``.
    # Bind ``deps`` here and wire the identical topology below. Imported inside
    # build_graph so ``agent.graph`` is fully loaded (the re-exports the node
    # modules read) before the node modules import it back.
    from functools import partial

    from agent.nodes.clarify import clarify_node as _clarify_node_fn
    from agent.nodes.classify import _classify_router as _classify_router_fn
    from agent.nodes.classify import classify_node as _classify_node_fn
    from agent.nodes.deps import GraphDeps
    from agent.nodes.gather import explore_supervisor_node as _explore_supervisor_node_fn
    from agent.nodes.gather import gather_node as _gather_node_fn
    from agent.nodes.narrate import narrate_node as _narrate_node_fn
    from agent.nodes.plan import plan_node as _plan_node_fn
    from agent.nodes.synthesize import synthesize_node as _synthesize_node_fn

    deps = GraphDeps(
        tools=tools,
        event_emitter=event_emitter,
        compact_company_tool=compact_company_tool,
        comparison_metrics_tool=comparison_metrics_tool,
        active_retrievals=active_retrievals,
    )

    classify_node = partial(_classify_node_fn, deps=deps)
    plan_node = partial(_plan_node_fn, deps=deps)
    gather_node = partial(_gather_node_fn, deps=deps)
    explore_supervisor_node = partial(_explore_supervisor_node_fn, deps=deps)
    synthesize_node = partial(_synthesize_node_fn, deps=deps)
    narrate_node = partial(_narrate_node_fn, deps=deps)
    clarify_node = partial(_clarify_node_fn, deps=deps)
    # QNT-320 (G-2): the router is a pure state read now -- no deps to bind.
    _classify_router = _classify_router_fn

    def _wrap_path(
        node_name: str, fn: Callable[..., dict[str, object]]
    ) -> Callable[..., dict[str, object]]:
        """QNT-212: append this node's name to ``intent_path`` after the
        wrapped node returns. Classify is the per-turn entry node, so it
        RESETS the list to ``["classify"]`` -- otherwise a checkpointer-
        hydrated path from the prior turn would leak into the current turn's
        intent_path. Every other node reads ``state["intent_path"]`` (already
        merged with the prior nodes' returns) and appends its own name.

        Done this way instead of with an ``Annotated[list, add]`` reducer
        because the reducer accumulates the prior turn's path too -- the
        checkpointer hydrates state before classify even runs.
        """

        def wrapped(state: AgentState, config: RunnableConfig) -> dict[str, object]:
            result = fn(state, config)
            if not isinstance(result, dict):
                return result
            if node_name == "classify":
                result["intent_path"] = ["classify"]
            else:
                existing = list(state.get("intent_path") or [])
                result["intent_path"] = [*existing, node_name]
            return result

        return wrapped

    builder: StateGraph = StateGraph(AgentState)
    builder.add_node("classify", _wrap_path("classify", classify_node))
    builder.add_node("plan", _wrap_path("plan", plan_node))
    builder.add_node("gather", _wrap_path("gather", gather_node))
    builder.add_node("synthesize", _wrap_path("synthesize", synthesize_node))
    builder.add_node("narrate", _wrap_path("narrate", narrate_node))
    builder.add_node("clarify", _wrap_path("clarify", clarify_node))
    builder.add_node(
        "explore_supervisor",
        _wrap_path("explore_supervisor", explore_supervisor_node),
    )
    builder.add_edge(START, "classify")
    # QNT-212: classify routes by ambiguity / intent rather than always
    # falling through to plan. Three destinations:
    #   - clarify  : ambiguous question (no ticker, only one ticker for a
    #                compare, etc.) — ask back, exit through narrate.
    #   - synthesize: conversational greeting or followup-no-refetch — skip
    #                 plan + gather, save the latency of 2 no-op nodes.
    #   - explore_supervisor: anchored exploratory ask — bounded iterative
    #                         report selection, then normal synthesize/narrate.
    #   - plan     : thesis / focused / quick_fact / comparison — existing
    #                full-pipeline behavior.
    builder.add_conditional_edges(
        "classify",
        _classify_router,
        {
            "clarify": "clarify",
            "synthesize": "synthesize",
            "explore_supervisor": "explore_supervisor",
            "plan": "plan",
        },
    )
    builder.add_edge("plan", "gather")
    # QNT-156: always run synthesize. Empty reports no longer short-circuit
    # to END — synthesize handles every failure surface (no reports, empty
    # payload, structured-output crash) by emitting a deterministic
    # conversational redirect via ``domain_redirect``. The panel never sees
    # a blank state again.
    builder.add_edge("gather", "synthesize")
    builder.add_edge("explore_supervisor", "synthesize")
    # QNT-212: clarify exits through narrate the same way the rest of the
    # pipeline does. narrate sees ``state['conversational']`` is populated
    # and short-circuits (the bubble would duplicate the clarify question),
    # but we keep the edge for topology consistency + intent_path tracking.
    builder.add_edge("clarify", "narrate")
    # QNT-211: narrate streams a 1-4 sentence analyst-voice paragraph above
    # whichever structured shape synthesize produced. Always runs (even on
    # conversational + fallback redirects -- the node short-circuits those
    # internally) so the topology stays linear.
    builder.add_edge("synthesize", "narrate")
    builder.add_edge("narrate", END)
    # QNT-209: passing a checkpointer enables the followup path — the next
    # invocation against the same thread_id sees ``reports`` hydrated from
    # the prior turn. None ⇒ ephemeral compile (curl, tests, non-frontend
    # callers, the QNT-209 ephemeral fallback in the SSE endpoint).
    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


__all__ = [
    "ANALYTICAL_ANSWER_TYPES",
    "INTENT_POLICIES",
    "OPTIONAL_TOOLS",
    "REPORT_TOOLS",
    "AgentState",
    "AmbiguityKind",
    "ComparisonAnswer",
    "ConversationalAnswer",
    "EventEmitter",
    "ExplorationAnswer",
    "FocusedAnalysis",
    "Intent",
    "IntentPolicy",
    "QuickFactAnswer",
    "ReportToolName",
    "Thesis",
    "ThesisPlan",
    "ToolFn",
    "analytical_followup_suggestions",
    "build_graph",
]
