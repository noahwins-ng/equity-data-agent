"""Structured thesis output: Setup / Bull Case / Bear Case / Verdict (QNT-133).

The synthesize node forces the LLM through this schema with
``with_structured_output`` so the API can stream the four sections to the
frontend as JSON without having to re-parse prose. The CLI and the eval
harness still want a flat string, so :meth:`Thesis.to_markdown` re-renders
the structured form into the markdown shape the QNT-67 hallucination check
already understands.

Field shapes are deliberately permissive:

* ``bull_case`` / ``bear_case`` are ``list[str]`` of supporting points rather
  than nested ``{title, body}`` records — the design v2 mock is the example,
  not the contract, and a flatter shape lets the model produce 1-N points
  without padding the structure.
* Asymmetry is allowed: an empty ``bull_case`` or ``bear_case`` is a valid
  output for a one-sided name. The system prompt explicitly tells the model
  not to invent the missing side.
* ``verdict_stance`` is a closed set so the frontend can colour-code it
  without string-matching.

Field descriptions are picked up by ``with_structured_output`` and become
part of the JSON schema the LLM sees, so they double as inline prompting.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

VerdictStance = Literal["constructive", "cautious", "negative", "mixed"]


class Thesis(BaseModel):
    """Structured investment thesis with four sections.

    Returned by the synthesize node when ``with_structured_output(Thesis)``
    succeeds. Consumers that want a flat string (CLI ``--output``, the
    QNT-67 evals) call :meth:`to_markdown`; consumers that want JSON (the
    forthcoming SSE endpoint, frontend) call :meth:`model_dump`.
    """

    setup: str = Field(
        description=(
            "One-paragraph framing of the central question for this ticker. "
            "Name what is at stake — the tension that makes this a decision, "
            "not just 'here is NVDA'. Cite the supplied reports for any "
            "numeric or factual claim using (source: company|technical|fundamental|news)."
        ),
    )
    bull_case: list[str] = Field(
        default_factory=list,
        description=(
            "Supporting points for the bull thesis. Each entry is one bullet "
            "with an inline citation (source: company|technical|fundamental|news). "
            "Number of points must reflect the actual evidence in the supplied "
            "reports — do not pad. Leave EMPTY if the reports contain no real "
            "bull case rather than inventing one."
        ),
    )
    bear_case: list[str] = Field(
        default_factory=list,
        description=(
            "Supporting points for the bear thesis. Mirror of bull_case: one "
            "bullet per real concern, inline citations, EMPTY when the reports "
            "do not support a bear case."
        ),
    )
    verdict_stance: VerdictStance = Field(
        description=(
            "Overall stance. Use 'constructive' when bull dominates, "
            "'negative' when bear dominates, 'cautious' when bear edges bull, "
            "'mixed' when both sides have weight."
        ),
    )
    verdict_action: str = Field(
        description=(
            "Concrete actionable guidance for an investor. Action levels MUST "
            "reference values that appear verbatim in the supplied reports — "
            "for example, the moving-average level the technical report "
            "prints, or the overbought RSI threshold it cites. Every digit "
            "in this field must be a re-quote from the report bodies. Do "
            "not write any literal number that is not already in the "
            "reports, and do not echo numbers from the schema description "
            "itself."
        ),
    )

    def to_markdown(self) -> str:
        """Re-render the structured thesis as markdown.

        Used by the CLI (``--output thesis.md``) and the QNT-67 eval harness
        (which feeds the flat string to the hallucination / cosine / judge
        scorers). The format mirrors the section headings the system prompt
        names, so the eval text matches the prompt's contract one-to-one.
        """
        parts: list[str] = []
        parts.append("## Setup")
        parts.append(self.setup.strip() or "_(no framing supplied)_")

        parts.append("\n## Bull Case")
        if self.bull_case:
            parts.extend(f"- {point.strip()}" for point in self.bull_case)
        else:
            parts.append("_(no bull case supported by the reports)_")

        parts.append("\n## Bear Case")
        if self.bear_case:
            parts.extend(f"- {point.strip()}" for point in self.bear_case)
        else:
            parts.append("_(no bear case supported by the reports)_")

        parts.append("\n## Verdict")
        parts.append(f"**Stance:** {self.verdict_stance}")
        parts.append(self.verdict_action.strip() or "_(no action guidance supplied)_")

        return "\n".join(parts)


__all__ = ["Thesis", "VerdictStance"]
