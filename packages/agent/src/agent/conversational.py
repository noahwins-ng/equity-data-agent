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
    ("news", "Are there any concerning news items on UNH?"),
    ("thesis", "Should I be cautious about META based on the latest data?"),
    ("thesis", "Give me a balanced thesis on V right now."),
    ("comparison", "Compare NVDA vs AAPL on valuation."),
    ("comparison", "How does META stack up against GOOGL?"),
)


def _pick_suggestions(hint: str | None, tickers: Iterable[str]) -> list[str]:
    """Pick 3 example questions, biased toward ``hint`` if supplied.

    ``hint`` is one of the labels in ``_SUGGESTION_BANK`` (e.g. ``"technical"``)
    or None for a balanced mix. ``tickers`` is the full ticker registry —
    accepted for forward-compat; the bank questions already reference real
    tickers, so the param is currently informational only.
    """
    del tickers  # reserved for future per-question ticker substitution
    if hint:
        primary = [q for label, q in _SUGGESTION_BANK if label == hint]
        if primary:
            # Pad with two from other categories so the user sees variety.
            others = [q for label, q in _SUGGESTION_BANK if label != hint]
            return [primary[0], *others[:2]]
    # No hint — pick the first three labelled categories for breadth.
    return [_SUGGESTION_BANK[0][1], _SUGGESTION_BANK[2][1], _SUGGESTION_BANK[6][1]]


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


__all__ = ["ConversationalAnswer", "domain_redirect"]
