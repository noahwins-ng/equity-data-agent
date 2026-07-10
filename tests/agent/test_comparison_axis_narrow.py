"""QNT-358: axis-narrowed comparison plan + render-what-was-gathered.

Two compounding causes made an axis-named comparison fail-close the QNT-351
synthesis cap: (1) plan_node never narrowed for comparison, so all four reports
were gathered for both tickers; (2) ComparisonSection forced the full matrix.
These tests pin the fix:

* ``comparison_axis`` deterministically resolves the single named axis.
* plan_node narrows the (symmetric) comparison plan to ``["company", <axis>]``
  for each of fundamental / technical / news, and keeps the full plan with no
  axis. Asserted by exact plan-SET, NOT tool_call_ok (which is subset-based and
  passes on over-fetch -- the whole reason the goldens could not pin this).
* ComparisonSection's non-company aspects are optional and to_markdown tolerates
  None (the QNT-324 followup/narrate grounding substrate).
"""

from __future__ import annotations

from typing import cast

import pytest
from agent import graph as graph_module
from agent.comparison import ComparisonAnswer, ComparisonSection
from agent.graph import AgentState
from agent.intent import comparison_axis
from agent.nodes.deps import GraphDeps
from agent.nodes.plan import plan_node
from agent.thesis import AspectView

# ─────────────────────── comparison_axis unit ────────────────────────────


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Compare NVDA vs AAPL on valuation and fundamentals.", "fundamental"),
        ("Compare AMZN vs META -- which has the stronger fundamental case?", "fundamental"),
        (
            "Compare TSLA vs AMD on technical momentum -- who has the stronger chart setup?",
            "technical",
        ),
        ("Compare NVDA vs MSFT on technical momentum.", "technical"),
        ("Compare NVDA vs AMD on recent news and catalysts.", "news"),
        # No axis named -> full matrix (None).
        ("Compare NVDA vs AMD.", None),
        ("How does META stack up against GOOGL?", None),
        # More than one axis named -> not a single-axis narrow (None).
        ("Compare NVDA vs AMD on fundamentals and technicals.", None),
    ],
)
def test_comparison_axis_resolves_single_named_axis(question: str, expected: str | None) -> None:
    assert comparison_axis(question) == expected


# ─────────────────────── plan_node narrowing ─────────────────────────────


def _deps() -> GraphDeps:
    tools = {name: (lambda _t: "") for name in graph_module.REPORT_TOOLS}
    return GraphDeps(
        tools=cast(dict, tools),
        event_emitter=None,
        compact_company_tool=None,
        comparison_metrics_tool=None,
        active_retrievals=(),
    )


def _plan_for(question: str) -> list[str]:
    state = cast(
        AgentState,
        {
            "ticker": "NVDA",
            "question": question,
            "intent": "comparison",
            # classify resolves the pair; plan_node consumes it.
            "comparison_tickers": ["NVDA", "AMD"],
        },
    )
    result = plan_node(state, {}, _deps())
    return cast(list[str], result["plan"])


@pytest.mark.parametrize(
    ("question", "axis"),
    [
        ("Compare NVDA vs AMD on valuation and fundamentals.", "fundamental"),
        ("Compare NVDA vs AMD on technical momentum.", "technical"),
        ("Compare NVDA vs AMD on recent news and catalysts.", "news"),
    ],
)
def test_axis_named_comparison_narrows_to_company_plus_axis(question: str, axis: str) -> None:
    """AC1: an axis-named comparison narrows the plan to exactly company + that
    axis (applied symmetrically -- gather runs the SAME plan against both
    tickers). Exact set assertion, not subset."""
    assert set(_plan_for(question)) == {"company", axis}


def test_no_axis_comparison_keeps_full_plan() -> None:
    """AC1: a no-axis comparison keeps the full four-aspect plan, unchanged."""
    assert set(_plan_for("Compare NVDA vs AMD.")) == set(graph_module.REPORT_TOOLS)


# ─────────────────────── optional-aspect schema + render ──────────────────


def _aspect(summary: str) -> AspectView:
    return AspectView(label=None, summary=summary, supports=[], challenges=[])


def test_comparison_section_non_company_aspects_default_none() -> None:
    """AC2/AC3: only company is required; the other aspects default to None so a
    narrowed comparison can omit them."""
    section = ComparisonSection(ticker="NVDA", company=_aspect("Business context."))
    assert section.fundamental is None
    assert section.technical is None
    assert section.news is None


def test_to_markdown_omits_none_aspects() -> None:
    """AC2/AC3: to_markdown (the followup/narrate grounding substrate) renders
    only the gathered aspects and never dereferences a None aspect."""
    answer = ComparisonAnswer(
        sections=[
            ComparisonSection(
                ticker="NVDA",
                company=_aspect("NVDA business."),
                technical=_aspect("NVDA momentum read."),
            ),
            ComparisonSection(
                ticker="AMD",
                company=_aspect("AMD business."),
                technical=_aspect("AMD momentum read."),
            ),
        ],
        differences="NVDA screens more stretched technically than AMD.",
    )
    md = answer.to_markdown()
    assert "Technical" in md
    assert "NVDA momentum read." in md
    # The omitted aspects never render a heading.
    assert "Fundamental" not in md
    assert "News" not in md
