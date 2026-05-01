"""Intent classification for the agent (QNT-149).

The agent used to force every input through the same four-section thesis
template. A user asking "what's the RSI right now?" got the same heavy
treatment as a user asking "is this a buy?" — rigid, ignores the question,
every answer looked the same.

This module classifies an inbound question into one of two response shapes:

* ``thesis`` — a balanced, multi-source investment thesis (Setup / Bull /
  Bear / Verdict). Default for open-ended asks ("should I be cautious about
  META?", "give me a balanced thesis on V").
* ``quick_fact`` — a short prose answer plus a single cited value, no
  thesis card. For single-metric lookups ("what's NVDA's P/E?", "is AAPL
  overbought?").

Two-layer design:

1. A keyword heuristic short-circuits the obvious cases (single ``?``
   ending, tokens like 'rsi'/'p/e', length under N words). This keeps the
   classifier free for the common case and degrades gracefully when the
   LLM misbehaves.
2. The LLM picks via ``with_structured_output(IntentDecision)`` on the
   ambiguous middle. Failures bias toward ``thesis`` — the existing path is
   the safe default; the eval golden set (QNT-67, QNT-128) was built
   against it, so a misclassification toward thesis cannot regress those
   contracts.

The classifier is intentionally stateless: it sees only the question
string. Ticker, prior runs, and tools are not inputs — adding them would
push toward "rich classifier with planning bias", which belongs in the
plan node, not here.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from agent.llm import get_llm
from agent.tracing import langfuse

logger = logging.getLogger(__name__)

Intent = Literal["thesis", "quick_fact"]


class IntentDecision(BaseModel):
    """Structured-output schema for the classifier LLM call."""

    intent: Intent = Field(
        description=(
            "The response shape to use. 'thesis' for open-ended investment "
            "questions that warrant a Setup / Bull / Bear / Verdict treatment. "
            "'quick_fact' for single-metric lookups where a short prose answer "
            "plus one cited value is enough."
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

# Short questions are more likely quick-fact. Tuned conservatively: a 12-word
# question can still be open-ended, so this is one signal among several.
_SHORT_QUESTION_WORD_LIMIT = 12


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

If you are uncertain, default to "thesis" — that path is the existing safe \
shape. Pick "quick_fact" only when the question is clearly a single-metric \
lookup.

Question: {question}
"""


def classify_intent(question: str) -> Intent:
    """Return the response shape to use for ``question``.

    Pure dispatcher: heuristic first, LLM fallback. Both branches default
    to ``thesis`` on any failure or ambiguity so callers never see an
    invalid intent. Routed through ``langfuse.traced_invoke`` so a misroute
    is visible in the dashboard.
    """
    heuristic = _heuristic_intent(question)
    if heuristic is not None:
        logger.info("classify intent=%s via=heuristic question=%r", heuristic, question[:80])
        return heuristic

    structured_llm = get_llm(temperature=0.0).with_structured_output(IntentDecision)
    try:
        response = langfuse.traced_invoke(
            structured_llm,
            _CLASSIFY_PROMPT.format(question=question.strip()),
            name="classify",
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


__all__ = ["Intent", "IntentDecision", "classify_intent"]
