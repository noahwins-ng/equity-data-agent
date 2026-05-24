"""Per-aspect thesis (QNT-208, supersedes QNT-133).

Reshapes the thesis output to analyst-standard framing: per-aspect blocks
(Company / Fundamental / Technical / News) each with summary + supports +
challenges + an aspect-level label, plus a final Overweight / Neutral /
Underweight verdict with a 2-3 sentence rationale.

The synthesize node forces the LLM through this schema with
``with_structured_output`` so the API can stream the structured payload
to the frontend as JSON without re-parsing prose. The CLI and the eval
harness call :meth:`Thesis.to_markdown` for the flat-string form the
QNT-67 hallucination check already understands.

Field shapes are deliberately permissive:

* Per aspect, ``supports`` / ``challenges`` are ``list[str]`` of bullets.
  Empty lists are valid — asymmetric aspects (all-supports or
  all-challenges) are real analyst reads, not a schema violation.
* ``label`` is ``str | None`` per aspect — Company and News are
  narrative-only and pass ``None``; Fundamental carries one of
  ``Premium`` / ``Inline`` / ``Discounted`` and Technical carries one of
  ``Uptrend`` / ``Sideways`` / ``Downtrend`` (quoted verbatim from the
  matching report's embedded labels per QNT-207).
* ``verdict`` is a closed three-state set so the frontend pill can
  colour-code without string-matching.

Field descriptions are picked up by ``with_structured_output`` and become
part of the JSON schema the LLM sees, so they double as inline prompting.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent.disclaimer import DISCLAIMER

Verdict = Literal["Overweight", "Neutral", "Underweight"]

# Aspect verdict labels — the values the FUNDAMENTAL and TECHNICAL aspects
# may carry. Company and News stay narrative-only (``label=None``). These
# strings match the labels QNT-207 embeds in the report templates so the
# LLM can quote them verbatim.
AspectLabel = Literal[
    "Premium",
    "Inline",
    "Discounted",
    "Uptrend",
    "Sideways",
    "Downtrend",
]


class AspectView(BaseModel):
    """One source aspect inside a Thesis (company / fundamental / technical / news).

    Each aspect is a self-contained analyst paragraph: a summary, two
    bullet lists framing the read, and an optional label that names the
    aspect's verdict in the matching report's own vocabulary.
    """

    label: str | None = Field(
        default=None,
        description=(
            "Aspect verdict label. Fundamental carries one of "
            "Premium / Inline / Discounted; Technical carries one of "
            "Uptrend / Sideways / Downtrend. Quote the label verbatim from "
            "the matching report (the report templates print these "
            "explicitly). Company and News are narrative-only aspects -- "
            "pass null."
        ),
    )
    summary: str = Field(
        description=(
            "Two to three sentences of analytical prose for this aspect. "
            "Cite (source: company|technical|fundamental|news) on each "
            "sentence that makes a numeric or factual claim. Every digit "
            "must appear verbatim in the supplied reports."
        ),
    )
    supports: list[str] = Field(
        default_factory=list,
        description=(
            "Bullets that argue FOR the aspect's label. Each bullet is one "
            "sentence with an inline citation. Leave EMPTY when the "
            "supplied report does not support the label (asymmetry is "
            "expected; do not pad)."
        ),
    )
    challenges: list[str] = Field(
        default_factory=list,
        description=(
            "Bullets that argue AGAINST the aspect's label, or that "
            "complicate it. Each bullet is one sentence with an inline "
            "citation. Leave EMPTY when the supplied report contains no "
            "real counter-evidence."
        ),
    )


class Thesis(BaseModel):
    """Structured investment thesis with four aspect blocks + a verdict.

    Returned by the synthesize node when ``with_structured_output(Thesis)``
    succeeds. Consumers that want a flat string (CLI ``--output``, the
    QNT-67 evals) call :meth:`to_markdown`; consumers that want JSON (the
    SSE endpoint, frontend) call :meth:`model_dump`.
    """

    company: AspectView = Field(
        description=(
            "Business context aspect drawn from the company report. "
            "Narrative only -- ``label`` is null."
        ),
    )
    fundamental: AspectView = Field(
        description=(
            "Valuation / earnings / margin aspect drawn from the "
            "fundamental report. ``label`` is one of Premium / Inline / "
            "Discounted, quoted verbatim from the report."
        ),
    )
    technical: AspectView = Field(
        description=(
            "Price-action / indicator aspect drawn from the technical "
            "report. ``label`` is one of Uptrend / Sideways / Downtrend, "
            "quoted verbatim from the report."
        ),
    )
    news: AspectView = Field(
        description=(
            "Headline-flow aspect drawn from the news report. Narrative only -- ``label`` is null."
        ),
    )
    verdict: Verdict = Field(
        description=(
            "Final verdict on the ticker. Use Overweight when at least two "
            "aspects carry favourable labels and no aspect carries a "
            "critically unfavourable label; Underweight when at least two "
            "aspects carry unfavourable labels and news has at least one "
            "negative catalyst; Neutral otherwise."
        ),
    )
    verdict_rationale: str = Field(
        description=(
            "Two to three sentences naming which aspect labels shaped the "
            "verdict. Must mention at least one aspect's label verbatim "
            "(Premium, Inline, Discounted, Uptrend, Sideways, or "
            "Downtrend). Cite (source: ...) for any numeric claim."
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
        for heading, aspect in (
            ("Company", self.company),
            ("Fundamental", self.fundamental),
            ("Technical", self.technical),
            ("News", self.news),
        ):
            parts.append(f"## {heading}")
            if aspect.label:
                parts.append(f"**Label:** {aspect.label}")
            parts.append(aspect.summary.strip() or "_(no summary supplied)_")
            if aspect.supports:
                for point in aspect.supports:
                    parts.append(f"+ {point.strip()}")
            if aspect.challenges:
                for point in aspect.challenges:
                    parts.append(f"· {point.strip()}")
            parts.append("")

        parts.append("## Verdict")
        parts.append(f"**{self.verdict}**")
        parts.append(self.verdict_rationale.strip() or "_(no rationale supplied)_")

        parts.append(f"\n{DISCLAIMER}")
        return "\n".join(parts).strip()


__all__ = ["AspectLabel", "AspectView", "Thesis", "Verdict"]
