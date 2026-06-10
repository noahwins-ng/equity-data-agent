"""Comparison response shape (QNT-156, reshaped in QNT-208).

Triggered when the user asks a multi-ticker question ("Compare NVDA vs AAPL",
"How does META stack up against GOOGL?"). The synthesis combines per-ticker
sections drawn from each ticker's pre-computed reports plus a short
differences paragraph that narrates the contrast in plain English.

QNT-208 replaces the v1 ``key_values`` list with four ``AspectView`` blocks
per section (company / fundamental / technical / news) mirroring the new
Thesis shape. The differences paragraph stays qualitative (words, not
numbers) and the no-cross-ticker-arithmetic rule from ADR-003 still holds.

Constraints carried over from QNT-67 / ADR-003:

* Every numeric value in an aspect block is copied VERBATIM from the
  corresponding ticker's reports — no cross-ticker arithmetic, no
  synthetic deltas, no computed ratios. The hallucination scorer treats
  any number that does not appear in any of the supplied reports as a
  regression.
* The differences paragraph is qualitative ("higher P/E", "stronger
  margin trend"). It must NOT introduce numbers that are not already
  present in the per-ticker aspect blocks.

Two-ticker cap. The graph parses tickers from the question and clips the
list at 2; if the user named 3+ we fall back to a conversational redirect
that asks them to compare two at a time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent.disclaimer import DISCLAIMER
from agent.thesis import AspectView

# Same source enum as quick_fact.py / focused.py — Pydantic validates the
# Literal at structured-output parse time, and the LLM is told to cite
# (source: company) for qualitative business-context claims.
ComparisonSource = Literal["company", "technical", "fundamental", "news"]


class ComparisonSection(BaseModel):
    """Per-ticker section inside a ComparisonAnswer.

    Mirrors the four-aspect shape of the QNT-208 Thesis so the comparison
    card surfaces the same vocabulary as the single-ticker thesis card.
    Each aspect carries a summary + supports + challenges + optional
    aspect label (Premium/Inline/Discounted for fundamental,
    Uptrend/Sideways/Downtrend for technical, null for company/news).
    """

    ticker: str = Field(
        description=(
            "Ticker symbol this section describes (e.g. 'NVDA'). Must be "
            "one of the tickers the agent was asked to compare."
        ),
    )
    company: AspectView = Field(
        description="Business context aspect drawn from this ticker's company report.",
    )
    fundamental: AspectView = Field(
        description=(
            "Valuation / earnings aspect drawn from this ticker's "
            "fundamental report. Label is Premium / Inline / Discounted."
        ),
    )
    technical: AspectView = Field(
        description=(
            "Price-action / indicator aspect drawn from this ticker's "
            "technical report. Label is Uptrend / Sideways / Downtrend."
        ),
    )
    news: AspectView = Field(
        description="Headline-flow aspect drawn from this ticker's news report. Narrative only.",
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
            "numbers that already appear in the per-ticker aspect blocks. "
            "Do NOT compute new ratios, deltas, or synthetic comparisons "
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
            for heading, aspect in (
                ("Company", section.company),
                ("Fundamental", section.fundamental),
                ("Technical", section.technical),
                ("News", section.news),
            ):
                parts.append(f"### {heading}")
                if aspect.label:
                    parts.append(f"**Label:** {aspect.label}")
                parts.append(aspect.summary.strip() or "_(no summary supplied)_")
                if aspect.supports:
                    for point in aspect.supports:
                        parts.append(f"+ {point.strip()}")
                if aspect.challenges:
                    for point in aspect.challenges:
                        parts.append(f"- {point.strip()}")
                parts.append("")

        parts.append("## Differences")
        parts.append(self.differences.strip() or "_(no differences supplied)_")
        parts.append(f"\n{DISCLAIMER}")
        return "\n".join(parts).strip()


# ─── QNT-224: lean N-way comparison shape (3-4 tickers) ───────────────────
#
# Distinct from the rich two-ticker ComparisonAnswer above: a 3-4 way compare
# is about a few specific dimensions across names, not a full four-aspect
# thesis per name (that is a token explosion past two tickers). The lean shape
# is a compact metrics row per ticker, computed in SQL and formatted in the
# API (api.comparison_metrics) — the agent renders it verbatim and the narrate
# node speaks the qualitative contrast. No LLM synthesis call, no arithmetic
# here: every cell is a pre-formatted string copied straight from the API.


class LeanComparisonRow(BaseModel):
    """One ticker's compact metrics row in a lean N-way comparison.

    Field set mirrors api.comparison_metrics.ComparisonMetricRow. Values are
    display-ready strings (e.g. ``'28.4'``, ``'$182.50'``, ``'N/M (...)'``) —
    the agent never re-derives or re-formats them.
    """

    ticker: str = Field(description="Ticker symbol this row describes.")
    pe: str = Field(description="Latest quarterly P/E, pre-formatted.")
    rsi: str = Field(description="Latest daily RSI-14, pre-formatted.")
    net_margin: str = Field(description="Latest quarterly net margin, pre-formatted.")
    price: str = Field(description="Latest daily close, pre-formatted.")
    # QNT-224 follow-up: interpretive verdicts copied verbatim from the
    # fundamental + technical reports (Premium/Inline/Discounted,
    # Uptrend/Sideways/Downtrend). None when the report suppressed the label.
    valuation_label: str | None = Field(default=None, description="Premium / Inline / Discounted.")
    trend_daily: str | None = Field(
        default=None, description="Daily Uptrend / Sideways / Downtrend."
    )
    trend_weekly: str | None = Field(
        default=None, description="Weekly Uptrend / Sideways / Downtrend."
    )


class LeanComparisonAnswer(BaseModel):
    """Lean 3-4 way comparison: a metrics table, one row per ticker.

    Built deterministically from the API metrics payload (no LLM call). The
    narrate node reads :meth:`to_markdown` to speak the qualitative contrast,
    and the chat panel renders the rows as a compact metrics table.
    """

    rows: list[LeanComparisonRow] = Field(
        description="One metrics row per ticker, in the order the user named them.",
    )

    def to_markdown(self) -> str:
        """Render the metrics rows as a markdown table.

        narrate consumes this as its substrate, so every number the spoken
        paragraph might quote is present here (and, via the gather stash, in
        the runtime grounding report set).
        """
        header = (
            "| Ticker | P/E | RSI | Net margin | Price | Valuation | "
            "Trend (daily) | Trend (weekly) |"
        )
        sep = "| --- | --- | --- | --- | --- | --- | --- | --- |"
        body = [
            f"| {r.ticker} | {r.pe} | {r.rsi} | {r.net_margin} | {r.price} | "
            f"{r.valuation_label or 'N/M'} | {r.trend_daily or 'N/M'} | {r.trend_weekly or 'N/M'} |"
            for r in self.rows
        ]
        return "\n".join(["# COMPARISON METRICS", "", header, sep, *body, "", DISCLAIMER]).strip()


__all__ = [
    "ComparisonAnswer",
    "ComparisonSection",
    "ComparisonSource",
    "LeanComparisonAnswer",
    "LeanComparisonRow",
]
