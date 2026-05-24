"""Focused-analysis response shape (QNT-176, reshaped in QNT-208).

Triggered when the classifier picks one of the focused-analysis intents:
``fundamental``, ``technical``, or ``news``. The user asked for a deeper
read on a single domain ("walk me through META's fundamentals", "give me
a technical analysis of NVDA", "what's the news on AAPL?") -- shorter
than a full thesis, longer than a quick fact, and narrowed to the matching
report family.

QNT-208 reshapes the verdict + news fields:

* ``focus="fundamental"`` carries ``verdict in {Premium, Inline, Discounted}``
  quoted verbatim from the fundamental report's own label.
* ``focus="technical"`` carries ``verdict in {Uptrend, Sideways, Downtrend}``.
  When daily / weekly / monthly diverge the summary names each and the
  verdict reflects the majority rule (>=2 timeframes agree wins;
  otherwise Sideways).
* ``focus="news"`` (renamed from ``news_sentiment`` per the QNT-207 drop
  of sentiment surfacing) carries no verdict. Instead it populates
  ``existing_development`` (the running story), ``positive_catalysts``
  (cited headlines), and ``negative_catalysts`` (cited headlines).

Constraints carried over from QNT-67 / ADR-003:

* Every numeric value in ``summary``, ``key_points``, ``cited_values``,
  or the news catalyst lists is copied VERBATIM from the supplied reports.
* The matching report family is always in the supplied set; the
  ``company`` report is included for qualitative grounding.
* Inline ``(source: company|technical|fundamental|news)`` cites are
  required on any sentence that makes a numeric or factual claim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent.disclaimer import DISCLAIMER

# Same source enum as comparison.py / quick_fact.py — Pydantic validates the
# Literal at structured-output parse time, and the LLM is told to cite
# (source: company) for qualitative business context.
FocusedSource = Literal["company", "technical", "fundamental", "news"]

# QNT-208: ``news_sentiment`` renamed to ``news`` so v2 vocabulary carries
# no "sentiment" language anywhere.
FocusKind = Literal["fundamental", "technical", "news"]

# Per-focus verdict literals. Merged into one Literal so the JSON Schema
# emits a single {"type": "string", "enum": [...]} — Groq rejects anyOf
# with two string branches as "duplicate primitive types".
# Fundamental: Premium | Inline | Discounted
# Technical:   Uptrend | Sideways | Downtrend
FocusedVerdict = Literal["Premium", "Inline", "Discounted", "Uptrend", "Sideways", "Downtrend"]


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
    ``fundamental``, ``technical``, or ``news`` intent. The CLI / evals
    call :meth:`to_markdown`; the SSE endpoint dumps the model directly.
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
            "acceptable when no quantitative value is available."
        ),
    )
    verdict: FocusedVerdict | None = Field(
        default=None,
        description=(
            "Per-focus verdict label. For focus=fundamental: one of "
            "Premium / Inline / Discounted, quoted verbatim from the "
            "fundamental report. For focus=technical: one of Uptrend / "
            "Sideways / Downtrend, quoted verbatim from the technical "
            "report's per-timeframe TREND labels (majority rule across "
            "daily/weekly/monthly). For focus=news: null -- news uses the "
            "catalyst fields below instead."
        ),
    )
    existing_development: str | None = Field(
        default=None,
        description=(
            "For focus=news only: the running story in 1-2 sentences "
            "drawn from the news report. Null for other focuses."
        ),
    )
    positive_catalysts: list[str] = Field(
        default_factory=list,
        description=(
            "For focus=news only: cited headlines that argue constructive. "
            "Each entry is one bullet with (source: news). Empty for other "
            "focuses."
        ),
    )
    negative_catalysts: list[str] = Field(
        default_factory=list,
        description=(
            "For focus=news only: cited headlines that argue cautious. "
            "Each entry is one bullet with (source: news). Empty for other "
            "focuses."
        ),
    )

    def to_markdown(self) -> str:
        """Re-render the structured analysis as markdown."""
        parts: list[str] = []
        if self.verdict:
            parts.append(f"**Verdict:** {self.verdict}")
            parts.append("")
        parts.append(self.summary.strip() or "_(no summary supplied)_")
        if self.key_points:
            parts.append("")
            parts.extend(f"- {p.strip()}" for p in self.key_points if p.strip())
        if self.cited_values:
            parts.append("")
            for kv in self.cited_values:
                parts.append(f"- **{kv.label}:** {kv.value} (source: {kv.source})")
        if self.focus == "news":
            if self.existing_development:
                parts.append("")
                parts.append("**Existing development:**")
                parts.append(self.existing_development.strip())
            if self.positive_catalysts:
                parts.append("")
                parts.append("**Positive catalysts:**")
                parts.extend(f"+ {c.strip()}" for c in self.positive_catalysts if c.strip())
            if self.negative_catalysts:
                parts.append("")
                parts.append("**Negative catalysts:**")
                parts.extend(f"- {c.strip()}" for c in self.negative_catalysts if c.strip())
        parts.append(f"\n{DISCLAIMER}")
        return "\n".join(parts).strip()


__all__ = [
    "FocusedAnalysis",
    "FocusedSource",
    "FocusedValue",
    "FocusedVerdict",
    "FocusKind",
]
