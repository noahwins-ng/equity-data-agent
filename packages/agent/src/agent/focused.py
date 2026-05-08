"""Focused-analysis response shape (QNT-176).

Triggered when the classifier picks one of the three focused-analysis
intents: ``fundamental``, ``technical``, or ``news_sentiment``. The user
asked for a deeper read on a single domain ("walk me through META's
fundamentals", "give me a technical analysis of NVDA", "what's the news
sentiment on AAPL?") -- shorter than a full thesis, longer than a quick
fact, and narrowed to the matching report family.

One Pydantic class covers all three shapes. The ``focus`` discriminator
field tells the UI which header / accent to render; the body fields
(``summary``, ``key_points``, ``cited_values``) are identical across all
three. Rolling three near-duplicate models would have meant three identical
SSE event types, three render branches, and three sets of tests -- when
the only real difference is the prompt that produced the answer.

Constraints carried over from QNT-67 / ADR-003:

* Every numeric value in ``summary``, ``key_points``, or
  ``cited_values`` is copied VERBATIM from the supplied reports.
* The matching report family is always in the supplied set; the
  ``company`` report is included for qualitative grounding.
* Inline ``(source: company|technical|fundamental|news)`` cites are
  required on any sentence that makes a numeric or factual claim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Same source enum as comparison.py / quick_fact.py — Pydantic validates the
# Literal at structured-output parse time, and the LLM is told to cite
# (source: company) for qualitative business context.
FocusedSource = Literal["company", "technical", "fundamental", "news"]

FocusKind = Literal["fundamental", "technical", "news_sentiment"]


class FocusedValue(BaseModel):
    """One verbatim cited value attached to a focused analysis.

    Same contract as ``ComparisonValue``: ``value`` is copied byte-for-byte
    from the report, ``source`` names which report it came from, ``label``
    is the human-readable metric name.
    """

    label: str = Field(
        description=(
            "Short metric label — 'P/E', 'RSI', 'Headline sentiment'. "
            "Used as the row header in the focused-analysis card."
        ),
    )
    value: str = Field(
        description=(
            "The single value the analysis cites, copied VERBATIM from the "
            "supplied reports. Do not reformat: if the report wrote '12.3', "
            "this field is '12.3', not '12.30'."
        ),
    )
    source: FocusedSource = Field(
        description=(
            "Which report the cited value came from — company, technical, fundamental, or news."
        ),
    )


class FocusedAnalysis(BaseModel):
    """Structured focused-analysis answer for one ticker.

    Returned by the synthesize node when the classifier picks the
    ``fundamental``, ``technical``, or ``news_sentiment`` intent. The
    CLI / evals call :meth:`to_markdown`; the SSE endpoint dumps the
    model directly.
    """

    focus: FocusKind = Field(
        description=(
            "Which domain this analysis covers. Routed by the agent's "
            "classifier and matched against the supplied report family — "
            "the LLM does not pick this; it is set by the synthesize node "
            "from ``state['intent']`` so the schema stays self-describing."
        ),
    )
    summary: str = Field(
        description=(
            "Two- to four-sentence prose summary of the focused read. "
            "Cite sources inline using "
            "(source: company|technical|fundamental|news). No numbers that "
            "do not appear in the supplied reports."
        ),
    )
    key_points: list[str] = Field(
        default_factory=list,
        description=(
            "Two to five bullet points expanding the summary. Each bullet "
            "is one sentence with an inline citation. Leave the list empty "
            "if the supplied reports do not support more than the summary."
        ),
    )
    cited_values: list[FocusedValue] = Field(
        default_factory=list,
        description=(
            "One to four verbatim cited values that anchor the analysis. "
            "Pick the metrics the user's question implies. Empty list is "
            "acceptable for a news-sentiment read where no quantitative "
            "value is available."
        ),
    )

    def to_markdown(self) -> str:
        """Re-render the structured analysis as markdown."""
        parts: list[str] = []
        parts.append(self.summary.strip() or "_(no summary supplied)_")
        if self.key_points:
            parts.append("")
            parts.extend(f"- {p.strip()}" for p in self.key_points if p.strip())
        if self.cited_values:
            parts.append("")
            for kv in self.cited_values:
                parts.append(f"- **{kv.label}:** {kv.value} (source: {kv.source})")
        return "\n".join(parts).strip()


__all__ = ["FocusKind", "FocusedAnalysis", "FocusedSource", "FocusedValue"]
