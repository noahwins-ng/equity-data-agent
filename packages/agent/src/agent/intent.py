"""Intent classification for the agent (QNT-149, QNT-156).

The agent used to force every input through the same four-section thesis
template. A user asking "what's the RSI right now?" got the same heavy
treatment as a user asking "is this a buy?" — rigid, ignores the question,
every answer looked the same.

This module classifies an inbound question into one of four response shapes:

* ``thesis`` — a balanced, multi-source investment thesis (Setup / Bull /
  Bear / Verdict). Default for open-ended asks ("should I be cautious about
  META?", "give me a balanced thesis on V").
* ``quick_fact`` — a short prose answer plus a single cited value, no
  thesis card. For single-metric lookups ("what's NVDA's P/E?",
  "what's the volume?").
* ``comparison`` — a side-by-side ComparisonAnswer for multi-ticker asks
  ("Compare NVDA vs AAPL", "How does META stack up against GOOGL?"). The
  graph clips to 2 tickers; 3+ falls back to a conversational redirect.
* ``conversational`` — a short ConversationalAnswer for greetings ("hi"),
  capability asks ("what can you do?"), meta questions, and clearly
  off-domain inputs ("what's the weather?", "tell me a joke"). The agent
  must never pretend to know things outside its domain.

Two-layer design:

1. A keyword heuristic short-circuits the obvious cases (single ``?``
   ending, tokens like 'rsi'/'p/e', length under N words, multi-ticker
   asks, greetings). This keeps the classifier free for the common case and
   degrades gracefully when the LLM misbehaves.
2. The LLM picks via ``with_structured_output(IntentDecision)`` on the
   ambiguous middle. Failures bias toward ``thesis`` — the existing path is
   the safe default; the eval golden set (QNT-67, QNT-128) was built
   against it, so a misclassification toward thesis cannot regress those
   contracts. The conversational redirect is a SEPARATE path triggered by
   a positive classifier signal, not a fall-through.

The classifier keeps shape-picking mostly stateless. QNT-216 relaxes that
rule only for continuation detection: recent transcript turns may be supplied
so an elliptical follow-up can route to ``followup`` instead of an off-domain
conversational redirect. Ticker choice and tool planning still belong outside
this module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Literal

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from shared.tickers import TICKER_NAME_ALIASES, TICKERS

from agent.llm import SMALL_NODE_ALIAS, get_llm
from agent.prompts import SIMPLE_GREETING_INPUTS, ConversationMessage
from agent.tools import _QUERY_MAX_LEN

# Bare greetings (canonical set, incl. common typos) plus "help" -- a one-word
# capability ask that, like a greeting, should short-circuit to conversational
# without burning the LLM classifier or any tools.
_GREETING_OR_HELP: frozenset[str] = SIMPLE_GREETING_INPUTS | {"help"}

logger = logging.getLogger(__name__)

# Intents the classifier LLM is allowed to emit. Kept as a distinct, narrower
# Literal from ``Intent`` so ``IntentDecision``'s JSON Schema enum never offers
# "exploration" to the model -- that value is set ONLY by
# ``explore_supervisor_node`` after routing, never picked by the classifier
# (QNT-220 follow-up).
ClassifierIntent = Literal[
    "thesis",
    "quick_fact",
    "comparison",
    "conversational",
    "fundamental",
    "technical",
    "news",
    "followup",
]

# Full set of response shapes carried in AgentState. Superset of
# ``ClassifierIntent`` with the internal-only "exploration" shape produced by
# the deterministic explore_supervisor scan (QNT-220 follow-up).
Intent = Literal[
    "thesis",
    "quick_fact",
    "comparison",
    "conversational",
    "fundamental",
    "technical",
    "news",
    "followup",
    "exploration",
]

# Which code path resolved the intent — written into AgentState by classify_node
# so Langfuse trace tags carry classifier_source:<value> (QNT-189).
ClassifierSource = Literal["heuristic", "llm", "fallback"]


class IntentDecision(BaseModel):
    """Structured-output schema for the classifier LLM call."""

    intent: ClassifierIntent = Field(
        description=(
            "The response shape to use. 'thesis' for open-ended investment "
            "questions that warrant a Setup / Bull / Bear / Verdict treatment. "
            "'quick_fact' for single-metric lookups where a short prose answer "
            "plus one cited value is enough. 'comparison' when the user asks "
            "to compare two tickers side-by-side. 'conversational' for "
            "greetings, capability asks, meta-questions, and clearly "
            "off-domain inputs (anything not about US public equities). "
            "'fundamental' when the user explicitly asks for a fundamental "
            "deep dive (valuation, earnings, margins) on one ticker. "
            "'technical' when the user explicitly asks for a technical "
            "analysis (price action, indicators, trend) on one ticker. "
            "'news' when the user asks for a news / headline read on one "
            "ticker."
        ),
    )
    needs_news_search: bool = Field(
        default=False,
        description=(
            "Set True when the question is TARGETED -- it asks about a specific "
            "named news event, development, entity, or competitive/market topic, "
            "rather than a generic overview. Examples that ARE targeted: "
            "litigation or a lawsuit, a regulatory probe / antitrust action, "
            "what an executive (CEO/CFO) said, a buyback or dividend change, a "
            "recall, a partnership / deal / collaboration / acquisition, a "
            "product launch, a specific guidance change, OR a competitive / "
            "market-segment angle such as 'the latest on Nvidia in the data "
            "center switching market' or 'how is AMD doing against Intel in "
            "server CPUs'. The targeting can be topical, not just a single named "
            "event -- if a specific story or angle would answer the question "
            "better than a generic digest, set True. "
            "This is independent of the 'intent' field above: a targeted news "
            "ask can be phrased as a quick_fact ('what did the CEO say about "
            "the buyback?') or a thesis ('is NVDA a buy given the lawsuit?') -- "
            "still set True. Set False ONLY for a GENERIC, topic-less news ask "
            "('what's the news on AAPL?', 'any headlines on META?', 'how's "
            "sentiment?') and for any question not about recent developments. "
            "True triggers a semantic news search over the headline archive; a "
            "generic news ask does not need it."
        ),
    )
    needs_earnings_search: bool = Field(
        default=False,
        description=(
            "Set True when the question asks about the MANAGEMENT NARRATIVE from "
            "an earnings release or call -- forward guidance / outlook, what "
            "management said about the quarter, margin or demand commentary, how "
            "management framed the results, or anything quoting / paraphrasing "
            "the earnings-call language. This is the qualitative 8-K narrative, "
            "NOT the raw numbers: a bare metric ask ('what's the EPS?', "
            "'what's the P/E?', 'what was revenue?') flows through the "
            "fundamental report and does NOT need it. Independent of the "
            "'intent' field: 'what did the CEO say about guidance?' is "
            "intent=quick_fact but needs_earnings_search=True. True triggers a "
            "semantic search over the earnings-release archive."
        ),
    )
    search_query: str = Field(
        default="",
        description=(
            "A self-contained retrieval query naming the ticker/entity and topic. "
            "Produce this ONLY when needs_news_search or needs_earnings_search is "
            "True; leave it empty ('') otherwise. On a warm thread, resolve "
            "pronouns and ellipses from the conversation history so the query "
            "stands alone without the transcript -- e.g. if the prior turn was "
            "about NVDA and the user asks 'what about the buyback?', the query "
            "should be 'NVDA buyback', not the bare 'the buyback'. For a cold "
            "question that already names its subject, just restate the "
            "ticker/entity and topic ('NVDA lawsuit' for 'what's the latest on "
            "the NVDA lawsuit?')."
        ),
    )
    # QNT-327 (v3 G-6, spike): fold the thesis plan pick into the classify call so
    # a thesis turn drops from four sequential LLM calls to three. Produced ONLY
    # when intent == "thesis"; every other intent leaves these at their defaults,
    # and plan_node falls back to the dedicated ThesisPlan call. The raw list is
    # permissive by design -- plan_node filters it to registered tools and
    # re-imposes the company + >=2-tool contract (mirroring the search_query
    # sanitize-downstream pattern), so an off-list or degenerate pick can only
    # trigger the fallback, never a bad plan.
    report_picks: list[str] = Field(
        default_factory=list,
        description=(
            "Produce this ONLY when intent == 'thesis'; leave it empty ([]) for "
            "every other intent. The report tools to fetch for the thesis, chosen "
            "from exactly: 'company', 'fundamental', 'technical', 'news'. A broad "
            "thesis ('give me a balanced thesis on NVDA', 'should I buy?') wants "
            "the FULL picture -- pick all four. Narrow only when the user names a "
            "specific lens: fundamental for valuation/earnings/margins, technical "
            "for chart/trend/RSI/setup, news for headlines/catalysts/sentiment. "
            "Always include 'company' -- it grounds the thesis in the business. "
            "Pick at least two."
        ),
    )
    plan_rationale: str = Field(
        default="",
        description=(
            "Produce this ONLY when intent == 'thesis' and you filled report_picks; "
            "leave it empty ('') otherwise. One or two analyst-voice sentences that "
            "cite what the question is asking about and why those reports fit -- the "
            "same voice the standalone planner uses, e.g. 'Your question is about "
            "valuation, so I'll lean on fundamentals and the company profile.' For a "
            "broad thesis, say the question asks for a full thesis, so all reports "
            "are needed."
        ),
    )


# Tokens that strongly suggest a single-metric lookup. Hits here force
# ``quick_fact`` without an LLM call. Lower-cased, matched as whole-word/
# token-bounded substrings.
#
# Bare ``price`` is intentionally absent: it would false-match "price
# target" and "price action" — both thesis-shaped asks — and bias the
# heuristic away from the safe default. The longer ``current price`` /
# ``last price`` / ``what's the price`` already cover the legitimate
# single-metric case.
_QUICK_FACT_TOKENS: tuple[str, ...] = (
    "rsi",
    "macd",
    "p/e",
    "pe ratio",
    "eps",
    "current price",
    "last price",
    "what is the price",
    "what's the price",
    "volume",
    "market cap",
    "dividend",
    "dividend yield",
)

# Phrases that strongly suggest a thesis-style ask. Hits here force
# ``thesis`` even if a quick-fact token also appears (e.g. "give me a
# thesis covering RSI and fundamentals" — the user wants a thesis).
_THESIS_TOKENS: tuple[str, ...] = (
    "thesis",
    "balanced",
    "bull case",
    "bear case",
    "should i buy",
    "should i sell",
    "is this a buy",
    "is this a sell",
    "walk me through",
    "deep dive",
    "investment case",
)

# Phrases that strongly suggest a comparison-style ask. Combined with a
# 2+ ticker mention (see ``_extract_tickers``) they force ``comparison``;
# a comparison phrase alone with only one named ticker is ambiguous and
# defers to the LLM. ``vs`` / ``versus`` / ``v.`` are the canonical
# multi-ticker connectives in finance writing; ``compared to`` /
# ``stack up`` / ``which is`` are common in chat.
_COMPARISON_TOKENS: tuple[str, ...] = (
    "vs",
    "versus",
    "compare",
    "compared to",
    "compared with",
    "stack up",
    "stacks up",
    "which is cheaper",
    "which is better",
    "head to head",
    "head-to-head",
    "side by side",
    "side-by-side",
)

# Greetings + capability + clearly off-domain tokens. Hits here force
# ``conversational`` — the agent shouldn't burn tools on "hi". The list
# is conservative: anything that could plausibly be about a ticker
# (e.g. "tell me about NVDA") goes through the LLM rather than getting
# trapped here.
_CONVERSATIONAL_TOKENS: tuple[str, ...] = (
    "hi",
    "hello",
    "hey",
    "yo",
    "good morning",
    "good afternoon",
    "good evening",
    "what can you do",
    "what do you do",
    "who are you",
    "what are you",
    "how does this work",
    "how do you work",
    "help me",
    "help",
    # Clearly off-domain — these are the scope examples in the QNT-156
    # ticket. The list is short on purpose; the LLM handles the long tail.
    "weather",
    "joke",
    "tell me a joke",
    "recipe",
    "song",
    "poem",
)

# QNT-176: focused-analysis trigger phrases. These pick a single report
# family for a deeper read than ``quick_fact`` but narrower than the full
# four-section ``thesis``. Each tuple is matched on whole-word boundaries
# (see ``_matches_any``), so partial-word collisions ("technical analysis"
# inside a longer prose run is fine; "tech" alone is not) are avoided.
#
# Phrasings explicitly opted into:
#  * "fundamental analysis", "fundamentals" -- the canonical English asks.
#  * "valuation deep dive", "valuation breakdown", "earnings deep dive" --
#    the user has already named the report family in plain English.
#  * "valuation read", "what does the balance sheet say", "expensive" --
#    natural-language framings observed in analyst-quality assessment (QNT-186).
#  * "technical analysis", "technicals", "ta on", "ta for" -- the abbreviated
#    "TA" form is finance-domain shorthand the chat sees often.
#  * "chart setup", "technical setup" -- "walk me through TSLA technical setup"
#    fails the heuristic without "technical setup"; "chart setup" for the
#    literal ask.
#  * "overbought", "oversold" -- RSI-level questions are technical reads, not
#    quick-fact single-value answers; routing them here is more accurate.
#  * "what do the charts say" -- explicit chart ask (QNT-186).
#  * "news sentiment", "what is the sentiment", "what's the sentiment" -- the
#    structured sentiment question.
#  * "news read" -- shorter framing of the same ask.
#  * "what's the news say", "how is sentiment", "any catalysts" --
#    natural-language variants observed in analyst-quality assessment (QNT-186).
_FUNDAMENTAL_TOKENS: tuple[str, ...] = (
    "fundamental analysis",
    "fundamentals",
    "valuation deep dive",
    "valuation breakdown",
    "earnings deep dive",
    "valuation read",
    "what does the balance sheet say",
    "expensive",
    "fundamental picture",
)

_TECHNICAL_ANALYSIS_TOKENS: tuple[str, ...] = (
    "technical analysis",
    "technicals",
    "ta on",
    "ta for",
    "chart setup",
    "technical setup",
    "what do the charts say",
    "overbought",
    "oversold",
)

# Tightened on review: bare ``sentiment on`` matched non-ticker phrasings like
# "market sentiment on the sector" and "based on recent sentiment on Wall
# Street"; bare ``headlines on`` had the same false-positive shape ("headlines
# on the bond market"). The remaining tokens all carry the word "news" or
# "sentiment" with enough surrounding context to be unambiguous focused asks
# — anything broader defers to the LLM classifier, which is the safer arm
# given the safe-default-to-thesis bias.
#
# QNT-208 dropped the ``news_sentiment`` intent in favour of plain ``news``;
# the tokens stay (users still ask about "sentiment") but the variable name
# tracks the new vocabulary.
_NEWS_TOKENS: tuple[str, ...] = (
    "news sentiment",
    "what is the sentiment",
    "what's the sentiment",
    "news read",
    "what's the news say",
    "how is sentiment",
    "any catalysts",
)

# QNT-209: pronoun-style follow-up triggers. The heuristic only fires when
# the caller signals there IS a prior turn (``has_prior_turn=True``) and no
# ticker is named — "why" / "elaborate" on their own carry no semantic
# anchor, and we want them to route to the followup synthesizer that reuses
# the prior turn's reports rather than re-fetching tools.
_FOLLOWUP_TOKENS: tuple[str, ...] = (
    "why",
    "how come",
    "tell me more",
    "elaborate",
    "expand on",
    "dig in",
    "what about that",
    "what does that mean",
    "what does this mean",
    "what does that imply",
    "what does this imply",
    "go deeper",
)

# QNT-214 follow-up: bare "give me a view" / "compare them" gestures that name
# no ticker. The LLM classifier frequently labels these ``conversational``
# (they carry no analytical keyword), which ploughs into a generic redirect
# instead of asking what to analyse. Detected post-classification by the
# graph's ambiguity gate and routed to clarify (needs_ticker /
# needs_second_ticker), mirroring the QNT-212 contract that a subject-less
# analysis ask should ask back rather than answer on the placeholder
# ``state.ticker``. ``CLARIFY_SYSTEM_PROMPT``'s needs_ticker branch already
# uses "what do you think?" as its worked example. Kept disjoint from
# ``_CONVERSATIONAL_TOKENS`` so greetings / capability asks stay conversational.
_VIEW_GESTURE_TOKENS: tuple[str, ...] = (
    "what do you think",
    "what's your take",
    "whats your take",
    "your take",
    "your view",
    "your opinion",
    "your read",
    "your thoughts",
    "thoughts",
    "what do you reckon",
    "how does it look",
    "how's it looking",
    "hows it looking",
)
_COMPARE_GESTURE_TOKENS: tuple[str, ...] = (
    "compare them",
    "compare these",
    "compare those",
    "compare the two",
    "compare both",
)
# Bare exploratory asks ("what's interesting?") that name no ticker. Treated as
# a "view" gesture so a tickerless scan routes to clarify ("interesting about
# which ticker?") instead of falling through to the generic conversational
# capability card. This tuple is also the graph's canonical broad-exploration
# route trigger set.
_EXPLORATION_TRIGGERS: tuple[str, ...] = (
    "what's interesting",
    "what is interesting",
    "whats interesting",
    "what stands out",
    "what should i watch",
    "anything interesting",
    "interesting about",
)


# QNT-280: keyword recall FLOOR for the news search trigger. The product
# decision now lives in the classify LLM's ``needs_news_search`` flag (semantic,
# catches topical/competitive phrasings the token list misses). This token
# matcher is demoted to a fallback floor: it fires on obvious named events the
# small model might overlook (``needs_news_search OR _is_targeted_news``) and is
# the sole signal on the heuristic short-circuit path where no LLM ran. It is
# tuned to NOT fire on a generic ask ("news on AAPL"), so OR-ing it can only add
# recall on targeted asks, never introduce a generic false positive.
_TARGETED_NEWS_TOKENS: tuple[str, ...] = (
    "litigation",
    "lawsuit",
    "lawsuits",
    "sue",
    "sued",
    "suing",
    "settlement",
    "ceo",
    "cfo",
    "executive",
    "buyback",
    "buybacks",
    "repurchase",
    "recall",
    "recalls",
    "investigation",
    "probe",
    "antitrust",
    "merger",
    "acquisition",
    "acquire",
    "acquires",
    "collaboration",
    "collaborate",
    "collaborates",
    "collaborating",
    "partnership",
    "partner",
    "deal",
    "stake",
    "layoff",
    "layoffs",
    "fraud",
    "sec",
    "catalyst",
    "catalysts",
)

_NEWS_QUERY_TOKENS: tuple[str, ...] = (
    "news",
    "headline",
    "headlines",
    "latest",
    "development",
    "developments",
    "update",
    "updates",
)

_TARGETED_NEWS_QUALIFIERS: tuple[str, ...] = (
    "with",
    "about",
    "regarding",
    "related to",
    "around",
    "concerning",
    "involving",
)

_GENERIC_QUALIFIER_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "company",
        "stock",
        "shares",
    }
)


def _specific_news_qualifier_phrase(question: str) -> str | None:
    """Return the entity/topic that narrows a broad-news ask, or None.

    Examples:
    * "latest news on NVDA" -> None (overview)
    * "latest news on NVDA with SK Hynix" -> "sk hynix" (RAG)
    * "headlines on META about antitrust" -> "antitrust" (RAG)

    QNT-322: the returned phrase is the topic half of the deterministic
    ticker+topic query the heuristic/fallback paths compose (see
    :func:`_floor_search_query`).
    """
    text = question.lower()
    if _matches_any(text, _NEWS_QUERY_TOKENS) is None:
        return None
    ticker_words = {ticker.lower() for ticker in TICKERS}
    for qualifier in _TARGETED_NEWS_QUALIFIERS:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(qualifier)}\s+([^?.,;:!]+)"
        for match in re.finditer(pattern, text):
            words = re.findall(r"[a-z0-9]+", match.group(1))
            specific_words = [
                word
                for word in words
                if word not in ticker_words and word not in _GENERIC_QUALIFIER_WORDS
            ]
            if specific_words:
                return " ".join(specific_words)
    return None


def _has_specific_news_qualifier(question: str) -> bool:
    """Return True for broad-news phrasing narrowed by an entity/topic."""
    return _specific_news_qualifier_phrase(question) is not None


def _is_targeted_news(question: str) -> bool:
    """Return True when a question's wording obviously names a targeted event.

    QNT-280: this is the keyword recall FLOOR, not the primary decider. The
    classify LLM's ``needs_news_search`` flag owns the product boundary (it
    catches topical/competitive phrasings this token list cannot); this matcher
    only adds recall on obvious named events and is the fallback on the
    heuristic short-circuit path. Tuned to stay False on a generic overview
    ("latest news on NVDA") so OR-ing it never fires a generic ask.
    """
    text = question.lower()
    return _matches_any(text, _TARGETED_NEWS_TOKENS) is not None or _has_specific_news_qualifier(
        question
    )


# QNT-263 / QNT-280: keyword recall FLOOR for the earnings-corpus trigger. The
# second RAG corpus (equity_earnings) holds 8-K Item 2.02 narrative --
# management framing, guidance language, quarterly highlights. As with
# _is_targeted_news, the primary decider is now the classify LLM's
# ``needs_earnings_search`` flag; this token matcher is the fallback floor
# (OR-ed under the flag, sole signal on the heuristic path). A question about
# that narrative ("what did management say about guidance?", "NVDA's outlook for
# the quarter") reaches the earnings corpus; the quantitative numbers
# (revenue/EPS/margins) already flow through the fundamental report and are NOT
# RAG material. Tokens carry "guidance"/"outlook"/"management"/"earnings call"
# signal -- broad phrasings like a bare "earnings" (which usually wants the
# number) are intentionally absent so the firing stays narrative-shaped.
# False-positive blast radius is contained by the intent gate (the gather node
# only fires earnings search for intents whose synthesis reads the earnings
# corpus -- _intent_reads_corpus over the QNT-288 policy table), the same way
# _is_targeted_news is gated to the news-reading intents.
_EARNINGS_SEARCH_TOKENS: tuple[str, ...] = (
    "guidance",
    "guided",
    "outlook",
    "forecast",
    "earnings call",
    "earnings release",
    "earnings report",
    "earnings commentary",
    "management commentary",
    "management said",
    "what did management say",
    "how did management",
    "press release",
    "quarterly results",
    "raised its guidance",
    "cut its guidance",
)


def _is_earnings_search(question: str) -> bool:
    """Return True when a question's wording obviously names earnings narrative.

    QNT-280: keyword recall FLOOR mirroring :func:`_is_targeted_news`. The
    classify LLM's ``needs_earnings_search`` flag is the primary decider; this
    matcher adds recall on obvious narrative asks (management framing, guidance,
    outlook) and is the fallback on the heuristic path. Independent of intent --
    the gather node gates the actual fetch to the intents whose synthesis reads
    the fundamental report.
    """
    return _matches_any(question.lower(), _EARNINGS_SEARCH_TOKENS) is not None


def _floor_search_query(question: str) -> str:
    """Deterministic ticker+topic retrieval query for the paths with no LLM
    rewrite (``heuristic`` short-circuit and ``fallback``).

    QNT-322 (G-11): the LLM classify path rewrites elliptical follow-ups into a
    self-contained query (QNT-289); the heuristic/fallback paths used to return
    ``""`` and hand Qdrant the raw (often elliptical) question instead. This
    composes the same shape without an LLM call: the ticker the question names
    (if any) plus the keyword floor that fired -- the matched news/earnings
    token or the specific news-qualifier phrase. Returns ``""`` when no floor
    matched, which is exactly when ``_is_targeted_news`` / ``_is_earnings_search``
    are both False, so a caller can never emit a non-empty query while reporting
    both search flags False -- OR when the composed string would exceed the
    tool-side ``_QUERY_MAX_LEN`` cap (a pathological, punctuation-free qualifier
    run), mirroring :func:`sanitize_search_query` so an over-long query falls
    back to the raw question rather than degrading to "[]" hits.
    """
    text = question.lower()
    topic = (
        _matches_any(text, _TARGETED_NEWS_TOKENS)
        or _matches_any(text, _EARNINGS_SEARCH_TOKENS)
        or _specific_news_qualifier_phrase(question)
    )
    if topic is None:
        return ""
    tickers = extract_tickers(question)
    ticker = tickers[0] if tickers else ""
    query = f"{ticker} {topic}".strip()
    return query if len(query) <= _QUERY_MAX_LEN else ""


def route_search_corpora(needs_news_search: bool, needs_earnings_search: bool) -> tuple[str, ...]:
    """Compose the two search-trigger flags into the ordered set of RAG corpora.

    QNT-263 multi-corpus routing; QNT-280 made the flags semantic. ``()`` means
    neither fires (the canned digests carry the answer); ``("news", "earnings")``
    means a query that spans both ("what did the CEO say about guidance?" -- a
    named-executive news event AND a guidance ask).

    This MUST stay a pure OR over the SAME two booleans the runtime writes to
    state (``needs_news_search`` -> news, ``needs_earnings_search`` -> earnings,
    both resolved by :func:`classify_intent_with_source`): the gather node gates
    each corpus on those exact flags, and the routing eval composes this function
    over the live flags. That is what makes "what the eval scores == what the
    agent does" hold -- do NOT add routing logic here that the gather node won't
    see, and do NOT re-derive the flags from the raw question text here (that
    would re-introduce the keyword-gate drift QNT-280 removed).
    """
    corpora: list[str] = []
    if needs_news_search:
        corpora.append("news")
    if needs_earnings_search:
        corpora.append("earnings")
    return tuple(corpora)


def underspecified_gesture(question: str) -> Literal["view", "compare"] | None:
    """Return 'compare'/'view' if ``question`` is a subject-less analysis ask.

    These are the bare "what do you think?" / "compare them" / "what's
    interesting?" phrasings that carry analytical intent but name no ticker.
    The graph's ambiguity gate uses this (alongside a no-ticker / no-prior-turn
    guard) to route such asks to clarify. Returns None for everything else,
    including greetings and capability asks, which match a disjoint token list.
    Whole-word matched via :func:`_matches_any`.
    """
    text = question.lower().strip()
    if _matches_any(text, _COMPARE_GESTURE_TOKENS) is not None:
        return "compare"
    if _matches_any(text, _VIEW_GESTURE_TOKENS) is not None:
        return "view"
    if _matches_any(text, _EXPLORATION_TRIGGERS) is not None:
        return "view"
    return None


def has_comparison_phrase(question: str) -> bool:
    """Return True when ``question`` uses comparison-shaped wording."""
    return _matches_any(question.lower(), _COMPARISON_TOKENS) is not None


# QNT-358: deterministic axis detector for the comparison plan. When a
# comparison question names exactly ONE report axis ("compare TSLA vs AMD on
# technical momentum"), plan_node narrows the (symmetric) plan to
# ``["company", <axis>]`` -- the same three focus axes the single-ticker
# focused path already has, with company riding along as grounding. This is a
# distinct token set from the single-ticker ``_FUNDAMENTAL_TOKENS`` /
# ``_TECHNICAL_ANALYSIS_TOKENS`` / ``_NEWS_TOKENS`` above: those are tuned to
# fire the focused INTENT (deliberately conservative, deferring the ambiguous
# middle to the LLM), whereas the intent here is already resolved to
# ``comparison`` -- so this set can afford the plainer axis words ("valuation",
# "momentum", "headlines") a user naturally uses to name the contrast axis.
# Whole-word matched via :func:`_matches_any`.
_COMPARISON_AXIS_TOKENS: dict[str, tuple[str, ...]] = {
    "fundamental": (
        "fundamental",
        "fundamentals",
        "valuation",
        "valuations",
        "earnings",
        "margin",
        "margins",
        "p/e",
        "pe ratio",
        "eps",
        "revenue",
        "multiple",
        "multiples",
        "balance sheet",
        "cash flow",
    ),
    "technical": (
        "technical",
        "technicals",
        "momentum",
        "chart",
        "charts",
        "chart setup",
        "rsi",
        "macd",
        "trend",
        "trends",
        "moving average",
        "overbought",
        "oversold",
        "price action",
    ),
    "news": (
        "news",
        "headline",
        "headlines",
        "catalyst",
        "catalysts",
        "sentiment",
        "developments",
    ),
}


def comparison_axis(question: str) -> str | None:
    """Return the single report axis a comparison question names, or None.

    QNT-358: ``"fundamental"`` / ``"technical"`` / ``"news"`` when the question
    names exactly one axis; ``None`` when it names zero (a bare "compare NVDA vs
    AMD" wants the full four-aspect matrix) OR more than one (a cross-domain ask
    like "on fundamentals and technicals" is not a single-axis narrow -- mirrors
    the single-ticker focused heuristic's ``len(hits) == 1`` gate). ``company``
    is never an axis on its own -- it is always-included grounding, matching the
    single-ticker focused path.
    """
    text = question.lower()
    hits = [
        axis
        for axis, tokens in _COMPARISON_AXIS_TOKENS.items()
        if _matches_any(text, tokens) is not None
    ]
    return hits[0] if len(hits) == 1 else None


# Short questions are more likely quick-fact. Tuned conservatively: a 12-word
# question can still be open-ended, so this is one signal among several.
_SHORT_QUESTION_WORD_LIMIT = 12

# A comparison ask must name at least 2 tickers from our coverage list AND
# carry a comparison phrase. ``extract_tickers`` accepts either the symbol or
# the company name on an alpha word boundary (no slashes — ``$NVDA`` and
# ``NVDA's`` are tolerated by the strip later).
#
# QNT-257: company-name -> ticker resolution. ``extract_tickers`` recognises the
# plain company name a user types ("micron", "google", "tesla"), not just the
# symbol, so a name-only chat ask no longer bounces to the clarify node. The
# alias data lives next to the registry in ``shared.tickers``; this layer only
# compiles it into a matcher.
#
# One combined regex matches either a symbol OR a name alias, case-insensitively
# on alpha word boundaries. The alternation tokens are lower-cased and set-deduped
# (matching is IGNORECASE, so a symbol whose short name equals it -- META vs the
# "Meta" alias -- would otherwise be a redundant alternative), then sorted
# longest-first so a multi-word alias ("Advanced Micro Devices") wins over any
# contained token. Each match is mapped back to its ticker via
# ``_NAME_ALIAS_TO_TICKER`` (symbols resolve to themselves).
_TICKERS_SET: frozenset[str] = frozenset(TICKERS)
_NAME_ALIAS_TO_TICKER: dict[str, str] = {
    alias.lower(): ticker for ticker, aliases in TICKER_NAME_ALIASES.items() for alias in aliases
}
_REFERENCE_TOKENS: list[str] = sorted(
    {token.lower() for token in (*TICKERS, *_NAME_ALIAS_TO_TICKER.keys())},
    key=len,
    reverse=True,
)
_TICKER_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(t) for t in _REFERENCE_TOKENS) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _matches_any(text: str, tokens: tuple[str, ...]) -> str | None:
    """Return the first token from ``tokens`` that appears in ``text``,
    or None. Whole-word boundary check so 'price' doesn't match 'priced'."""
    for tok in tokens:
        # Token can contain spaces or slashes — escape the whole thing and
        # require it sit at a word boundary on both ends. ``re.escape`` keeps
        # 'p/e' literal.
        pattern = rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])"
        if re.search(pattern, text):
            return tok
    return None


def extract_tickers(text: str) -> list[str]:
    """Return the unique tickers mentioned in ``text``, in first-occurrence order.

    Matches both the literal symbol (``MU``) and the company name a user types
    (``micron``), case-insensitively on alpha word boundaries (QNT-257). A
    symbol and its name in the same question collapse to one entry ("compare
    Micron and MU" -> ``["MU"]``).

    Public so the graph can reuse the same parser for the comparison node
    without duplicating the regex.
    """
    seen: list[str] = []
    for match in _TICKER_REFERENCE_RE.finditer(text):
        token = match.group(1)
        # Every matched token came from TICKERS or _NAME_ALIAS_TO_TICKER (the only
        # alternation sources), so the lookup below can't KeyError: a non-symbol
        # match is always a known alias key.
        ticker = (
            token.upper() if token.upper() in _TICKERS_SET else _NAME_ALIAS_TO_TICKER[token.lower()]
        )
        if ticker not in seen:
            seen.append(ticker)
    return seen


# QNT-289: ticker-shaped token (2-5 uppercase letters) used by the
# hallucinated-entity guard below. Common finance/business acronyms that are
# NOT tickers are excluded so a legitimate rewrite like "NVDA CEO comments"
# doesn't get rejected for naming "CEO".
_TICKER_LIKE_RE = re.compile(r"(?<![A-Za-z])[A-Z]{2,5}(?![A-Za-z])")
_NON_TICKER_ACRONYMS: frozenset[str] = frozenset(
    {
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "IPO",
        "SEC",
        "FDA",
        "GDP",
        "EPS",
        "ROI",
        "ETF",
        "USD",
        "API",
        "KPI",
        "ESG",
        "LLC",
        "INC",
        "LTD",
        "SMA",
        "RSI",
        "MACD",
        "US",
        "UK",
        "EU",
        "AI",
        "IT",
        "HR",
        "PR",
        "YOY",
        "QOQ",
    }
)


def _token_in_context(token: str, *contexts: str) -> bool:
    """True when ``token`` appears as a whole word in any context string
    (case-insensitive). Word-boundary matched so "SK" doesn't hit "ask"."""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
    return any(re.search(pattern, ctx, re.IGNORECASE) for ctx in contexts if ctx)


def sanitize_search_query(search_query: str, *, question: str = "", history_text: str = "") -> str:
    """Guardrail the classifier's rewritten retrieval query before use.

    Returns the trimmed query when it passes both guards, or ``""`` when it
    should be rejected. Callers fall back to the raw question on ``""`` --
    the QNT-280 keyword-floor pattern: the fallback IS today's behaviour, so a
    rejection can never regress cold-turn retrieval, only decline an upgrade.

    * Length cap -- mirrors the tool-side ``_QUERY_MAX_LEN`` cap in
      ``agent.tools`` (``search_news`` / ``search_earnings`` already degrade
      to "[]" on an over-long query; rejecting here instead falls back to the
      raw question rather than silently returning no hits).
    * Hallucinated-entity guard -- a ticker-shaped token (2-5 uppercase
      letters, excluding common finance acronyms) that is NOT in
      ``shared.tickers.TICKERS`` means the model named a company outside our
      coverage universe; reject rather than search on a ticker that isn't ours.
      QNT-322 (G-10): a ticker-shaped token is only treated as hallucinated
      when it appears in NEITHER the raw ``question`` nor the ``history_text``
      rendered to the classifier -- a competitive entity named anywhere in the
      thread (SK, TSMC, ASML), whether the user typed it or a prior assistant
      turn surfaced it from real data, survives; an invented one still dies.
    """
    query = search_query.strip()
    if not query:
        return ""
    if len(query) > _QUERY_MAX_LEN:
        return ""
    for match in _TICKER_LIKE_RE.finditer(query):
        token = match.group(0)
        if token in _NON_TICKER_ACRONYMS:
            continue
        if token in _TICKERS_SET:
            continue
        if _token_in_context(token, question, history_text):
            continue
        return ""
    return query


def _heuristic_intent(question: str, *, has_prior_turn: bool = False) -> Intent | None:
    """Cheap keyword classifier.

    Returns the intent when the heuristic is confident, or None to defer to
    the LLM. The LLM is the fallback, not the primary — most questions in
    the chat are short and word-spotty enough for this layer to handle.

    QNT-216: ``has_prior_turn`` is True when the caller hydrated either
    report state or recent transcript messages from an earlier turn. It gates
    followup detection — bare "why?" without context has no anchor and falls
    through to the safe default.
    """
    text = question.strip().lower()
    if not text:
        # No question = no override; the existing graph already defaults the
        # thesis path for empty messages, and the LLM call would be wasted.
        return "thesis"

    # QNT-209: followup pronouns. Must run BEFORE the thesis bias so a short
    # "why?" with a prior turn routes here instead of falling into the safe
    # default. Requires: short question, no ticker named (the user is
    # gesturing at the previous answer, not naming a new one), a followup
    # token, AND a prior turn on this thread. Without all four signals we
    # defer — bare "why?" with no context is ambiguous and the safe default
    # still applies.
    if (
        has_prior_turn
        and len(text.split()) <= _SHORT_QUESTION_WORD_LIMIT
        and not extract_tickers(question)
        and _matches_any(text, _FOLLOWUP_TOKENS) is not None
    ):
        return "followup"

    # Conversational greetings / capability asks are unambiguous — short
    # greetings ("hi", "hey") are usually the WHOLE message, so a strict
    # equality check or a small-leading-tokens check catches them without
    # false-firing on a longer question that happens to start with "hi".
    stripped = text.rstrip("?!.,")
    if stripped in _GREETING_OR_HELP:
        return "conversational"
    conv_token = _matches_any(text, _CONVERSATIONAL_TOKENS)
    if conv_token is not None and conv_token not in _GREETING_OR_HELP:
        # Multi-word phrases (e.g. "what can you do", "weather") are strong
        # enough to fire on their own — they almost never appear inside a
        # legitimate equities question. Safety net: if the question also
        # names a covered ticker, defer to the LLM. This blocks the
        # "help me understand NVDA's RSI" false positive — the user
        # asked about a ticker, not for help with the tool.
        if not extract_tickers(question):
            return "conversational"

    # Comparison: 2+ tickers AND a comparison phrase. Either signal alone
    # is too noisy ("Compare NVDA's RSI to its 200-day SMA" mentions one
    # ticker; "vs" can show up inside news headlines without intent).
    if _matches_any(text, _COMPARISON_TOKENS) is not None:
        if len(extract_tickers(question)) >= 2:
            return "comparison"

    # QNT-176: focused-analysis intents are checked BEFORE thesis tokens so
    # phrases like "walk me through META's fundamentals" (which would
    # otherwise match the thesis token "walk me through") and "valuation
    # deep dive" (which would otherwise match the thesis token "deep dive")
    # route to the narrower path the user explicitly asked for.
    #
    # Mutual exclusivity: if the question names MORE than one focus
    # ("triangulate technicals AND fundamentals"), the user wants a
    # synthesis across domains — that's a thesis, not a focused read.
    # Only fire the focused branch when exactly one focus matches.
    focused_hits: list[Intent] = []
    if _matches_any(text, _FUNDAMENTAL_TOKENS) is not None:
        focused_hits.append("fundamental")
    if _matches_any(text, _NEWS_TOKENS) is not None:
        focused_hits.append("news")
    if _matches_any(text, _TECHNICAL_ANALYSIS_TOKENS) is not None:
        focused_hits.append("technical")
    if len(focused_hits) == 1:
        return focused_hits[0]

    if _matches_any(text, _THESIS_TOKENS):
        return "thesis"

    quick_token = _matches_any(text, _QUICK_FACT_TOKENS)
    word_count = len(text.split())
    if quick_token is not None and word_count <= _SHORT_QUESTION_WORD_LIMIT:
        return "quick_fact"

    return None


def _render_history_for_classifier(
    history: Sequence[ConversationMessage] | None,
) -> str:
    """Compact recent transcript for continuation classification only."""
    if not history:
        return "(none)"
    lines: list[str] = []
    for item in history[-6:]:
        role = item.get("role", "user")
        content = item.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content[:600]}")
    return "\n".join(lines) if lines else "(none)"


_CLASSIFY_PROMPT = """You classify a user's question to pick an answer shape.

Respond with the structured field 'intent' set to one of:

* "quick_fact" — the user is asking for a single value or a yes/no read on \
one metric (e.g. "What's the RSI?", "What's MSFT's P/E?", "What's the \
volume today?"). The answer is one or two sentences plus one cited number.
* "thesis" — the user is asking for a balanced, multi-source view, an \
investment recommendation, or a walk-through (e.g. "Should I be cautious \
about META?", "Give me a balanced thesis on V", "Walk me through NVDA's \
setup"). The answer is a Setup / Bull / Bear / Verdict treatment.
* "comparison" — the user wants to compare two tickers side-by-side \
(e.g. "Compare NVDA vs AAPL", "How does META stack up against GOOGL on \
margins?", "Which is cheaper, V or MA?"). Pick this ONLY when the question \
clearly names or implies two tickers AND asks for a contrast.
* "conversational" — the user said hi, asked what the agent can do, asked \
a meta question about the tool itself, or asked something clearly off-topic \
(e.g. "what's the weather?", "tell me a joke", "hello", "what can you do?"). \
The agent should answer briefly and redirect to its actual capabilities; \
it must NOT fabricate an equities answer.
* "fundamental" — the user asked for a fundamental deep dive on one ticker \
(e.g. "walk me through META's fundamentals", "valuation deep dive on AAPL", \
"how is MSFT valued?", "valuation read on TSLA", "is NVDA expensive?", \
"what does the balance sheet say about AAPL?"). The answer is a focused \
multi-sentence read on valuation, earnings, and margins for that ticker — \
narrower than a full thesis, deeper than a single number.
* "technical" — the user asked for a technical analysis on one ticker \
(e.g. "give me the technical analysis of NVDA", "how do the technicals \
look on AAPL?", "chart setup for TSLA", "Walk me through TSLA technical \
setup", "what do the charts say for META?", "is AAPL overbought?", \
"is TSLA oversold?"). The answer is a focused read on price action, \
indicators, and trend.
* "news" — the user asked for a news / headline read on one ticker \
(e.g. "what's the news on AAPL?", "headlines on META", "any concerning \
news for INTC?", "what's the news say on NVDA?", "how is sentiment on \
META?", "any catalysts for TSLA?"). The answer is a focused read on \
recent headlines (positive and negative catalysts) tied to the running \
story.
* "followup" — the user is referring back to your prior turn; the question \
does not name a ticker or metric directly (e.g. "why?", "tell me more", \
"elaborate on the bear case", "go deeper"). Only pick this when the \
question reads like a continuation of the immediately preceding answer; \
a question that names a new ticker, metric, or topic is NOT a followup \
even if it's short.

If you are uncertain between "thesis" and "quick_fact", default to "thesis" \
— that path is the existing safe shape. Pick "comparison" only when there \
is an explicit multi-ticker contrast in the question. Pick "conversational" \
only for greetings, capability asks, or clearly off-domain inputs — \
ambiguous equity questions should NOT route here. Pick "fundamental", \
"technical", or "news" ONLY when the user explicitly named that \
domain; an open-ended "should I buy NVDA?" still routes to "thesis" even \
though the answer happens to lean on fundamentals.

Separately, set the boolean 'needs_news_search' True when the question is \
TARGETED -- it asks about a specific named news event, development, entity, or \
competitive/market topic, rather than a generic overview: litigation / a \
lawsuit, a regulatory probe or antitrust action, what an executive said, a \
buyback or dividend change, a recall, a partnership / deal / collaboration / \
acquisition, a product launch, a specific guidance change, OR a competitive / \
market-segment angle such as "the latest on Nvidia in the data center \
switching market" or "how is AMD doing against Intel in server CPUs". The \
targeting can be topical, not just a single named event -- if a specific story \
or angle would answer the question better than a generic digest, set True. \
This is independent of the intent label: "what did the CEO say about the \
buyback?" is intent=quick_fact but needs_news_search=True; "is NVDA a buy given \
the lawsuit?" is intent=thesis but needs_news_search=True. Set it False ONLY \
for a generic, topic-less news ask ("what's the news on AAPL?", "any \
headlines?", "how's sentiment?") and for anything not about recent developments.

Separately, set the boolean 'needs_earnings_search' True when the question \
asks about the MANAGEMENT NARRATIVE from an earnings release or call -- forward \
guidance / outlook, what management said about the quarter, margin or demand \
commentary, how management framed the results, or anything quoting the \
earnings-call language. This is the qualitative narrative, NOT the raw numbers: \
a bare metric ask ("what's the EPS?", "what's the P/E?", "what was revenue?") \
flows through the fundamental report and does NOT need it. Independent of the \
intent label: "what did the CEO say about guidance?" is intent=quick_fact but \
needs_earnings_search=True. A question can set BOTH flags ("what did the CEO \
say about guidance?" -- a named-executive event AND a guidance ask).

Separately, when either search flag above is True, set 'search_query' to a \
self-contained retrieval query naming the ticker/entity and topic -- this is \
the ONE field allowed to pull a ticker/entity out of the conversation below. \
If the question itself already names its subject ("is NVDA involved in a \
lawsuit?"), just restate it ("NVDA lawsuit"). If the question is elliptical \
and only makes sense given the prior turn ("what about the buyback?" after an \
NVDA turn), resolve the missing ticker/entity from the conversation ("NVDA \
buyback"). Leave 'search_query' as "" when neither search flag is True.

Separately, ONLY when the 'intent' above is "thesis", set 'report_picks' to the \
report tools to fetch for the thesis, chosen from exactly: "company", \
"fundamental", "technical", "news". A broad thesis request ("give me a balanced \
thesis on NVDA", "should I be cautious about META?") wants the full investment \
picture -- pick all four. Narrow ONLY when the user names a specific lens: \
"fundamental" for valuation / earnings / margins, "technical" for chart / trend \
/ RSI / setup, "news" for headlines / catalysts / sentiment. Always include \
"company"; it grounds the thesis in the business. Pick at least two. Then set \
'plan_rationale' to one or two analyst-voice sentences citing what the question \
asks and why those reports fit (e.g. "Your question is about valuation, so I'll \
lean on fundamentals and the company profile."; for a broad thesis, say the \
question asks for a full thesis, so all reports are needed). For EVERY non-thesis \
intent, leave 'report_picks' empty ([]) and 'plan_rationale' empty ("").

Recent conversation (for follow-up detection and for resolving 'search_query' \
as described above ONLY; do not use it to change the 'intent' label, pick \
tools, compute values, or switch which ticker this turn analyzes):
{history}

Question: {question}
"""


def classify_intent_with_source(
    question: str,
    *,
    config: RunnableConfig | None = None,
    has_prior_turn: bool = False,
    history: Sequence[ConversationMessage] | None = None,
) -> tuple[Intent, ClassifierSource, bool, bool, str, list[str], str]:
    """Return ``(intent, source, needs_news_search, needs_earnings_search,
    search_query, report_picks, plan_rationale)``.

    ``source`` identifies which code path resolved the intent:
    - ``"heuristic"`` — keyword matcher decided without an LLM call
    - ``"llm"`` — heuristic abstained; structured-output call succeeded
    - ``"fallback"`` — LLM call failed/timed out or returned unexpected shape;
      biased to ``"thesis"`` as the safe default

    QNT-280: the two search-trigger flags (``needs_news_search`` /
    ``needs_earnings_search``) are now SEMANTIC -- carried by the classify LLM's
    structured output, which catches topical/competitive phrasings the old
    keyword gates missed (e.g. "the latest on Nvidia in the data center
    switching market"). The deterministic keyword deciders (``_is_targeted_news``
    / ``_is_earnings_search``) are demoted to a recall FLOOR: OR-ed under the LLM
    flag on the ``llm`` path (so an obvious named event the small model overlooks
    still fires) and the sole signal on the ``heuristic`` short-circuit and
    ``fallback`` paths (where no usable LLM judgment exists). Both flags are
    independent of the intent label -- a targeted ask can be quick_fact or
    thesis. The keyword floor is tuned to stay False on generic asks, so OR-ing
    it can only add recall on targeted asks, never fire a generic one.

    QNT-289: ``search_query`` is the classifier's self-contained retrieval
    query (ticker/entity + topic, pronouns/ellipses resolved from history),
    guardrailed by :func:`sanitize_search_query` (length cap + context-aware
    hallucinated-entity rejection).

    QNT-322: EVERY path composes a deterministic ticker+topic query from the
    keyword floor that fired (:func:`_floor_search_query`) so a targeted ask
    never hands Qdrant the raw ellipsis. The ``heuristic`` and ``fallback``
    paths (no LLM rewrite ran) return it directly; the ``llm`` path prefers the
    model's own rewrite and falls back to the same floor query when that rewrite
    is empty or rejected (QNT-322 follow-up -- closes the last asymmetry, where
    a misclassify/skip left flag=True with an empty query). Still ``""`` when no
    floor fired, in which case callers fall back to the raw question -- today's
    behaviour, so this can only add recall, never regress it.

    QNT-181: ``config`` carries the LangGraph CallbackHandler so the
    LLM-fallback path's generation observation nests under the parent trace.

    QNT-216: ``history`` is rendered into the LLM classifier prompt only for
    the followup/ambiguity arm. The heuristic still handles obvious
    continuation phrasing without an LLM call.

    QNT-327 (v3 G-6, spike): ``report_picks`` + ``plan_rationale`` fold the
    thesis plan pick into this call so a thesis turn drops from four sequential
    LLM calls to three. They are produced ONLY on the ``llm`` path for a
    ``thesis`` intent; the ``heuristic`` and ``fallback`` paths (and every
    non-thesis intent) return ``([], "")`` so plan_node falls back to the
    dedicated ThesisPlan call. The picks are passed through raw here -- plan_node
    filters them to registered tools and re-imposes the company + >=2-tool
    contract, so an off-list or degenerate pick can only trigger the fallback.
    """
    # Keyword floor -- the fallback signal, also OR-ed under the LLM flag below.
    floor_news = _is_targeted_news(question)
    floor_earnings = _is_earnings_search(question)
    # QNT-322 (G-11): deterministic ticker+topic query for the no-LLM paths.
    # Non-empty iff a floor fired, so it never disagrees with the flags above.
    floor_query = _floor_search_query(question)

    has_context = has_prior_turn or bool(history)
    heuristic = _heuristic_intent(question, has_prior_turn=has_context)
    if heuristic is not None:
        logger.info(
            "classify intent=%s search_query=%r via=heuristic question=%r",
            heuristic,
            floor_query,
            question[:80],
        )
        # QNT-327: no LLM ran -> no folded plan picks; plan_node falls back to
        # the dedicated ThesisPlan call for a heuristic-thesis turn.
        return heuristic, "heuristic", floor_news, floor_earnings, floor_query, [], ""

    # QNT-220 (#7): the classifier is a small structured call -- tier it to the
    # fast/small alias rather than the 70b synthesis model.
    structured_llm = get_llm(temperature=0.0, model_alias=SMALL_NODE_ALIAS).with_structured_output(
        IntentDecision
    )
    history_text = _render_history_for_classifier(history)
    try:
        response = structured_llm.invoke(
            _CLASSIFY_PROMPT.format(history=history_text, question=question.strip()),
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 — bias to thesis on any failure
        logger.warning("classify llm failed, defaulting to thesis: %s", exc)
        return "thesis", "fallback", floor_news, floor_earnings, floor_query, [], ""

    decision: IntentDecision | None = None
    if isinstance(response, IntentDecision):
        decision = response
    elif isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, IntentDecision):
            decision = parsed
    if decision is not None:
        needs_news_search = decision.needs_news_search or floor_news
        needs_earnings_search = decision.needs_earnings_search or floor_earnings
        # QNT-322 follow-up: when the model's own rewrite is empty or rejected
        # but a keyword floor fired, fall back to the SAME deterministic
        # ticker+topic query the heuristic/fallback paths compose, rather than
        # handing Qdrant the raw (often elliptical) question. Closes the last
        # asymmetry -- the LLM path could still emit flag=True with an empty
        # query when the small model misclassified or skipped the rewrite. The
        # ``or`` keeps a valid model rewrite untouched; ``floor_query`` is
        # non-empty only when a floor fired, so the flag/query invariant holds.
        search_query = (
            sanitize_search_query(
                decision.search_query, question=question, history_text=history_text
            )
            or floor_query
        )
        # QNT-327 (v3 G-6): fold the thesis plan pick out of this call. Only a
        # thesis intent carries picks -- the schema instructs the model to leave
        # them empty otherwise, but gate on the intent here too so a stray pick on
        # a non-thesis intent (which plan_node never reads) can't leak into state.
        report_picks = list(decision.report_picks) if decision.intent == "thesis" else []
        plan_rationale = decision.plan_rationale.strip() if decision.intent == "thesis" else ""
        logger.info(
            "classify intent=%s needs_news_search=%s needs_earnings_search=%s "
            "search_query=%r report_picks=%s via=llm question=%r",
            decision.intent,
            needs_news_search,
            needs_earnings_search,
            search_query,
            report_picks,
            question[:80],
        )
        return (
            decision.intent,
            "llm",
            needs_news_search,
            needs_earnings_search,
            search_query,
            report_picks,
            plan_rationale,
        )
    logger.warning("classify llm returned unexpected shape, defaulting to thesis")
    return "thesis", "fallback", floor_news, floor_earnings, floor_query, [], ""


def classify_intent(
    question: str,
    *,
    config: RunnableConfig | None = None,
    has_prior_turn: bool = False,
    history: Sequence[ConversationMessage] | None = None,
) -> Intent:
    """Return the response shape to use for ``question``.

    Thin wrapper around :func:`classify_intent_with_source` for callers that
    only need the intent. Use ``classify_intent_with_source`` when the
    classifier path (heuristic / llm / fallback) also matters.
    """
    intent, *_ = classify_intent_with_source(
        question,
        config=config,
        has_prior_turn=has_prior_turn,
        history=history,
    )
    return intent


__all__ = [
    "ClassifierIntent",
    "ClassifierSource",
    "Intent",
    "IntentDecision",
    "_is_earnings_search",
    "classify_intent",
    "classify_intent_with_source",
    "comparison_axis",
    "extract_tickers",
    "has_comparison_phrase",
    "route_search_corpora",
    "sanitize_search_query",
    "underspecified_gesture",
]
