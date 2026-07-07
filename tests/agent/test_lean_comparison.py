"""Tests for the lean N-way comparison path (QNT-224).

3-4 tickers take a NEW lean metrics shape (LeanComparisonAnswer), distinct
from the rich two-ticker ComparisonAnswer. The lean synthesize branch makes NO
LLM call — it parses the metrics JSON the gather node fetched from the API and
builds the answer deterministically (ADR-003). 5+ tickers redirect; the
two-ticker rich path is unchanged.

The metrics fetch is injected as ``comparison_metrics_tool`` so these tests
never hit a live API; the fake echoes a row per requested ticker.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from agent import graph as graph_module
from agent.comparison import ComparisonAnswer, LeanComparisonAnswer
from agent.graph import build_graph
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from ._thesis_factory import make_comparison_section
from .test_graph import _mock_tool, _StructuredLLM


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> _StructuredLLM:
    """Local copy of test_graph's stub_llm fixture (importing it by name trips
    F811 against the same-named test params). Patches both graph + intent
    get_llm so the classifier heuristic never reaches the live proxy."""
    from unittest.mock import MagicMock

    from agent import intent as intent_module

    llm = _StructuredLLM()
    factory = MagicMock(return_value=llm)
    monkeypatch.setattr(graph_module, "get_llm", factory)
    monkeypatch.setattr(intent_module, "get_llm", factory)
    return llm


def _fake_metrics_tool(rows: dict[str, dict[str, str]] | None = None):
    """Return a (list[str]) -> JSON-str metrics tool that echoes one row per
    requested ticker. ``rows`` overrides specific tickers' cells."""
    rows = rows or {}

    labels = ["Premium", "Inline", "Discounted", "Inline"]
    trends_d = ["Uptrend", "Sideways", "Downtrend", "Sideways"]
    trends_w = ["Sideways", "Uptrend", "Sideways", "Downtrend"]

    def tool(tickers: list[str]) -> str:
        out = []
        for i, t in enumerate(tickers):
            default = {
                "ticker": t,
                "pe": f"2{i}.4",
                "rsi": f"6{i}.2",
                "net_margin": f"2{i}.1%",
                "price": f"${100 + i}.50",
                "valuation_label": labels[i % len(labels)],
                "trend_daily": trends_d[i % len(trends_d)],
                "trend_weekly": trends_w[i % len(trends_w)],
            }
            default.update(rows.get(t, {}))
            out.append(default)
        return json.dumps({"rows": out})

    return tool


def _as_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("comparison", "heuristic", False, False, "", [], ""),
    )


def test_three_ticker_comparison_produces_lean_answer(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 3-ticker compare yields an N-section LeanComparisonAnswer (one row per
    ticker, in order) and never the rich ComparisonAnswer."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    graph = build_graph(
        {"fundamental": _mock_tool("fund")},
        comparison_metrics_tool=_fake_metrics_tool(),
    )

    # Primary ticker = AAPL (one of the named three) so resolve does NOT append
    # a fourth — exercises an exact 3-way.
    result = graph.invoke({"ticker": "AAPL", "question": "Compare AAPL, MSFT and GOOGL."})

    assert result["intent"] == "comparison"
    lean = result["answer"]
    assert isinstance(lean, LeanComparisonAnswer)
    assert [r.ticker for r in lean.rows] == ["AAPL", "MSFT", "GOOGL"]
    # QNT-224 follow-up: the report-derived labels ride through verbatim and
    # land in to_markdown so narrate (and the grounding substrate) see them.
    assert lean.rows[0].valuation_label == "Premium"
    assert lean.rows[0].trend_daily == "Uptrend"
    assert lean.rows[0].trend_weekly == "Sideways"
    md = lean.to_markdown()
    assert "Premium" in md and "Uptrend" in md and "Trend (weekly)" in md
    # The rich two-ticker payload is NOT produced on this path.
    # The synthesize LLM (structured output) was never called for the lean path.
    stub_llm.structured_invoke.assert_not_called()


def test_lean_metrics_stashed_as_grounding_substrate(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advisor #1: the lean gather must stash the metrics text into ``reports``
    so _runtime_report_texts (and the narrate grounding check) sees the numbers
    the table / narration quote — otherwise every lean number reads ungrounded."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    graph = build_graph(
        {"fundamental": _mock_tool("fund")},
        comparison_metrics_tool=_fake_metrics_tool({"AAPL": {"pe": "28.4"}}),
    )

    result = graph.invoke({"ticker": "AAPL", "question": "Compare AAPL, MSFT and GOOGL."})

    texts = graph_module._runtime_report_texts(result)  # type: ignore[arg-type] — dict surface
    blob = "\n".join(texts)
    # The exact metric value is present in the runtime report substrate, so a
    # narrative quoting it grounds clean.
    assert "28.4" in blob
    _, rate = graph_module._runtime_grounding_check("AAPL trades around 28.4.", texts)
    assert rate == 1.0


def test_four_ticker_comparison_is_lean(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is four — a 4-way compare still takes the lean path."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    graph = build_graph(
        {"fundamental": _mock_tool("fund")},
        comparison_metrics_tool=_fake_metrics_tool(),
    )

    result = graph.invoke({"ticker": "AAPL", "question": "Compare AAPL, MSFT, GOOGL and AMZN."})

    lean = result["answer"]
    assert isinstance(lean, LeanComparisonAnswer)
    assert [r.ticker for r in lean.rows] == ["AAPL", "MSFT", "GOOGL", "AMZN"]


def test_five_tickers_redirects_without_fetching_metrics(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5+ named tickers exceed the cap: redirect, and never call the metrics
    tool (the plan gate empties the list before gather)."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")

    calls: list[list[str]] = []

    def spy_tool(tickers: list[str]) -> str:
        calls.append(tickers)
        return _fake_metrics_tool()(tickers)

    graph = build_graph(
        {"fundamental": _mock_tool("fund")},
        comparison_metrics_tool=spy_tool,
    )

    result = graph.invoke(
        {"ticker": "AAPL", "question": "Compare AAPL, MSFT, GOOGL, AMZN and META."}
    )

    # Deterministic conversational redirect landed instead.
    assert result.get("answer") is not None
    # Metrics tool was never invoked for an over-cap set.
    assert calls == []


def test_two_ticker_path_stays_rich_and_unchanged(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tickers keep the rich four-aspect ComparisonAnswer; the lean slot
    stays empty even when a metrics tool is wired."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    stub_llm.structured_invoke.return_value = ComparisonAnswer(
        sections=[
            make_comparison_section("NVDA", "Premium", "Uptrend"),
            make_comparison_section("AAPL", "Inline", "Sideways"),
        ],
        differences="NVDA carries a richer multiple than AAPL (source: fundamental).",
    )
    graph = build_graph(
        {"fundamental": _mock_tool("fund")},
        comparison_metrics_tool=_fake_metrics_tool(),
    )

    result = graph.invoke({"ticker": "NVDA", "question": "Compare NVDA vs AAPL on valuation."})

    assert isinstance(result["answer"], ComparisonAnswer)
    assert set(result["reports_by_ticker"]) == {"NVDA", "AAPL"}


def test_offpage_two_named_stays_rich_no_primary_inflation(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a 2-named compare from a page whose ticker is NOT named must
    stay the rich 2-way card. The page-context primary (NVDA) is only a
    minimum-backfill; it must NOT inflate a filled 2-ticker request into a lean
    3-way that includes a ticker the user never asked about."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    stub_llm.structured_invoke.return_value = ComparisonAnswer(
        sections=[
            make_comparison_section("AAPL", "Premium", "Uptrend"),
            make_comparison_section("MSFT", "Inline", "Sideways"),
        ],
        differences="AAPL carries a richer multiple than MSFT (source: fundamental).",
    )
    graph = build_graph(
        {"fundamental": _mock_tool("fund")},
        comparison_metrics_tool=_fake_metrics_tool(),
    )

    # On /ticker/NVDA, but the user named exactly AAPL + MSFT.
    result = graph.invoke({"ticker": "NVDA", "question": "Compare AAPL and MSFT."})

    assert isinstance(result["answer"], ComparisonAnswer)
    # NVDA (the page primary) is NOT pulled into the comparison.
    assert set(result["reports_by_ticker"]) == {"AAPL", "MSFT"}


# ─────────────── QNT-302: LeanComparisonRow label normalization (AC1) ─────────


def test_lean_row_normalizes_label_casing() -> None:
    """The three pill fields share the frontend ASPECT_LABEL_PILL palette, so
    off-casing must coerce to the canonical spelling."""
    from agent.comparison import LeanComparisonRow

    row = LeanComparisonRow(
        ticker="NVDA",
        pe="28.4",
        rsi="61",
        net_margin="25%",
        price="$120",
        valuation_label="discounted",  # pyright: ignore[reportArgumentType]  # normalized by validator
        trend_daily="UPTREND",  # pyright: ignore[reportArgumentType]
        trend_weekly="Sideways",
    )
    assert row.valuation_label == "Discounted"
    assert row.trend_daily == "Uptrend"
    assert row.trend_weekly == "Sideways"


def test_lean_row_off_vocabulary_labels_normalize_to_none() -> None:
    """An off-vocabulary label (e.g. the API's 'N/M' sentinel) maps to None so
    the frontend renders a muted dash, never an unknown pill key."""
    from agent.comparison import LeanComparisonRow

    row = LeanComparisonRow(
        ticker="NVDA",
        pe="N/M",
        rsi="N/M",
        net_margin="N/M",
        price="$120",
        valuation_label="N/M",  # pyright: ignore[reportArgumentType]  # normalized to None by validator
        trend_daily="Bullish",  # pyright: ignore[reportArgumentType]
        trend_weekly=None,
    )
    assert row.valuation_label is None
    assert row.trend_daily is None
    assert row.trend_weekly is None


# ─────────────── QNT-326 (G-14): comparison RAG demand detector ──────────────


@pytest.mark.parametrize(
    ("news", "earnings", "expected"),
    [
        (False, False, ""),
        (True, False, "news"),
        (False, True, "earnings"),
        (True, True, "news+earnings"),
    ],
)
def test_comparison_rag_demand_maps_flags(news: bool, earnings: bool, expected: str) -> None:
    """The detector joins the flagged corpora a comparison turn asked for but
    can't fold (comparison's rag_corpora is empty). "" when neither fired."""
    from agent.nodes.gather import _comparison_rag_demand

    state = {"needs_news_search": news, "needs_earnings_search": earnings}
    assert _comparison_rag_demand(state) == expected  # type: ignore[arg-type]


def test_flagged_comparison_turn_records_demand(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A targeted-event comparison ("... on their antitrust exposure") whose
    classifier set needs_news_search records the discarded demand in state --
    the signal the SSE handler stamps as comparison_rag_demand:news."""
    monkeypatch.setattr(
        graph_module,
        "classify_intent_with_source",
        lambda _q, **_: ("comparison", "llm", True, False, "antitrust exposure", [], ""),
    )
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    stub_llm.structured_invoke.return_value = ComparisonAnswer(
        sections=[
            make_comparison_section("NVDA", "Premium", "Uptrend"),
            make_comparison_section("AMD", "Inline", "Sideways"),
        ],
        differences="NVDA carries a richer multiple than AMD (source: fundamental).",
    )
    graph = build_graph({"fundamental": _mock_tool("fund")})

    result = graph.invoke(
        {"ticker": "NVDA", "question": "Compare NVDA and AMD on their antitrust exposure."}
    )

    assert isinstance(result["answer"], ComparisonAnswer)
    assert result["comparison_rag_demand"] == "news"


def test_unflagged_comparison_turn_records_no_demand(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero behaviour diff: a plain comparison (no search flags) records "" so
    no comparison_rag_demand tag is stamped."""
    _as_comparison(monkeypatch)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    stub_llm.structured_invoke.return_value = ComparisonAnswer(
        sections=[
            make_comparison_section("NVDA", "Premium", "Uptrend"),
            make_comparison_section("AAPL", "Inline", "Sideways"),
        ],
        differences="NVDA carries a richer multiple than AAPL (source: fundamental).",
    )
    graph = build_graph({"fundamental": _mock_tool("fund")})

    result = graph.invoke({"ticker": "NVDA", "question": "Compare NVDA vs AAPL on valuation."})

    assert result.get("comparison_rag_demand", "") == ""


def test_comparison_rag_demand_does_not_bleed_across_turns(
    stub_llm: _StructuredLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the demand marker must NOT persist across turns via the
    checkpointer. Turn 1 is a flagged comparison (demand="news"); turn 2 on the
    SAME thread is a conversational greeting that short-circuits and SKIPS gather
    entirely -- so only classify_node's turn-boundary reset can clear the stale
    value. Without the reset the SSE handler would stamp a false
    comparison_rag_demand:news tag on the unrelated greeting turn."""

    def fake_classify(
        question: str, **_: object
    ) -> tuple[str, str, bool, bool, str, list[str], str]:
        if question.strip().lower() == "hi":
            return ("conversational", "heuristic", False, False, "", [], "")
        return ("comparison", "llm", True, False, "antitrust exposure", [], "")

    monkeypatch.setattr(graph_module, "classify_intent_with_source", fake_classify)
    stub_llm.invoke.return_value = AIMessage(content="fundamental")
    stub_llm.structured_invoke.return_value = ComparisonAnswer(
        sections=[
            make_comparison_section("NVDA", "Premium", "Uptrend"),
            make_comparison_section("AMD", "Inline", "Sideways"),
        ],
        differences="NVDA carries a richer multiple than AMD (source: fundamental).",
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    graph = build_graph({"fundamental": _mock_tool("fund")}, checkpointer=SqliteSaver(conn))
    config: RunnableConfig = {"configurable": {"thread_id": "bleed:NVDA"}}

    first = graph.invoke(
        {"ticker": "NVDA", "question": "Compare NVDA and AMD on their antitrust exposure."},
        config=config,
    )
    assert first["comparison_rag_demand"] == "news"

    # Turn 2: greeting short-circuits (classify -> synthesize), gather skipped.
    second = graph.invoke({"ticker": "NVDA", "question": "hi"}, config=config)
    assert second["intent"] == "conversational"
    assert second["comparison_rag_demand"] == ""
