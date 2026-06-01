"""LLM-as-judge scoring for multi-turn dialogue quality (QNT-214).

This judge is deliberately separate from the agent-under-test model. The
structured golden-set judge historically reuses ``get_llm()``; that is fine
as a soft prompt-regression signal, but dialogue quality is explicitly meant
to compare topology/model changes. Reusing the generator as its own judge
would invite self-preference bias.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from shared.config import settings

logger = logging.getLogger(__name__)

JUDGE_MODEL_ALIAS = "equity-agent/bench-cerebras-gptoss120b"
JUDGE_RESOLVED_MODEL = "cerebras/gpt-oss-120b"
AGENT_UNDER_TEST_ALIAS = "equity-agent/default"
AGENT_UNDER_TEST_RESOLVED_MODEL = "groq/llama-3.3-70b-versatile"


class DialogueAxisScore(BaseModel):
    """One bounded dialogue score plus a compact debugging rationale."""

    score: float = Field(ge=0.0, le=1.0)
    rationale: str


class DialogueJudgeScore(BaseModel):
    """Five-axis dialogue scorecard, each axis 0.0-1.0."""

    analyst_likeness: DialogueAxisScore
    helpfulness: DialogueAxisScore
    non_hallucination: DialogueAxisScore
    exploration_quality: DialogueAxisScore
    voice_match: DialogueAxisScore

    @property
    def composite(self) -> float:
        """Simple average across all dialogue axes."""
        values = (
            self.analyst_likeness.score,
            self.helpfulness.score,
            self.non_hallucination.score,
            self.exploration_quality.score,
            self.voice_match.score,
        )
        return round(sum(values) / len(values), 4)


_RUBRIC_PROMPT = """You are evaluating an equity-analysis chat agent.
Return a structured score with five axes. Scores are floats from 0.0 to 1.0,
and each rationale must be exactly one concise sentence.

Important boundaries:
- Do not reward padding, apology-spam, generic throat-clearing, or false confidence.
- Reward a direct analyst voice: specific, grounded, plain-spoken, and willing to ask back.
- Penalize any fabricated numeric claim. The deterministic numeric checker result is provided;
  treat a failed deterministic check as a serious non_hallucination failure.
- Judge only dialogue quality. Do not perform arithmetic.

Fixture id:
{fixture_id}

Expected signals:
{expected_signals}

User/assistant transcript:
{transcript}

Final narrative shown to the user:
{narrative}

Structured payload summary:
{structured_payload}

Deterministic numeric-support result:
{numeric_support}

Score axes:
analyst_likeness — does the response sound like a competent senior US-equities analyst
continuing a real conversation, rather than filling a template?

helpfulness — does it answer the user with useful next-step context, not evasion?

non_hallucination — are numeric/factual claims grounded in the supplied reports and the
deterministic numeric-support result?

exploration_quality — does it ask back when the request is ambiguous and volunteer relevant
context when the request is under-specified?

voice_match — does it follow the ADR voice: direct, appropriately hedged, no padding,
no apology-spam, no sign-off, no repeating the user's question back?"""


def build_judge_llm(*, agent_model_alias: str = AGENT_UNDER_TEST_ALIAS) -> ChatOpenAI:
    """Return the dedicated dialogue judge LLM.

    The explicit alias keeps judge routing independent from the global eval
    model override, so benchmarking the agent does not accidentally benchmark
    the judge into self-scoring the same model.
    """
    if agent_model_alias == JUDGE_MODEL_ALIAS:
        raise ValueError("dialogue judge model must differ from agent-under-test model")
    return ChatOpenAI(
        model=JUDGE_MODEL_ALIAS,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]
        temperature=0.0,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        # QNT-218: explicit exponential backoff so a transient provider 429 retries
        # rather than dropping the fixture to a (contaminating) None score.
        max_retries=3,
    )


def score(
    *,
    fixture_id: str,
    transcript: str,
    narrative: str,
    structured_payload: str,
    expected_signals: tuple[str, ...],
    numeric_support: str,
    llm: Any | None = None,
    config: RunnableConfig | None = None,
) -> DialogueJudgeScore | None:
    """Return per-axis dialogue scores, or ``None`` if the judge fails."""
    base_llm = llm if llm is not None else build_judge_llm()
    judge_llm = base_llm.with_structured_output(DialogueJudgeScore)
    prompt = _RUBRIC_PROMPT.format(
        fixture_id=fixture_id,
        expected_signals=", ".join(expected_signals) or "(none)",
        transcript=transcript.strip() or "(empty)",
        narrative=narrative.strip() or "(no narrative)",
        structured_payload=structured_payload.strip() or "(no structured payload)",
        numeric_support=numeric_support.strip() or "(not checked)",
    )
    try:
        response = judge_llm.invoke(prompt, config=config)
    except Exception as exc:  # noqa: BLE001 -- judge failure must not crash the eval sweep
        logger.warning("dialogue-judge failed for %s: %s", fixture_id, exc)
        return None
    if isinstance(response, DialogueJudgeScore):
        return response
    if isinstance(response, dict):
        parsed = response.get("parsed")
        if isinstance(parsed, DialogueJudgeScore):
            return parsed
    logger.warning(
        "dialogue-judge returned unexpected shape for %s: %r", fixture_id, type(response)
    )
    return None


__all__ = [
    "AGENT_UNDER_TEST_ALIAS",
    "AGENT_UNDER_TEST_RESOLVED_MODEL",
    "DialogueAxisScore",
    "DialogueJudgeScore",
    "JUDGE_MODEL_ALIAS",
    "JUDGE_RESOLVED_MODEL",
    "build_judge_llm",
    "score",
]
