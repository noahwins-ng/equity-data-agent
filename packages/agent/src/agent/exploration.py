"""Exploration-scan response shape (QNT-220 follow-up).

Triggered when the classifier routes a broad, anchored exploratory ask
("what's interesting about NVDA?", "what stands out on AAPL?", "what should
I watch next week?") to ``explore_supervisor`` — the deterministic,
zero-LLM-call scan node. That node gathers two complementary lenses
(news-first when the ask is timely, otherwise company + news) and hands the
reports to this shape.

Exploration is deliberately NOT a thesis and NOT a single-domain focused
read:

* It carries no verdict — a scan surfaces what is notable across lenses, it
  does not take a buy/sell stance.
* It spans the gathered lenses rather than staying inside one domain, so the
  observations cite a mix of (source: news) / (source: technical) / etc.
* It carries no forward "watch next" calendar: no report exposes dated
  catalysts, so under ADR-003 there is nothing to copy verbatim and the card
  must not invent one.

Constraints carried over from QNT-67 / ADR-003:

* Every numeric value in ``headline``, ``observations``, or ``cited_values``
  is copied VERBATIM from the supplied reports.
* Inline ``(source: company|technical|fundamental|news)`` cites are required
  on any sentence that makes a numeric or factual claim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent.disclaimer import DISCLAIMER

# Same source enum the other card modules each define locally (comparison.py /
# focused.py) — Pydantic validates the Literal at structured-output parse time.
ExplorationSource = Literal["company", "technical", "fundamental", "news"]


class ExplorationValue(BaseModel):
    """One verbatim cited value attached to an exploration scan.

    Mirrors ``FocusedValue``: ``value`` is copied byte-for-byte from the
    report, ``source`` names which report it came from, ``label`` is the
    human-readable metric name.
    """

    label: str = Field(
        description=(
            "Short metric label — 'RSI', 'P/E', 'Daily trend'. "
            "Used as the chip header in the exploration card."
        ),
    )
    value: str = Field(
        description=(
            "The single value the scan cites, copied VERBATIM from the "
            "supplied reports. Do not reformat: if the report wrote '71', "
            "this field is '71', not '71.0'."
        ),
    )
    source: ExplorationSource = Field(
        description=(
            "Which report the cited value came from — company, technical, fundamental, or news."
        ),
    )


class ExplorationAnswer(BaseModel):
    """Structured exploration-scan answer for one ticker.

    Returned by the synthesize node when ``explore_supervisor`` routed a
    broad anchored exploratory ask here. The CLI / evals call
    :meth:`to_markdown`; the SSE endpoint dumps the model directly.
    """

    headline: str = Field(
        description=(
            "One to two sentences naming what stands out across the scanned "
            "lenses. Cite sources inline using "
            "(source: company|technical|fundamental|news). No numbers that "
            "do not appear in the supplied reports."
        ),
    )
    observations: list[str] = Field(
        default_factory=list,
        description=(
            "Two to five bullet points, each one sentence with an inline "
            "citation, spanning the gathered lenses (a news observation AND "
            "a technical observation, not three of one kind). Leave shorter "
            "if the supplied reports do not support more."
        ),
    )
    cited_values: list[ExplorationValue] = Field(
        default_factory=list,
        description=(
            "Zero to four verbatim cited values that anchor the scan — e.g. "
            "the RSI reading, the daily trend label, a headline count. Empty "
            "list is acceptable when no quantitative value is available."
        ),
    )

    def to_markdown(self) -> str:
        """Re-render the structured scan as markdown."""
        parts: list[str] = [self.headline.strip() or "_(no headline supplied)_"]
        if self.observations:
            parts.append("")
            parts.extend(f"- {o.strip()}" for o in self.observations if o.strip())
        if self.cited_values:
            parts.append("")
            for kv in self.cited_values:
                parts.append(f"- **{kv.label}:** {kv.value} (source: {kv.source})")
        parts.append(f"\n{DISCLAIMER}")
        return "\n".join(parts).strip()


__all__ = [
    "ExplorationAnswer",
    "ExplorationSource",
    "ExplorationValue",
]
