"""QNT-294 (AC1): per-intent routing policy table + shared routing types/constants.

Extracted verbatim from graph.py. The single source of truth for per-intent
behaviour (``INTENT_POLICIES``, QNT-288) plus the tool-type aliases, ambiguity
kinds, history budget, and clarify constants the nodes share. graph.py and the
node modules import from here; this module has no dependency on graph.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from agent.intent import Intent
from agent.prompts import HISTORY_TURN_LIMIT

ToolFn = Callable[[str], str]
# QNT-222: search_news has a two-arg (ticker, query) signature that does not
# fit the single-arg ``ToolFn`` plan-surface dispatch, so it travels as its own
# typed callable rather than as a ``REPORT_TOOLS`` entry.
SearchToolFn = Callable[[str, str], str]
# QNT-224: lean comparison-metrics fetch takes the full ticker list (2-4) in one
# call and returns the JSON metrics payload; like search_news it travels outside
# the single-arg ``ToolFn`` plan-surface map.
ComparisonMetricsToolFn = Callable[[list[str]], str]
ReportToolName = Literal["company", "technical", "fundamental", "news"]

# QNT-224: N-way comparison band. 2 tickers keep the rich four-aspect bundle
# (unchanged); 3-4 take the lean metrics-table path; 5+ redirect.
_MIN_COMPARISON_TICKERS = 2
_MAX_COMPARISON_TICKERS = 4


# QNT-159: optional event emitter for streaming intermediate state out of the
# graph as it runs, NOT after it completes. The classify node fires this with
# ``("intent", {"intent": <intent>})`` as soon as the classifier resolves so
# the SSE wrapper can surface the routing decision before the first tool_call
# event lands. Mirrors the tool-instrumentation pattern in
# ``api.routers.agent_chat._instrument_tools`` (worker thread → asyncio queue
# via ``loop.call_soon_threadsafe``); keeping it as a typed alias here makes
# the SSE adapter's responsibility explicit. None means "no streaming" — the
# CLI and unit tests don't pass one, the SSE endpoint always does.
EventEmitter = Callable[[str, dict[str, object]], None]

# Tool registry is canonical in ``agent.prompts.system`` so the citation list
# in SYSTEM_PROMPT and the dispatch list here can't drift. ``summary`` is
# intentionally omitted from the trio: plan/gather select from what the thesis
# composes over; adding summary is a QNT-60 call if the eval shows it improves
# grounding.

# News is optional — Qdrant or the news ingest can be down without invalidating
# the thesis. Technical & fundamental are load-bearing. This is a TOOL-level
# property (keyed by report name, not by intent), so it stays outside
# ``IntentPolicy`` / ``INTENT_POLICIES`` below.
OPTIONAL_TOOLS: frozenset[str] = frozenset({"news"})

# QNT-232 #13: fresh analytical asks (thesis / quick_fact / focused /
# comparison / exploration) stand on the reports they just gathered and
# rarely need deep history; only continuations (followup / conversational)
# genuinely lean on it. See ``IntentPolicy.history_budget``.
_FRESH_ANALYTICAL_HISTORY_TURNS = 3


@dataclass(frozen=True)
class IntentPolicy:
    """Declarative per-intent routing policy (QNT-288).

    Consolidates behaviour that used to live in six-plus parallel frozensets
    which had to be kept mutually consistent by hand -- QNT-263 caught one
    drift where ``quick_fact`` was missing from the earnings-search set. One
    ``IntentPolicy`` exists per ``Intent`` literal member, held in
    ``INTENT_POLICIES``; the legacy frozensets/dicts below are DERIVED views
    over this table rather than hand-maintained.
    """

    # Report family plan_node deterministically narrows to for a
    # focused-analysis intent (fundamental/technical/news). News is the
    # report family for the news focus even though it is in
    # ``OPTIONAL_TOOLS`` -- if news is down, synthesize falls back to a
    # domain redirect like any other empty-reports failure. None for intents
    # that plan differently (LLM-driven narrow/over-fetch, all-tools, or no
    # plan at all).
    focused_report: str | None
    # Which RAG corpora this intent's synthesis can consume a retrieved hit
    # from -- gates gather_node's search_news / search_earnings calls.
    rag_corpora: frozenset[Literal["news", "earnings"]]
    # Max prior turns injected into this intent's synthesize/narrate prompt prefix.
    history_budget: int
    # Which company-report variant plan/gather swaps into the 'company' slot.
    company_variant: Literal["compact", "full"]
    # Whether classify's ambiguity gate requires a named ticker (or a
    # hydrated prior turn) before this intent can proceed unclarified.
    requires_ticker: bool
    # Whether classify routes this intent straight to synthesize, skipping
    # plan + gather.
    short_circuit: bool
    # Label fed to domain_redirect's suggestion picker when this intent's
    # synthesis fails and falls back to a conversational redirect. Must
    # match a label in agent.conversational._SUGGESTION_BANK. None for
    # intents that never reach the fallback redirect narratively or have no
    # failure-path bias to set.
    suggestion_hint: str | None
    # QNT-298: deterministic follow-up chip templates shown under this
    # intent's card once the turn completes -- each pair is
    # (target_intent, template), where target_intent is the shape a click
    # would classify as (used by tests to confirm the chip does not route to
    # clarify) and template contains ``{ticker}`` and, for the comparison
    # shape, ``{partner}`` placeholders. None for intents that render no
    # analytical card to follow up on: conversational already IS the
    # suggestion surface (see suggestion_hint), and followup reuses the
    # prior turn's card with nothing new to suggest.
    followup_templates: tuple[tuple[Intent, str], ...] | None


# QNT-288: single source of truth for per-intent routing behaviour. A
# meta-test in test_graph.py asserts every ``Intent`` literal member has an
# entry here with all fields populated, so a future intent cannot ship
# half-configured the way the QNT-263 quick_fact/earnings-search miss did.
INTENT_POLICIES: dict[Intent, IntentPolicy] = {
    "thesis": IntentPolicy(
        focused_report=None,
        rag_corpora=frozenset({"news", "earnings"}),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="compact",
        requires_ticker=True,
        short_circuit=False,
        suggestion_hint="thesis",
        followup_templates=(
            ("news", "What's the news angle on {ticker}?"),
            ("comparison", "Compare {ticker} vs {partner}"),
            ("exploration", "What should I watch this week on {ticker}?"),
        ),
    ),
    "quick_fact": IntentPolicy(
        focused_report=None,
        rag_corpora=frozenset({"news", "earnings"}),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="full",
        requires_ticker=True,
        short_circuit=False,
        # No bank label for "quick_fact" itself -- most single-metric asks
        # are technical (RSI, MACD, price), so failures bias there.
        suggestion_hint="technical",
        followup_templates=(
            ("thesis", "Full thesis on {ticker}?"),
            ("news", "What's the news angle on {ticker}?"),
        ),
    ),
    "comparison": IntentPolicy(
        focused_report=None,
        rag_corpora=frozenset(),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="compact",
        requires_ticker=False,  # its own multi-ticker ambiguity check governs this
        short_circuit=False,
        suggestion_hint="comparison",
        followup_templates=(
            ("thesis", "Full thesis on {ticker}?"),
            ("thesis", "Full thesis on {partner}?"),
        ),
    ),
    "conversational": IntentPolicy(
        focused_report=None,
        rag_corpora=frozenset(),
        history_budget=HISTORY_TURN_LIMIT,
        company_variant="full",
        requires_ticker=False,
        short_circuit=True,
        # Conversational IS the fallback redirect -- this hint is unreachable.
        suggestion_hint=None,
        # Conversational already ships its own suggestions on the answer
        # payload (see suggestion_hint / domain_redirect) -- no separate
        # follow-up chip row.
        followup_templates=None,
    ),
    "fundamental": IntentPolicy(
        focused_report="fundamental",
        rag_corpora=frozenset({"earnings"}),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="full",
        requires_ticker=True,
        short_circuit=False,
        suggestion_hint="fundamental",
        followup_templates=(
            ("technical", "How is {ticker} trending technically?"),
            ("thesis", "Full thesis on {ticker}?"),
        ),
    ),
    "technical": IntentPolicy(
        focused_report="technical",
        rag_corpora=frozenset(),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="full",
        requires_ticker=True,
        short_circuit=False,
        suggestion_hint="technical",
        followup_templates=(
            ("fundamental", "What's the fundamental case for {ticker}?"),
            ("thesis", "Full thesis on {ticker}?"),
        ),
    ),
    "news": IntentPolicy(
        focused_report="news",
        rag_corpora=frozenset({"news"}),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="full",
        requires_ticker=True,
        short_circuit=False,
        suggestion_hint="news",
        followup_templates=(
            ("thesis", "Full thesis on {ticker}?"),
            ("technical", "How is {ticker} trending technically?"),
        ),
    ),
    "followup": IntentPolicy(
        focused_report=None,
        # QNT-290: a warm-thread pivot to a new targeted event sets the same
        # search flags as a cold turn; both corpora fold onto the
        # checkpointer-hydrated reports.
        rag_corpora=frozenset({"news", "earnings"}),
        history_budget=HISTORY_TURN_LIMIT,
        company_variant="full",
        requires_ticker=False,  # its own has_prior_turn guard governs this
        short_circuit=True,
        # narrate owns the spoken response on this path; no bias to set.
        suggestion_hint=None,
        # Reuses the prior turn's card (QuickFactAnswer) verbatim -- nothing
        # new happened this turn to suggest a follow-up from.
        followup_templates=None,
    ),
    "exploration": IntentPolicy(
        focused_report=None,
        rag_corpora=frozenset(),
        history_budget=_FRESH_ANALYTICAL_HISTORY_TURNS,
        company_variant="compact",
        requires_ticker=False,
        short_circuit=False,
        # QNT-220 follow-up: a failed broad scan biases toward thesis suggestions.
        suggestion_hint="thesis",
        followup_templates=(
            ("thesis", "Full thesis on {ticker}?"),
            ("comparison", "Compare {ticker} vs {partner}"),
        ),
    ),
}

# --- Derived views (QNT-288) -------------------------------------------------
# Same shape callers relied on pre-refactor, but computed from
# INTENT_POLICIES instead of hand-maintained -- fixing an intent's behaviour
# means editing ONE entry above, not auditing every one of these.

# QNT-176: focused-analysis intent → matching report family. The plan node
# narrows to ``["company", <report>]`` for these intents (company grounds
# qualitative business context per QNT-175; the matching report carries the
# numbers).
_FOCUSED_REPORT: dict[Intent, str] = {
    intent: policy.focused_report
    for intent, policy in INTENT_POLICIES.items()
    if policy.focused_report is not None
}


# QNT-291: intents whose synthesis consumes a given RAG corpus gate that
# corpus's retrieval fetch -- a semantic-search hit only reaches the prompt
# for an intent whose synthesis actually reads the report it folds into
# (news -> reports["news"], earnings -> reports["fundamental"]). A
# fundamental/technical focused read is forbidden from citing news
# (FOCUSED_SYSTEM_PROMPT rule 3), so firing news search there would be a
# wasted Qdrant call. This replaces the former per-corpus _NEWS_SEARCH_INTENTS
# / _EARNINGS_SEARCH_INTENTS frozensets: one predicate over the QNT-288 policy
# table, so adding a corpus needs no new hand-derived intent set.
def _intent_reads_corpus(intent: Intent, corpus: str) -> bool:
    """QNT-291: True when ``intent``'s synthesis consumes ``corpus`` hits.

    The single gating predicate for every retrieval tool in the registry --
    reads the QNT-288 policy table directly (``INTENT_POLICIES[intent].
    rag_corpora``) rather than a per-corpus frozenset, so a new corpus gates
    off one ``RetrievalSpec.corpus`` plus the policy entries that list it, with
    no new ``_X_SEARCH_INTENTS`` set to hand-maintain. ``.get`` so an unknown
    intent is a clean no-fire rather than a KeyError.
    """
    policy = INTENT_POLICIES.get(intent)
    return policy is not None and corpus in policy.rag_corpora


# QNT-212: intents that need a named ticker (or hydrated prior turn) to
# answer non-fabricated. With no ticker AND no prior turn we route to
# clarify rather than ship a thesis built on a placeholder.
_TICKER_REQUIRING_INTENTS: frozenset[Intent] = frozenset(
    intent for intent, policy in INTENT_POLICIES.items() if policy.requires_ticker
)

# QNT-212: short-circuit intents skip plan + gather and route classify
# directly to synthesize. Conversational has no tools to gather; followup
# reuses the checkpointer-hydrated reports verbatim. QNT-290: this is the
# cheap-path default for followup -- ``_classify_router`` special-cases
# followup BEFORE consulting this set when the classifier flagged a
# targeted RAG need, routing through plan/gather instead (see
# ``_followup_fires_search``). Membership here still governs the pure
# no-tools-needed case (both search flags False) and every other reader of
# this set (e.g. ``_should_route_exploration``).
_SHORT_CIRCUIT_INTENTS: frozenset[Intent] = frozenset(
    intent for intent, policy in INTENT_POLICIES.items() if policy.short_circuit
)

# QNT-220 (#8): intents that force-include the company report (QNT-175) and so
# get the compact company variant when one is supplied. Focused
# fundamental/technical/news asks keep the full report. Exploration (QNT-220
# follow-up) is a hot path whose non-news-led plan includes company, so it
# inherits the compact variant to preserve the lever #8 token savings.
_COMPACT_COMPANY_INTENTS: frozenset[Intent] = frozenset(
    intent for intent, policy in INTENT_POLICIES.items() if policy.company_variant == "compact"
)

AmbiguityKind = Literal["needs_second_ticker", "needs_ticker", "needs_prior_turn"]

# QNT-212: deterministic fallback prose for when the clarify LLM call fails.
# Reasons must stay digit-free -- ``domain_redirect`` rejects digits at the
# boundary so a future caller cannot trip the hallucination guardrail.
_CLARIFY_FALLBACK_REASON: dict[str, str] = {
    "needs_ticker": "Which ticker did you have in mind?",
    "needs_second_ticker": "Which second ticker should I compare against?",
    "needs_prior_turn": "I don't have an earlier turn on this thread to follow up on.",
}

# QNT-220 follow-up: deterministic clarify lead-in bubbles. A clarify turn
# gathered ZERO reports, so an LLM-narrated bubble invents a stance with nothing
# to ground it (observed in prod: "On balance, the read is constructive for
# NVDA..." on a turn that fetched no data). The lead-in carries no analysis, so
# per ADR-003 (no reasoning => no LLM) it is emitted deterministically: always a
# content-free, engaging, digit-free readiness line that never restates the
# clarify card's question.
_CLARIFY_LEAD_IN: dict[str, str] = {
    "needs_ticker": "Happy to dig into any of the names I track.",
    "needs_second_ticker": "Happy to run that comparison for you.",
    "needs_prior_turn": "Happy to pick this up whenever you're ready.",
}
_CLARIFY_LEAD_IN_DEFAULT = "Happy to dig in."


# QNT-244: map each clarify ambiguity kind to the suggestion-bank hint used
# when the LLM's suggestions are rejected. needs_second_ticker wants concrete
# covered pairs; the others take a balanced mix (needs_prior_turn typically
# carries no suggestions at all, which coerce_suggestions leaves empty).
_CLARIFY_SUGGESTION_HINT: dict[str, str | None] = {
    "needs_second_ticker": "comparison",
    "needs_ticker": None,
    "needs_prior_turn": None,
}


# "clarify" is not an ``Intent`` literal member -- it is defensive belt-and-
# braces for ``_history_budget``'s public str contract (see
# test_message_history.py::test_history_budget_is_intent_aware), not a value
# any live ``AgentState['intent']`` ever holds. Kept outside INTENT_POLICIES
# for that reason rather than added as a fake Intent entry.
_DEEP_HISTORY_EXTRA: frozenset[str] = frozenset({"clarify"})


def _history_budget(intent: str) -> int:
    """Max prior turns to inject into a node's prompt prefix for ``intent``.

    QNT-288: reads ``IntentPolicy.history_budget`` from ``INTENT_POLICIES``
    for a live ``Intent``; falls back to the deep budget for
    ``_DEEP_HISTORY_EXTRA`` and the fresh-analytical budget otherwise.
    """
    policy = INTENT_POLICIES.get(intent)  # type: ignore[arg-type]
    if policy is not None:
        return policy.history_budget
    if intent in _DEEP_HISTORY_EXTRA:
        return HISTORY_TURN_LIMIT
    return _FRESH_ANALYTICAL_HISTORY_TURNS
