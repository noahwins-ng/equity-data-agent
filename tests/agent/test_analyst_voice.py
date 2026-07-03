"""Analyst-voice guardrail fixtures (QNT-303).

Pins the two rules the 2026-07 live-sample assessment SUPPORTED
(docs/assessments/agent-analyst-voice-2026-07.md):

* **D-6 filler tripwire** -- ``agent.analyst_voice.find_filler`` flags the stock
  padding a senior desk never writes, and the dialogue eval's
  ``_apply_deterministic_filler_gate`` caps ``voice_match`` at 0 when it fires.
  The deterministic, no-regret twin of the QNT-156 conversational digit check.
* **D-1 falsifier** -- the narrate prompt teaches "what would change this view"
  for the forward-looking intents (thesis/comparison/followup/news) and stays
  out of the terse single-lens / lookup shapes.
"""

from __future__ import annotations

import pytest
from agent.analyst_voice import find_filler, has_filler
from agent.evals.dialogue_eval import _apply_deterministic_filler_gate
from agent.evals.dialogue_judge import DialogueAxisScore, DialogueJudgeScore
from agent.prompts import NARRATE_FALSIFIER_RULE, build_narrate_prompt
from langchain_core.messages import SystemMessage

# ─── D-6: filler detector ──────────────────────────────────────────────────

FILLER_SAMPLES = [
    "It's important to note that NVDA trades at a premium.",
    "It is important to note the RSI is neutral.",
    "The setup is mixed; it's worth noting the weak momentum.",
    "RSI 64.7, indicating potential for further growth.",
    "Overall, the read is constructive.",
    "In conclusion, AAPL screens rich.",
    "In summary, the trend is up.",
    "On balance, the read here is cautious.",
    "The call is neutral. That being said, momentum is cooling.",
]

CLEAN_SAMPLES = [
    "**Constructive, but priced for it.** The AAPL setup leans on strong "
    "Services growth (source: fundamental), tempered by a rich multiple.",
    # 'overall' used substantively, not as a leading throat-clear, must pass.
    "The overall signal is bullish, with the daily trend up (source: technical).",
    # 'noting' as a real verb, not the padding phrase.
    "The report is noting a cooling RSI (source: technical).",
    "This turns cautious if it loses the SMA-50 the report prints (source: technical).",
    # 'On balance sheet ...' at line start is about the balance sheet, not the
    # 'On balance,' filler hedge -- must not trip the gate.
    "On balance sheet strength, MSFT looks well-capitalized (source: fundamental).",
    # A word ending in 'it' before 'is/'s important to note' must NOT false-fire
    # the "it's important to note" pattern (needs the leading \b).
    "Profit's important to note here, but margins compressed (source: fundamental).",
    "The deficit is important to note in this read, but the trend holds (source: fundamental).",
]


@pytest.mark.parametrize("text", FILLER_SAMPLES)
def test_find_filler_flags_padding(text: str) -> None:
    assert has_filler(text), f"expected filler detected in: {text!r}"
    assert find_filler(text), "find_filler must return the matched phrase(s)"


@pytest.mark.parametrize("text", CLEAN_SAMPLES)
def test_find_filler_passes_clean_analyst_prose(text: str) -> None:
    assert not has_filler(text), f"false positive on clean prose: {find_filler(text)!r}"


def test_find_filler_empty_is_clean() -> None:
    assert find_filler("") == []
    assert not has_filler("")


# ─── D-6: eval-path gate ───────────────────────────────────────────────────


def _full_score(value: float = 1.0) -> DialogueJudgeScore:
    axis = lambda: DialogueAxisScore(score=value, rationale="ok")  # noqa: E731
    return DialogueJudgeScore(
        analyst_likeness=axis(),
        helpfulness=axis(),
        non_hallucination=axis(),
        exploration_quality=axis(),
        voice_match=axis(),
    )


def test_filler_gate_zeroes_voice_match_on_filler() -> None:
    """A filler phrase caps voice_match at 0 regardless of the judge score."""
    score = _full_score(1.0)
    gated = _apply_deterministic_filler_gate(score, "Overall, the read is constructive here.")
    assert gated is not None
    assert gated.voice_match.score == 0.0
    assert "filler" in gated.voice_match.rationale.lower()
    # Other axes are untouched -- the gate is voice-only.
    assert gated.analyst_likeness.score == 1.0


def test_filler_gate_noop_on_clean_narrative() -> None:
    score = _full_score(0.8)
    gated = _apply_deterministic_filler_gate(
        score, "**Constructive, but priced for it.** Services carries it."
    )
    assert gated is not None
    assert gated.voice_match.score == 0.8


def test_filler_gate_handles_none_score() -> None:
    assert _apply_deterministic_filler_gate(None, "Overall, junk.") is None


# ─── D-1: falsifier prompt rule ────────────────────────────────────────────


def _system_text(intent: str) -> str:
    messages = build_narrate_prompt(
        intent=intent,
        ticker="NVDA",
        question="q?",
        payload_markdown="## Verdict\nNeutral\n",
    )
    for m in messages:
        if isinstance(m, SystemMessage):
            return str(m.content)
    raise AssertionError("no SystemMessage")


@pytest.mark.parametrize("intent", ["thesis", "comparison"])
def test_falsifier_rule_present_for_label_bearing_intents(intent: str) -> None:
    """D-1: thesis/comparison carry printed labels, so they teach the falsifier."""
    text = _system_text(intent)
    assert "what would change your view" in text
    assert NARRATE_FALSIFIER_RULE.strip()[:40] in text


@pytest.mark.parametrize(
    "intent",
    ["news", "followup", "quick_fact", "technical", "fundamental", "conversational"],
)
def test_falsifier_rule_absent_where_no_printed_threshold(intent: str) -> None:
    """News/followup are narrative-only (no printed regime label to anchor on),
    and the terse single-lens/lookup shapes stay terse -- no forced falsifier.
    An earlier draft that included news fabricated a "200-day moving average"
    that no report stated (2026-07 clean-window regression)."""
    assert "what would change your view" not in _system_text(intent)


def test_falsifier_rule_is_adr003_safe_no_invented_thresholds() -> None:
    """The rule must reuse a printed label/band, never invent a number."""
    assert "ALREADY prints" in NARRATE_FALSIFIER_RULE
    assert "never reach for" in NARRATE_FALSIFIER_RULE  # forbids the 200-day cliche
    # No multi-digit literal that could bleed into a narration.
    import re

    assert re.findall(r"(?<!\w)\d{2,}(?!\w)", NARRATE_FALSIFIER_RULE) == []
