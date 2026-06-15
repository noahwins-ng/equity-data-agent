"""Conversational response shape (QNT-156).

Triggered when the classifier picks the ``conversational`` intent — greetings
("hi", "hello"), capability asks ("what can you do?", "how does this work?"),
meta-questions, and clearly off-domain inputs ("what's the weather?", "tell
me a joke"). The agent must NOT pretend to know things outside its domain;
"I don't know that" + a redirect to what the agent CAN do is the canonical
answer.

Two failure surfaces also produce a ``ConversationalAnswer``:

1. The classifier confidently picks ``conversational`` for an off-domain or
   capability ask — the synthesize node generates a friendly redirect via the
   conversational LLM call.
2. ANY synthesize-path failure (thesis empty, quick_fact empty, comparison
   parse failed, no reports gathered) falls back to a deterministic
   :func:`domain_redirect` payload built from the agent's actual capabilities
   (covered tickers, report types, two example questions). This replaces the
   pre-QNT-156 SSE error / blank panel surface so the user always sees an
   in-domain response, never a stack trace.

Hard rule: the conversational answer must NEVER produce numeric claims. The
hallucination scorer treats any digit in :attr:`ConversationalAnswer.answer`
as a regression — see :meth:`ConversationalAnswer.has_numeric_claims`.
Suggestions are free-form because they're presented as canned questions, but
the standard ones we ship don't carry numbers either.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from shared.tickers import TICKERS

from agent.disclaimer import DISCLAIMER

if TYPE_CHECKING:
    from collections.abc import Iterable

# Match any digit run — the conversational rule is "no numeric claims at all",
# so the simplest detector is "any digit". This deliberately catches
# "year 2026" / "10 tickers" too: numbers in conversational text invariably
# look like a fact, and the path is supposed to stay vibes-only.
_DIGIT_RE = re.compile(r"\d")


class ConversationalAnswer(BaseModel):
    """Structured short conversational reply with optional suggestions.

    Returned by the synthesize node when the classifier picks the
    ``conversational`` intent OR when any other path fails and we fall back
    to a deterministic domain redirect. The CLI / evals call
    :meth:`to_markdown` for a flat string; the SSE endpoint dumps the model
    directly.
    """

    answer: str = Field(
        description=(
            "One short paragraph (1-3 sentences) of plain prose. For "
            "greetings: a friendly hello. For capability asks: a one-line "
            "summary of what the agent can do. For off-domain asks: a "
            "polite 'I don't know that' followed by a redirect. NEVER "
            "include numbers, percentages, prices, or dates — the "
            "hallucination scorer flags any digit as a regression."
        ),
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description=(
            "0 or 3 example questions the user could ask instead, drawn "
            "from the agent's actual capabilities (covered tickers, "
            "report types). Empty when the user's ask was a simple "
            "greeting that doesn't need redirection. Each suggestion is a "
            "complete question the agent could answer."
        ),
    )

    def has_numeric_claims(self) -> bool:
        """Return True if the answer contains any digit.

        Used by the QNT-67 hallucination scorer — the conversational path is
        not allowed to produce numeric claims, so even a stray '10 tickers'
        is treated as a regression.
        """
        return bool(_DIGIT_RE.search(self.answer))

    def to_markdown(self) -> str:
        """Re-render the structured answer as markdown.

        Used by the CLI and the QNT-67 eval harness. Suggestions render as a
        bullet list so the eval text matches what the chat panel displays.
        """
        parts: list[str] = [self.answer.strip() or "_(no answer supplied)_"]
        if self.suggestions:
            parts.append("\n**You could ask:**")
            parts.extend(f"- {s.strip()}" for s in self.suggestions if s.strip())
        parts.append(f"\n{DISCLAIMER}")
        return "\n".join(parts)


# ─── Deterministic redirect (synthesize-failure fallback) ──────────────────


# Default suggestion bank — short, in-domain, no digits. Picked at random by
# index, not shuffled, so the hash is stable across renders for screenshot
# tests. The graph picks 3 suggestions matching the user's evident shape (see
# :func:`domain_redirect`).
_SUGGESTION_BANK: tuple[tuple[str, str], ...] = (
    ("technical", "What's NVDA's RSI right now?"),
    ("technical", "How is AAPL trending technically?"),
    ("fundamental", "How is MSFT valued relative to its earnings?"),
    ("fundamental", "What's the fundamental case for AMZN?"),
    ("news", "What's driving META headlines lately?"),
    ("news", "Are there any concerning news items on INTC?"),
    ("thesis", "Should I be cautious about META based on the latest data?"),
    ("thesis", "Give me a balanced thesis on AMD right now."),
    ("comparison", "Compare NVDA vs AAPL on valuation."),
    ("comparison", "How does META stack up against GOOGL?"),
    ("comparison", "Compare AMZN vs MSFT on growth."),
)

# QNT-156/QNT-244: the suggestion card shows either 0 entries (a bare greeting
# that needs no redirect) or exactly this many. A partial list is never shown.
_SUGGESTION_COUNT = 3


def _pick_suggestions(hint: str | None, tickers: Iterable[str]) -> list[str]:
    """Pick 3 example questions, biased toward ``hint`` if supplied.

    ``hint`` is one of the labels in ``_SUGGESTION_BANK`` (e.g. ``"technical"``)
    or None for a balanced mix. ``tickers`` is the full ticker registry —
    accepted for forward-compat; the bank questions already reference real
    tickers, so the param is currently informational only.

    When a hint is given, the picks are drawn from that label first (so a
    ``comparison`` hint yields concrete covered pairs, per QNT-244 AC4), padding
    from other labels only if the hinted label has fewer than three entries.
    """
    del tickers  # reserved for future per-question ticker substitution
    if hint:
        primary = [q for label, q in _SUGGESTION_BANK if label == hint]
        if primary:
            picks = primary[:_SUGGESTION_COUNT]
            if len(picks) < _SUGGESTION_COUNT:
                # Pad with other categories so the user always sees three.
                others = [q for label, q in _SUGGESTION_BANK if label != hint]
                picks = [*picks, *others[: _SUGGESTION_COUNT - len(picks)]]
            return picks
    # No hint — one suggestion each from three distinct shapes for breadth.
    # Filter by label (not hardcoded indices) so reordering or inserting bank
    # entries can't silently change which shapes the balanced mix draws from.
    return [
        next(q for label, q in _SUGGESTION_BANK if label == want)
        for want in ("technical", "fundamental", "thesis")
    ]


# ─── QNT-244: clickable-suggestion guardrail ───────────────────────────────


# A displayed suggestion must be an answerable prompt: it names a covered
# ticker by symbol and avoids out-of-scope placeholders. The LLM-generated
# ``ConversationalAnswer.suggestions`` were previously accepted verbatim, so a
# cold "what can you do?" could ship "trend for a specific stock?" — which then
# routes straight to clarify on click because no ticker was named.
#
# Tickers are matched case-sensitively against the uppercase symbol so a short
# symbol can't false-match a stray lowercase occurrence inside prose.
_COVERED_TICKER_RE = re.compile(r"\b(" + "|".join(TICKERS) + r")\b")

# Placeholder / unsupported-scope phrases that make a suggestion unanswerable.
# Matched case-insensitively. "market" is allowed only as "market cap" (a real
# fundamental metric); every other use ("the market", "broader market") is the
# out-of-scope sense the issue rejects.
_OUT_OF_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bspecific stock\b"),
    re.compile(r"\b(?:a|an|some|any|the)\s+company\b"),
    re.compile(r"\bcompanies\b"),
    re.compile(r"\bpeers?\b"),
    re.compile(r"\bsectors?\b"),
    re.compile(r"\bmarket\b(?!\s+cap)"),
    re.compile(r"\bmacro(?:economic)?\b"),
    re.compile(r"\betfs?\b"),
    re.compile(r"\bbenchmarks?\b"),
    re.compile(r"\b(?:crypto|cryptocurrency|bitcoin|ethereum)\b"),
    re.compile(r"\boptions?\b"),
    re.compile(r"\bportfolio\b"),
    re.compile(r"\ballocat\w*\b"),
    re.compile(r"\bprice targets?\b"),
    re.compile(r"\b(?:financial|investment) advice\b"),
)


def is_answerable_suggestion(text: str) -> bool:
    """Return True if ``text`` is a concrete, in-scope, clickable suggestion.

    Answerable means: names at least one covered ticker by symbol and contains
    no out-of-scope placeholder (``specific stock``, ``a company``, ``peers``,
    ``sector``, ``market``, macro, ETFs, crypto, options, portfolio, price
    targets, advice). Comparison prompts naming two covered tickers pass by the
    same rule. Used to gate LLM-generated suggestions before they reach the
    chat card (QNT-244).
    """
    if not text or not text.strip():
        return False
    lowered = text.lower()
    if any(pattern.search(lowered) for pattern in _OUT_OF_SCOPE_PATTERNS):
        return False
    return bool(_COVERED_TICKER_RE.search(text))


def coerce_suggestions(
    suggestions: Iterable[str],
    *,
    hint: str | None = None,
) -> list[str]:
    """Normalise LLM-proposed suggestions to the displayed contract (QNT-244).

    The card shows either zero suggestions (a bare greeting that needs no
    redirect) or exactly three answerable ones. An empty input stays empty.
    A non-empty input passes through unchanged when it already carries three or
    more answerable prompts (first three kept); otherwise — invalid or
    incomplete — it is replaced wholesale with deterministic in-scope picks from
    the centralized bank, biased by ``hint``.
    """
    items = [s.strip() for s in suggestions if s and s.strip()]
    if not items:
        return []
    valid = [s for s in items if is_answerable_suggestion(s)]
    if len(valid) >= _SUGGESTION_COUNT:
        return valid[:_SUGGESTION_COUNT]
    return _pick_suggestions(hint, TICKERS)


def domain_redirect(
    *,
    reason: str,
    tickers: Iterable[str],
    hint: str | None = None,
) -> ConversationalAnswer:
    """Build the deterministic conversational fallback payload.

    Used when any synthesize path fails — the panel sees a useful in-domain
    reply instead of a stack trace. ``reason`` shapes the apology line;
    ``tickers`` lists the symbols the user can actually ask about; ``hint``
    biases the suggestion picks toward whichever shape the user's evident
    question implied (e.g. ``"comparison"`` when the parser saw multiple
    tickers but couldn't satisfy the request).

    The QNT-156 conversational guardrail rejects any digit in the answer
    body — the hallucination scorer treats one as a regression. Guard
    ``reason`` at the boundary so a future caller cannot accidentally
    embed an HTTP status code, retry count, or year into the redirect
    and ship a payload that immediately fails the eval contract.
    """
    if _DIGIT_RE.search(reason):
        raise ValueError(
            f"domain_redirect reason must not contain digits "
            f"(would fail the hallucination guardrail): {reason!r}"
        )
    ticker_list = ", ".join(sorted(tickers))
    answer = (
        f"{reason} I cover the following US equities: {ticker_list}. "
        "I can pull a balanced thesis, a single-metric quick fact, or "
        "compare two of these names side-by-side. Pick one of the "
        "suggestions below or rephrase your question."
    )
    return ConversationalAnswer(
        answer=answer,
        suggestions=_pick_suggestions(hint, tickers),
    )


__all__ = [
    "ConversationalAnswer",
    "coerce_suggestions",
    "domain_redirect",
    "is_answerable_suggestion",
]
