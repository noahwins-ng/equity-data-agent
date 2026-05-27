"""Persona test (QNT-210): every synthesis shape carries the analyst-voice ADR marker.

ADR-020 (`docs/decisions/020-equity-analyst-voice.md`) defines the agent's
analyst voice. ``ANALYST_VOICE_ADR`` is the stable marker string threaded into
every per-shape system prompt; this test pins the contract so a future prompt
rewrite cannot silently strip the persona from one shape and leave the others
inconsistent.

The test runs against the **rendered** prompt (i.e. the SystemMessage content
emitted by each ``build_*_prompt`` function), not just the module-level
constants -- that catches a regression where the constant is updated but the
builder accidentally interpolates a different string.
"""

from __future__ import annotations

from agent.prompts import (
    ANALYST_VOICE_ADR,
    COMPARISON_SYSTEM_PROMPT,
    CONVERSATIONAL_SYSTEM_PROMPT,
    FOCUSED_SYSTEM_PROMPT,
    FOLLOWUP_SYSTEM_PROMPT,
    QUICK_FACT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_comparison_prompt,
    build_conversational_prompt,
    build_focused_prompt,
    build_followup_prompt,
    build_quick_fact_prompt,
    build_synthesis_prompt,
)
from langchain_core.messages import SystemMessage


def test_marker_is_non_empty_stable_token() -> None:
    """The marker must be a non-empty string. A naked empty token would make
    every ``in`` check below trivially pass and defeat the regression guard."""
    assert isinstance(ANALYST_VOICE_ADR, str)
    assert ANALYST_VOICE_ADR.strip()


def test_marker_present_in_thesis_system_prompt() -> None:
    assert ANALYST_VOICE_ADR in SYSTEM_PROMPT


def test_marker_present_in_quick_fact_system_prompt() -> None:
    assert ANALYST_VOICE_ADR in QUICK_FACT_SYSTEM_PROMPT


def test_marker_present_in_comparison_system_prompt() -> None:
    assert ANALYST_VOICE_ADR in COMPARISON_SYSTEM_PROMPT


def test_marker_present_in_conversational_system_prompt() -> None:
    assert ANALYST_VOICE_ADR in CONVERSATIONAL_SYSTEM_PROMPT


def test_marker_present_in_focused_system_prompt() -> None:
    assert ANALYST_VOICE_ADR in FOCUSED_SYSTEM_PROMPT


def test_marker_present_in_followup_system_prompt() -> None:
    """Followup reuses the QuickFactAnswer schema (QNT-209) but its own
    system prompt -- carrying the marker keeps voice consistent across the
    thread."""
    assert ANALYST_VOICE_ADR in FOLLOWUP_SYSTEM_PROMPT


def _system_message(messages: list) -> str:
    """Extract the SystemMessage content from a build_*_prompt result."""
    for m in messages:
        if isinstance(m, SystemMessage):
            return str(m.content)
    raise AssertionError("no SystemMessage in messages")


def test_marker_present_in_rendered_thesis_prompt() -> None:
    messages = build_synthesis_prompt("NVDA", "thesis?", {"technical": "x"})
    assert ANALYST_VOICE_ADR in _system_message(messages)


def test_marker_present_in_rendered_quick_fact_prompt() -> None:
    messages = build_quick_fact_prompt("AAPL", "rsi?", {"technical": "x"})
    assert ANALYST_VOICE_ADR in _system_message(messages)


def test_marker_present_in_rendered_comparison_prompt() -> None:
    messages = build_comparison_prompt(
        ["NVDA", "AAPL"], "compare", {"NVDA": {"technical": "x"}, "AAPL": {"technical": "y"}}
    )
    assert ANALYST_VOICE_ADR in _system_message(messages)


def test_marker_present_in_rendered_conversational_prompt() -> None:
    messages = build_conversational_prompt("hi")
    assert ANALYST_VOICE_ADR in _system_message(messages)


def test_marker_present_in_rendered_focused_prompt() -> None:
    messages = build_focused_prompt("technical", "TSLA", "trend?", {"technical": "x"})
    assert ANALYST_VOICE_ADR in _system_message(messages)


def test_marker_present_in_rendered_followup_prompt() -> None:
    messages = build_followup_prompt("NVDA", "why?", {"technical": "x"}, prior_thesis=None)
    assert ANALYST_VOICE_ADR in _system_message(messages)
