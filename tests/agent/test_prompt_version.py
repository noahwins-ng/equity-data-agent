"""Tests for the shared prompt-version hash (QNT-230 #11).

The hash must (a) be identical across the ``agent.graph`` and
``agent.evals.golden_set`` copies -- they previously drifted -- and (b) move
when a routing prompt (classify / plan) changes, so version-filtered Langfuse
before/after comparisons can't silently mix two routing behaviours.
"""

from __future__ import annotations

from agent.evals.golden_set import _prompt_version as golden_prompt_version
from agent.graph import (
    _build_plan_prompt,
    _build_thesis_plan_prompt,
)
from agent.graph import (
    _prompt_version as graph_prompt_version,
)
from agent.prompt_version import compute_prompt_version


def test_graph_and_golden_set_versions_agree() -> None:
    assert graph_prompt_version() == golden_prompt_version()


def test_changing_classify_prompt_changes_version(monkeypatch) -> None:
    before = compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)
    monkeypatch.setattr(
        "agent.intent._CLASSIFY_PROMPT",
        "totally different classifier prompt {history} {question}",
    )
    after = compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)
    assert before != after


def test_changing_plan_prompt_changes_version() -> None:
    baseline = compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)

    def _altered_plan_prompt(ticker, question, available, intent="thesis"):
        return _build_plan_prompt(ticker, question, available) + " EXTRA GUIDANCE"

    altered = compute_prompt_version(_altered_plan_prompt, _build_thesis_plan_prompt)
    assert baseline != altered


def test_changing_narrate_prompt_changes_version(monkeypatch) -> None:
    """QNT-303: the narrate voice surface is now inside the hash, so a narrate
    rule edit (e.g. the D-1 falsifier) bumps prompt_version instead of shipping
    invisibly."""
    before = compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)
    monkeypatch.setattr(
        "agent.prompts.NARRATE_FALSIFIER_RULE",
        "an entirely different falsifier rule body",
    )
    after = compute_prompt_version(_build_plan_prompt, _build_thesis_plan_prompt)
    assert before != after
