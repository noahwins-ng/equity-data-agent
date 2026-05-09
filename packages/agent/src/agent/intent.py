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
  thesis card. For single-metric lookups ("what's NVDA's P/E?", "is AAPL
  overbought?").
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

The classifier is intentionally stateless: it sees only the question
string. Ticker, prior runs, and tools are not inputs — adding them would
push toward "rich classifier with planning bias", which belongs in the
plan node, not here.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from shared.tickers import TICKERS

from agent.llm import get_llm

logger = logging.getLogger(__name__)

Intent = Literal[
    "thesis",
    "quick_fact",
    "comparison",
    "conversational",
    "fundamental",
    "technical",
    "news_sentiment",
]


class IntentDecision(BaseModel):
    """Structured-output schema for the classifier LLM call."""

    intent: Intent = Field(
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
            "'news_sentiment' when the user asks for a news / headline / "
            "sentiment read on one ticker."
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
#  * "technical analysis", "technicals", "ta on", "ta for" -- the abbreviated
#    "TA" form is finance-domain shorthand the chat sees often.
#  * "chart setup" -- common phrasing for "what does the chart say?".
#  * "news sentiment", "sentiment on", "what is the sentiment" -- the
#    structured sentiment question.
#  * "headlines on", "news read" -- shorter framings of the same ask.
_FUNDAMENTAL_TOKENS: tuple[str, ...] = (
    "fundamental analysis",
    "fundamentals",
    "valuation deep dive",
    "valuation breakdown",
    "earnings deep dive",
)

_TECHNICAL_ANALYSIS_TOKENS: tuple[str, ...] = (
    "technical analysis",
    "technicals",
    "ta on",
    "ta for",
    "chart setup",
)

# Tightened on review: bare ``sentiment on`` matched non-ticker phrasings like
# "market sentiment on the sector" and "based on recent sentiment on Wall
# Street"; bare ``headlines on`` had the same false-positive shape ("headlines
# on the bond market"). The remaining tokens all carry the word "news" or
# "sentiment" with enough surrounding context to be unambiguous focused asks
# — anything broader defers to the LLM classifier, which is the safer arm
# given the safe-default-to-thesis bias.
_NEWS_SENTIMENT_TOKENS: tuple[str, ...] = (
    "news sentiment",
    "what is the sentiment",
    "what's the sentiment",
    "news read",
)

# Short questions are more likely quick-fact. Tuned conservatively: a 12-word
# question can still be open-ended, so this is one signal among several.
_SHORT_QUESTION_WORD_LIMIT = 12

# A comparison ask must name at least 2 tickers from our coverage list AND
# carry a comparison phrase. Heuristic accepts the upper-cased symbol on a
# word boundary (no slashes — ``$NVDA`` and ``NVDA's`` are tolerated by the
# strip later, but the boundary check is alpha-only).
_TICKER_BOUNDARY_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(t) for t in TICKERS) + r")(?![A-Za-z])"
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
    """Return the unique tickers from ``shared.tickers.TICKERS`` mentioned
    in ``text``, in first-occurrence order.

    Public so the graph can reuse the same parser for the comparison node
    without duplicating the regex.
    """
    seen: list[str] = []
    for match in _TICKER_BOUNDARY_RE.finditer(text.upper()):
        ticker = match.group(1)
        if ticker not in seen:
            seen.append(ticker)
    return seen


def _heuristic_intent(question: str) -> Intent | None:
    """Cheap keyword classifier.

    Returns the intent when the heuristic is confident, or None to defer to
    the LLM. The LLM is the fallback, not the primary — most questions in
    the chat are short and word-spotty enough for this layer to handle.
    """
    text = question.strip().lower()
    if not text:
        # No question = no override; the existing graph already defaults the
        # thesis path for empty messages, and the LLM call would be wasted.
        return "thesis"

    # Conversational greetings / capability asks are unambiguous — short
    # greetings ("hi", "hey") are usually the WHOLE message, so a strict
    # equality check or a small-leading-tokens check catches them without
    # false-firing on a longer question that happens to start with "hi".
    stripped = text.rstrip("?!.,")
    if stripped in {"hi", "hello", "hey", "yo", "help"}:
        return "conversational"
    conv_token = _matches_any(text, _CONVERSATIONAL_TOKENS)
    if conv_token is not None and conv_token not in {"hi", "hello", "hey", "yo", "help"}:
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
    if _matches_any(text, _NEWS_SENTIMENT_TOKENS) is not None:
        focused_hits.append("news_sentiment")
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


_CLASSIFY_PROMPT = """You classify a user's question to pick an answer shape.

Respond with the structured field 'intent' set to one of:

* "quick_fact" — the user is asking for a single value or a yes/no read on \
one metric (e.g. "What's the RSI?", "Is AAPL overbought?", "What's MSFT's \
P/E?"). The answer is one or two sentences plus one cited number.
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
"how is MSFT valued?"). The answer is a focused multi-sentence read on \
valuation, earnings, and margins for that ticker — narrower than a full \
thesis, deeper than a single number.
* "technical" — the user asked for a technical analysis on one ticker \
(e.g. "give me the technical analysis of NVDA", "how do the technicals \
look on AAPL?", "chart setup for TSLA"). The answer is a focused read on \
price action, indicators, and trend.
* "news_sentiment" — the user asked for a news / headline / sentiment \
read on one ticker (e.g. "what's the news sentiment on AAPL?", "headlines \
on META", "any concerning news for UNH?"). The answer is a focused read \
on recent headlines and their sentiment.

If you are uncertain between "thesis" and "quick_fact", default to "thesis" \
— that path is the existing safe shape. Pick "comparison" only when there \
is an explicit multi-ticker contrast in the question. Pick "conversational" \
only for greetings, capability asks, or clearly off-domain inputs — \
ambiguous equity questions should NOT route here. Pick "fundamental", \
"technical", or "news_sentiment" ONLY when the user explicitly named that \
domain; an open-ended "should I buy NVDA?" still routes to "thesis" even \
though the answer happens to lean on fundamentals.

Question: {question}
"""


def classify_intent(
    question: str,
    *,
    config: RunnableConfig | None = None,
) -> Intent:
    """Return the response shape to use for ``question``.

    Pure dispatcher: heuristic first, LLM fallback. Both branches default
    to ``thesis`` on any failure or ambiguity so callers never see an
    invalid intent.

    QNT-181: ``config`` carries the LangGraph CallbackHandler from the
    graph entry point so the LLM-fallback path's generation observation
    nests under the parent ``agent-chat`` trace. ``None`` (the default,
    used by tests + the heuristic-only direct callers) skips tracing.
    """
    heuristic = _heuristic_intent(question)
    if heuristic is not None:
        logger.info("classify intent=%s via=heuristic question=%r", heuristic, question[:80])
        return heuristic

    structured_llm = get_llm(temperature=0.0).with_structured_output(IntentDecision)
    try:
        response = structured_llm.invoke(
            _CLASSIFY_PROMPT.format(question=question.strip()),
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 — bias to thesis on any failure
        logger.warning("classify llm failed, defaulting to thesis: %s", exc)
        return "thesis"

    if isinstance(response, IntentDecision):
        logger.info("classify intent=%s via=llm question=%r", response.intent, question[:80])
        return response.intent
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, IntentDecision):
            return parsed.intent
    logger.warning("classify llm returned unexpected shape, defaulting to thesis")
    return "thesis"


__all__ = ["Intent", "IntentDecision", "classify_intent", "extract_tickers"]
