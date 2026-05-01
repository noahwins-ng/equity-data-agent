"""Comparison response shape (QNT-156).

Triggered when the user asks a multi-ticker question ("Compare NVDA vs AAPL",
"How does META stack up against GOOGL?"). The synthesis combines per-ticker
sections drawn from each ticker's pre-computed reports plus a short
differences paragraph that narrates the contrast in plain English.

Constraints carried over from QNT-67 / ADR-003:

* Every numeric value in a section is copied VERBATIM from the corresponding
  ticker's reports — no cross-ticker arithmetic, no synthetic deltas, no
  computed ratios. The hallucination scorer treats any number that does not
  appear in any of the supplied reports as a regression.
* The differences paragraph is qualitative ("higher P/E", "stronger margin
  trend"). It must NOT introduce numbers that are not already present in
  the per-ticker sections.

Two-ticker cap. The graph parses tickers from the question and clips the
list at 2; if the user named 3+ we fall back to a conversational redirect
that asks them to compare two at a time. 3-way (and N-way) comparison UX is
explicitly out of scope until the 2-ticker shape ships and we see demand.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ComparisonSource = Literal["technical", "fundamental", "news"]


class ComparisonValue(BaseModel):
    """One verbatim cited value attached to a ticker section.

    Same contract as ``QuickFactAnswer``: ``value`` is copied byte-for-byte
    from the corresponding ticker's report, ``source`` names which report
    the value came from, ``label`` is the human-readable metric name.
    """

    label: str = Field(
        description=(
            "Short metric label as the user would read it — 'P/E', "
            "'RSI', 'EPS', 'Net margin'. Used as the row header in the "
            "comparison card; keep under ~20 chars."
        ),
    )
    value: str = Field(
        description=(
            "The single value the comparison cites, copied VERBATIM from "
            "the ticker's reports — '32.4', '$1,234.56', 'overbought'. "
            "Do not reformat: if the report wrote '12.3', this field is "
            "'12.3', not '12.30'."
        ),
    )
    source: ComparisonSource = Field(
        description=("Which report the cited value came from — technical, fundamental, or news."),
    )


class ComparisonSection(BaseModel):
    """Per-ticker section inside a ComparisonAnswer."""

    ticker: str = Field(
        description=(
            "Ticker symbol this section describes (e.g. 'NVDA'). Must be "
            "one of the tickers the agent was asked to compare."
        ),
    )
    summary: str = Field(
        description=(
            "One- or two-sentence prose summary of this ticker's situation "
            "drawn strictly from its supplied reports. Cite the source "
            "inline using (source: technical|fundamental|news). No "
            "numbers that do not appear in the reports."
        ),
    )
    key_values: list[ComparisonValue] = Field(
        default_factory=list,
        description=(
            "1-4 cited values that anchor the comparison for this "
            "ticker. Pick metrics that the user's question implies — if "
            "they asked about valuation, surface P/E; if about momentum, "
            "RSI. Keep the list short — this is a card, not a table."
        ),
    )


class ComparisonAnswer(BaseModel):
    """Structured side-by-side comparison for two tickers.

    Returned by the synthesize node when the classifier picks the
    ``comparison`` intent. The CLI / evals call :meth:`to_markdown` for a
    flat string; the SSE endpoint dumps the model directly.
    """

    sections: list[ComparisonSection] = Field(
        description=(
            "One section per ticker, in the same order the user named "
            "them. Exactly 2 entries — the parser caps at 2 upstream."
        ),
    )
    differences: str = Field(
        description=(
            "Short qualitative paragraph contrasting the two sections — "
            "where they agree, where they diverge. Use only language and "
            "numbers that already appear in the per-ticker sections. Do "
            "NOT compute new ratios, deltas, or synthetic comparisons "
            "(e.g. 'NVDA's P/E is 2x AAPL's'). Phrase contrasts in "
            "qualitative terms ('NVDA trades at a richer multiple', "
            "'AAPL shows weaker momentum')."
        ),
    )

    def to_markdown(self) -> str:
        """Re-render the structured comparison as markdown.

        Mirrors the section layout the chat panel shows so the QNT-67
        hallucination eval reads the same string the user does.
        """
        parts: list[str] = []
        for section in self.sections:
            parts.append(f"## {section.ticker}")
            parts.append(section.summary.strip() or "_(no summary supplied)_")
            if section.key_values:
                for kv in section.key_values:
                    parts.append(f"- **{kv.label}:** {kv.value} (source: {kv.source})")
            parts.append("")

        parts.append("## Differences")
        parts.append(self.differences.strip() or "_(no differences supplied)_")
        return "\n".join(parts).strip()


__all__ = ["ComparisonAnswer", "ComparisonSection", "ComparisonSource", "ComparisonValue"]
