"""QNT-294 (AC1): build-time dependencies threaded into every graph node.

The node closures used to capture ``tools`` / ``event_emitter`` /
``compact_company_tool`` / ``comparison_metrics_tool`` / ``active_retrievals``
from ``build_graph``'s scope. Now that the nodes are module-level functions they
receive this frozen ``GraphDeps`` bundle explicitly, plus the three small
dispatch helpers (``effective_tools`` / ``run_retrievals`` /
``followup_fires_search``) that also used to be closures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.policy import (
    _COMPACT_COMPANY_INTENTS,
    ComparisonMetricsToolFn,
    EventEmitter,
    SearchToolFn,
    ToolFn,
)

if TYPE_CHECKING:
    from agent.intent import Intent
    from agent.support import RetrievalSpec

logger = logging.getLogger("agent.graph")


@dataclass(frozen=True)
class GraphDeps:
    """Build-time wiring shared by every node (QNT-294)."""

    tools: dict[str, ToolFn]
    event_emitter: EventEmitter | None
    compact_company_tool: ToolFn | None
    comparison_metrics_tool: ComparisonMetricsToolFn | None
    active_retrievals: tuple[tuple[RetrievalSpec, SearchToolFn | None], ...]

    def effective_tools(self, intent: Intent) -> dict[str, ToolFn]:
        """Swap the compact company tool into the ``company`` slot for the
        thesis/comparison hot path; keep the full report for every other intent.
        Returns the original map untouched when no compact tool was supplied."""
        if (
            self.compact_company_tool is not None
            and "company" in self.tools
            and intent in _COMPACT_COMPANY_INTENTS
        ):
            return {**self.tools, "company": self.compact_company_tool}
        return self.tools

    def run_retrievals(
        self, state: AgentState, reports: dict[str, str]
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        """QNT-291: drive every gated retrieval corpus and fold the hits.

        The single dispatch loop that replaces the four hand-written RAG
        branches (news + earnings, in both the cold and followup gather
        paths). For each ``RetrievalSpec`` whose gate fires
        (``spec.fires``), calls the injected tool with the same
        ``search_query`` -> raw-question fallback the branches used (QNT-289),
        folds the result via ``spec.fold``, and appends its provenance rows.
        The ``[Rn]`` ids continue past hits already folded (``len(...) + 1``)
        so the combined list stays R1..Rn gap-free in registry order (QNT-301,
        news before earnings). A tool exception is swallowed to ``"[]"`` --
        search is additive and must never crash gather. Returns the folded
        ``reports`` (a copy is the caller's responsibility) and the ordered
        provenance rows.
        """
        ticker = state["ticker"]
        # QNT-289: the classifier's self-contained rewrite is the query when it
        # survived sanitize_search_query; "" falls back to the raw (possibly
        # elliptical) question -- today's behaviour, so recall can only gain.
        query = state.get("search_query") or state.get("question", "")
        retrieved_sources: list[dict[str, str]] = []
        for spec, tool in self.active_retrievals:
            if not spec.fires(tool, state):
                continue
            assert tool is not None  # spec.fires guarantees this
            try:
                raw = tool(ticker, query)
            except Exception as exc:  # noqa: BLE001 — search is additive; never crash gather
                logger.warning("gather %s: %s failed: %s (continuing)", ticker, spec.name, exc)
                raw = "[]"
            reports, sources = spec.fold(reports, raw, len(retrieved_sources) + 1)
            if sources:
                retrieved_sources += sources
                logger.info(
                    "gather %s: folded %d %s hits into %s report",
                    ticker,
                    len(sources),
                    spec.corpus,
                    spec.corpus if spec.corpus == "news" else "fundamental",
                )
        return reports, retrieved_sources

    def followup_fires_search(self, state: AgentState) -> bool:
        """QNT-290/291: True when a followup turn should visit plan/gather for
        RAG at all (any corpus). Used by ``classify_router``'s routing decision;
        ``gather_node`` calls ``run_retrievals`` which re-checks each spec's
        gate, so the router and the loop can never drift apart -- both read
        ``RetrievalSpec.fires`` over the same ``active_retrievals``."""
        return any(spec.fires(tool, state) for spec, tool in self.active_retrievals)


if TYPE_CHECKING:
    from agent.graph import AgentState
