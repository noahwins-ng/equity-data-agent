"""Quick-fact response shape (QNT-149).

When the user asks a single-metric question ("What's NVDA's RSI?", "What's
TSLA's P/E?") the heavy four-section thesis is overkill — it pads, it cites
reports the question doesn't need, and the panel renders an answer that
ignores the question. The quick-fact shape is the alternative: a short prose
answer plus exactly one cited value pulled verbatim from a single report.

Constraints carried over from QNT-67 / QNT-133:

* ``cited_value`` MUST be a substring of the report it claims to cite —
  the hallucination scorer treats it like any other numeric claim.
* ``source`` is one of the canonical report names (``technical`` |
  ``fundamental`` | ``news``) so the chat panel can render the same
  ``(source: …)`` chip vocabulary it already understands.
* ``answer`` is short prose (one or two sentences), not bullets — the
  chat panel renders it inline rather than as a card section.

The schema is deliberately minimal. Adding a list of supporting bullets or
a stance enum would re-create the thesis shape under a new name; the whole
point of this path is to NOT do that.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# QNT-175: kept aligned with ``REPORT_TOOLS`` (which now includes ``company``)
# so the Pydantic boundary is the superset, not a subset of the runtime tool
# registry. The plan layer strips ``company`` for quick-fact intent, so the
# LLM should never see a company report here in practice — but the schema
# matching the tool registry protects against future plumbing changes that
# might pass company through, and against any structured-output retry that
# routes a thesis-shaped citation into a quick-fact field.
QuickFactSource = Literal["company", "technical", "fundamental", "news"]


class QuickFactAnswer(BaseModel):
    """Structured short-form answer for single-metric questions.

    Returned by the synthesize node when the classifier picks the
    ``quick_fact`` intent. Consumers that want a flat string (CLI, evals)
    call :meth:`to_markdown`; the SSE endpoint dumps the model directly.
    """

    answer: str = Field(
        description=(
            "One- or two-sentence prose answer to the user's question. "
            "Cite the source inline using (source: technical|fundamental|news). "
            "If the relevant value is not in the supplied reports, write "
            "'<metric> not available in the supplied reports' and leave "
            "cited_value empty. Do not invent numbers, do not extrapolate, "
            "do not add a thesis."
        ),
    )
    cited_value: str = Field(
        default="",
        description=(
            "The single value the answer cites, copied VERBATIM from the "
            "supplied reports — e.g. '62.4' or '$1,234.56' or 'overbought'. "
            "Empty string when no value is available. Do not reformat: "
            "if the report wrote '12.3', this field is '12.3', not '12.30'."
        ),
    )
    source: QuickFactSource | None = Field(
        default=None,
        description=(
            "Which report the cited value came from. Required when "
            "cited_value is non-empty; null when the answer is a "
            "'not available' apology."
        ),
    )

    def to_markdown(self) -> str:
        """Re-render the structured quick-fact as markdown.

        The CLI and the QNT-67 hallucination eval both want a flat string;
        this output mirrors what the chat panel shows so the eval text is
        the same shape the user actually sees.
        """
        body = self.answer.strip() or "_(no answer supplied)_"
        if self.cited_value and self.source:
            body = f"{body}\n\n**Value:** {self.cited_value} (source: {self.source})"
        return body


__all__ = ["QuickFactAnswer", "QuickFactSource"]
