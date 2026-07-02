"""Shared test factory for v2 Thesis / ComparisonSection (QNT-208).

Tests that previously constructed v1 Thesis(setup, bull_case, ...) now
import these helpers so a future schema tweak only touches one file.
"""

from __future__ import annotations

from agent.comparison import ComparisonSection
from agent.thesis import AspectView, Thesis


def make_aspect(
    label: str | None = None,
    summary: str = "x",
    supports: list[str] | None = None,
    challenges: list[str] | None = None,
) -> AspectView:
    return AspectView(
        label=label,  # pyright: ignore[reportArgumentType]  # str normalized by field validator
        summary=summary,
        supports=supports or [],
        challenges=challenges or [],
    )


def make_thesis(
    *,
    supports: list[str] | None = None,
    challenges: list[str] | None = None,
    verdict: str = "Neutral",
    verdict_rationale: str = "Premium paired with Uptrend (source: technical).",
    company_summary: str = "Company framing (source: company).",
) -> Thesis:
    """A reasonable v2 stub thesis. Override aspect-level fields when needed.

    ``supports`` and ``challenges`` go into the Technical aspect by default,
    which is where v1 ``bull_case`` and ``bear_case`` content most naturally
    landed.
    """
    return Thesis(
        company=make_aspect(label=None, summary=company_summary),
        fundamental=make_aspect(
            label="Premium",
            summary="Multiple Premium (source: fundamental).",
        ),
        technical=make_aspect(
            label="Uptrend",
            summary="TREND Uptrend (source: technical).",
            supports=supports if supports is not None else ["bull (source: technical)"],
            challenges=challenges if challenges is not None else [],
        ),
        news=make_aspect(label=None, summary="No headlines (source: news)."),
        verdict=verdict,  # pyright: ignore[reportArgumentType]
        verdict_rationale=verdict_rationale,
    )


def make_comparison_section(
    ticker: str = "NVDA",
    fundamental_label: str = "Premium",
    technical_label: str = "Uptrend",
) -> ComparisonSection:
    return ComparisonSection(
        ticker=ticker,
        company=make_aspect(label=None, summary=f"{ticker} context (source: company)."),
        fundamental=make_aspect(
            label=fundamental_label,
            summary=f"{ticker} sits {fundamental_label} (source: fundamental).",
        ),
        technical=make_aspect(
            label=technical_label,
            summary=f"{ticker} TREND {technical_label} (source: technical).",
        ),
        news=make_aspect(label=None, summary=f"{ticker} no headlines (source: news)."),
    )


__all__ = ["make_aspect", "make_comparison_section", "make_thesis"]
