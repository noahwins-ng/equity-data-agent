"""QNT-309: pin the unified answer-projection contract.

Two guardrails for the payload-projection seam QNT-294 split across modules:

* AC1 -- ``narrate_node`` and ``_assistant_surface`` now share ONE payload-pick
  precedence via :func:`agent.support._pick_payload`. Before QNT-309 narrate read
  ``prior_answer`` first while ``_assistant_surface`` read ``answer`` first; the
  two disagreed only on a followup metric turn. ``test_followup_metric_substrate_*``
  pin the chosen precedence (prior_answer wins for followup) so it can't silently
  drift back.
* AC2 -- ``_synthesize_payload`` and ``clarify_node`` project their answer through
  the ``project_answer`` / ``_answer`` writer in every branch EXCEPT a small,
  deliberate set of narrow hand-written ``{"answer": ...}`` returns (the followup
  and clarify paths). ``test_no_new_narrow_projection_branch`` enumerates those
  returns from the AST and fails when a new one appears outside the allow-list.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections import Counter
from typing import cast

from agent.graph import AgentState
from agent.nodes.clarify import clarify_node
from agent.nodes.synthesize import _synthesize_payload
from agent.quick_fact import QuickFactAnswer
from agent.support import _pick_payload

from ._thesis_factory import make_thesis


def _prior_thesis():
    return make_thesis(
        company_summary="NVDA framing (source: company).",
        verdict="Overweight",
        verdict_rationale="AI demand durable (source: fundamental).",
    )


def _this_turn_quick_fact() -> QuickFactAnswer:
    return QuickFactAnswer(
        answer="RSI 78 overbought (source: technical).",
        cited_value="78",
        source="technical",
    )


# ─── AC1: the shared payload-pick precedence ────────────────────────────────


def test_followup_metric_substrate_is_prior_answer() -> None:
    """A followup metric turn carries BOTH a hydrated prior thesis and this
    turn's QuickFactAnswer card. The unified precedence picks the prior thesis
    (the substrate narrate speaks over), NOT this turn's compact card."""
    prior = _prior_thesis()
    state = cast(
        AgentState,
        {"intent": "followup", "answer": _this_turn_quick_fact(), "prior_answer": prior},
    )
    assert _pick_payload(state) is prior


def test_non_followup_substrate_is_this_turn_answer() -> None:
    """Every non-followup intent reads THIS turn's ``answer`` even when classify
    has carried a hydrated Thesis in ``prior_answer`` -- the guard is on the
    intent, so a fresh quick_fact turn narrates its own card, not the prior."""
    this_turn = _this_turn_quick_fact()
    state = cast(
        AgentState,
        {"intent": "quick_fact", "answer": this_turn, "prior_answer": _prior_thesis()},
    )
    assert _pick_payload(state) is this_turn


def test_followup_narrative_only_falls_back_to_prior() -> None:
    """A narrative-only followup carries ``answer=None``; the substrate is the
    hydrated prior thesis so narrate has something to react to."""
    prior = _prior_thesis()
    state = cast(AgentState, {"intent": "followup", "answer": None, "prior_answer": prior})
    assert _pick_payload(state) is prior


def test_non_followup_answer_none_does_not_borrow_prior() -> None:
    """The second divergence QNT-309 collapses: a NON-followup turn whose
    ``answer`` is None (the focused news/fundamental RAG-drop, ``{"answer": None}``)
    following a thesis turn. The old ``_assistant_surface`` (``answer or
    prior_answer``) borrowed the stale prior Thesis for the transcript anchor;
    the shared precedence returns None so the anchor tracks the narrated report,
    not a thesis narrate never spoke over."""
    state = cast(AgentState, {"intent": "news", "answer": None, "prior_answer": _prior_thesis()})
    assert _pick_payload(state) is None


# ─── AC2: the narrow-projection allow-list ──────────────────────────────────

# Every ``return {...}`` dict literal that writes the ``answer`` key DIRECTLY
# (rather than flowing through ``project_answer`` / ``_answer``, whose dict
# spreads ``**project_answer(...)`` and carries no explicit ``answer`` key) is a
# deliberate narrow projection. Each is enumerated here with the branch it
# guards. A new narrow ``{"answer": ...}`` return -- e.g. a future synthesize or
# clarify branch that copies the pattern instead of calling ``_answer`` -- shifts
# this multiset and fails the test below. To resolve: route the branch through
# ``_answer`` / ``project_answer``, or (if the narrow return is deliberate) add
# it here with a one-line justification.
_ALLOWED_NARROW_RETURNS: dict[str, Counter[tuple[str, ...]]] = {
    "_synthesize_payload": Counter(
        {
            # followup narrative-only ({"answer": None}), followup metric ask
            # ({"answer": QuickFactAnswer}), focused RAG-drop ({"answer": None}) --
            # all pair the narrow answer with this run's confidence.
            ("answer", "confidence"): 3,
        }
    ),
    "clarify_node": Counter(
        {
            # clarify LLM fallback -> domain_redirect, and clarify success ->
            # ConversationalAnswer. Both set only ``answer`` (no confidence): a
            # clarify turn gathered no reports, so there is nothing to score.
            ("answer",): 2,
        }
    ),
}


def _narrow_answer_return_signatures(func) -> Counter[tuple[str, ...]]:
    """Return-dict literals in ``func`` that write ``answer`` as an explicit key.

    Walks the function's AST (nested ``_answer`` / ``_fallback`` helpers
    included). A dict that projects through ``project_answer`` spreads it as
    ``**project_answer(...)`` and has no explicit ``"answer"`` key, so it is
    excluded; ``_fallback`` returns ``_answer(...)`` (a call, not a dict literal)
    and is excluded too. What remains is exactly the hand-written narrow returns.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    signatures: Counter[tuple[str, ...]] = Counter()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)):
            continue
        keys = [
            k.value
            for k in node.value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if "answer" in keys:
            signatures[tuple(sorted(keys))] += 1
    return signatures


def test_no_new_narrow_projection_branch() -> None:
    """The synthesize / clarify narrow ``{"answer": ...}`` returns match the
    documented allow-list exactly -- a new one (or a removed one) fails here."""
    for func, name in (
        (_synthesize_payload, "_synthesize_payload"),
        (clarify_node, "clarify_node"),
    ):
        actual = _narrow_answer_return_signatures(func)
        assert actual == _ALLOWED_NARROW_RETURNS[name], (
            f"{name}: narrow-projection returns drifted from the QNT-309 allow-list. "
            "Route the new branch through _answer / project_answer, or add it to "
            "_ALLOWED_NARROW_RETURNS with a justification."
        )
