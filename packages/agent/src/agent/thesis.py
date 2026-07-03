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

import logging
from collections.abc import Iterable
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from agent.disclaimer import DISCLAIMER

logger = logging.getLogger(__name__)

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

# QNT-302: case-insensitive map from any casing of a canonical label to its
# canonical spelling. Off-vocabulary / non-string values normalize to None.
_ASPECT_LABEL_BY_LOWER: dict[str, AspectLabel] = {v.lower(): v for v in get_args(AspectLabel)}


def normalize_aspect_label(raw: object) -> AspectLabel | None:
    """Coerce an arbitrary label value to a canonical AspectLabel, or None.

    NORMALIZE-never-raise (QNT-302): the frontend pill palette is indexed by
    the raw label string (``ASPECT_LABEL_PILL[label]``), so an off-vocabulary
    label ("premium", "Bullish") renders a broken chip. Any value that is not
    one of the six AspectLabel spellings (case-insensitively) maps to None --
    the frontend's null-label convention (no chip). Never raises, so a
    cosmetic mismatch in LLM output can't trigger the structured-output
    retry -> fallback-redirect chain.
    """
    if not isinstance(raw, str):
        return None
    return _ASPECT_LABEL_BY_LOWER.get(raw.strip().lower())


# QNT-302: verdict-vs-labels tripwire vocabulary. The frontend colour-codes
# Discounted / Uptrend green (favourable); Premium amber and Downtrend red
# (both unfavourable); Inline / Sideways neutral zinc. "Premium" is a
# valuation *caution* (multiple above own-history 75th pct / peer median per
# the fundamental report), so it counts against, not for, a bullish verdict.
_FAVOURABLE_LABELS: frozenset[str] = frozenset({"Discounted", "Uptrend"})
_UNFAVOURABLE_LABELS: frozenset[str] = frozenset({"Premium", "Downtrend"})


def expected_verdict_from_labels(labels: Iterable[str | None]) -> Verdict:
    """Deterministic verdict implied by the aspect labels alone (QNT-302).

    Restates the ``Thesis.verdict`` Field-description rule over the two
    label-bearing aspects (fundamental + technical): Overweight when at least
    two carry favourable labels and none carries an unfavourable one;
    Underweight when at least two carry unfavourable labels; Neutral
    otherwise. The description's news-catalyst clause is not checkable from
    the Thesis payload (it carries no catalyst field), so the label-only rule
    is an advisory approximation -- used to flag drift, not to rewrite the
    verdict.
    """
    materialised = list(labels)
    favourable = sum(1 for label in materialised if label in _FAVOURABLE_LABELS)
    unfavourable = sum(1 for label in materialised if label in _UNFAVOURABLE_LABELS)
    if favourable >= 2 and unfavourable == 0:
        return "Overweight"
    if unfavourable >= 2:
        return "Underweight"
    return "Neutral"


class AspectView(BaseModel):
    """One source aspect inside a Thesis (company / fundamental / technical / news).

    Each aspect is a self-contained analyst paragraph: a summary, two
    bullet lists framing the read, and an optional label that names the
    aspect's verdict in the matching report's own vocabulary.
    """

    label: AspectLabel | None = Field(
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

    @field_validator("label", mode="before")
    @classmethod
    def _normalize_label(cls, v: object) -> AspectLabel | None:
        """QNT-302: coerce off-vocabulary / mis-cased labels to a canonical
        value or None *before* the Literal validates, so junk normalizes
        instead of raising (which would trip the structured-output retry)."""
        return normalize_aspect_label(v)

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


def render_aspect_block(heading: str, aspect: AspectView, *, level: int = 2) -> list[str]:
    """Render one aspect block (heading + label + summary + bullets) to markdown lines.

    QNT-294 (C-6): the single aspect-rendering loop shared by
    :meth:`Thesis.to_markdown` and :meth:`ComparisonAnswer.to_markdown`, which
    used to duplicate it and disagreed on the challenges glyph (thesis rendered
    ``· ``, comparison ``- ``). Supports render ``+ ``, challenges ``- `` --
    one convention across both shapes. ``level`` is the heading depth (2 for a
    top-level thesis aspect, 3 for a comparison aspect nested under a ``## {ticker}``
    section). Returns lines including the trailing blank-line separator.
    """
    hashes = "#" * level
    parts = [f"{hashes} {heading}"]
    if aspect.label:
        parts.append(f"**Label:** {aspect.label}")
    parts.append(aspect.summary.strip() or "_(no summary supplied)_")
    parts.extend(f"+ {point.strip()}" for point in aspect.supports)
    parts.extend(f"- {point.strip()}" for point in aspect.challenges)
    parts.append("")
    return parts


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
            "Discounted, quoted verbatim from the report. Use null when "
            "the fundamental report was not supplied."
        ),
    )
    technical: AspectView = Field(
        description=(
            "Price-action / indicator aspect drawn from the technical "
            "report. ``label`` is one of Uptrend / Sideways / Downtrend, "
            "quoted verbatim from the report. Use null when the technical "
            "report was not supplied."
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

    def verdict_matches_labels(self) -> bool:
        """QNT-302 advisory flag: does the verdict agree with the
        label-derived expectation? Exposed to the eval layer so a golden run
        can record the mismatch rate. Never mutates the verdict -- promotion
        to normalization is a follow-up, gated on the observed rate."""
        expected = expected_verdict_from_labels([self.fundamental.label, self.technical.label])
        return expected == self.verdict

    @model_validator(mode="after")
    def _warn_on_verdict_label_mismatch(self) -> Thesis:
        """Log one advisory line when the verdict contradicts its own aspect
        labels. ADVISORY only (QNT-302) -- does not rewrite the verdict, so a
        human-plausible read the strict label rule disagrees with still
        ships; the log makes the drift observable for the promote decision."""
        if not self.verdict_matches_labels():
            expected = expected_verdict_from_labels([self.fundamental.label, self.technical.label])
            logger.warning(
                "Thesis verdict %s inconsistent with labels "
                "(fundamental=%s technical=%s; label-rule expects %s)",
                self.verdict,
                self.fundamental.label,
                self.technical.label,
                expected,
            )
        return self

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
            parts.extend(render_aspect_block(heading, aspect, level=2))

        parts.append("## Verdict")
        parts.append(f"**{self.verdict}**")
        parts.append(self.verdict_rationale.strip() or "_(no rationale supplied)_")

        parts.append(f"\n{DISCLAIMER}")
        return "\n".join(parts).strip()


__all__ = [
    "AspectLabel",
    "AspectView",
    "Thesis",
    "Verdict",
    "expected_verdict_from_labels",
    "normalize_aspect_label",
    "render_aspect_block",
]
