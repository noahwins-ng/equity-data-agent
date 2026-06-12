"""Offline pin for the QNT-232 #13 history-budget sample tool.

The tool itself makes a live LLM call for the authoritative provider token
count, but the invariant it demonstrates -- a fresh-intent budget assembles a
SMALLER synthesize prompt than the old uniform HISTORY_TURN_LIMIT on a deep
thread -- is deterministic and testable with a stub LLM (no network). The stub
reports input_tokens proportional to the assembled prompt's content length, so
fewer history turns -> fewer tokens.
"""

from __future__ import annotations

from agent.evals.history_budget_sample import _synthetic_history, measure_synthesize_input_tokens
from agent.prompts import HISTORY_TURN_LIMIT


class _CharCountLLM:
    """Returns usage_metadata.input_tokens = total chars across the prompt."""

    def invoke(self, prompt: list) -> object:
        total = sum(len(str(getattr(m, "content", m))) for m in prompt)

        class _Resp:
            usage_metadata = {"input_tokens": total}

        return _Resp()


def test_fresh_budget_assembles_smaller_synthesize_prompt() -> None:
    reports = {
        "company": "## company\nNVDA business context\n" * 5,
        "technical": "## technical\nRSI 55 neutral\n" * 5,
        "fundamental": "## fundamental\nP/E 40 Premium\n" * 5,
        "news": "## news\n- headline\n" * 5,
    }
    history = _synthetic_history("NVDA", turns=8)
    question = "Give me a full thesis on NVDA."
    llm = _CharCountLLM()

    pre_tok, pre_h = measure_synthesize_input_tokens(
        ticker="NVDA",
        question=question,
        reports=reports,
        history=history,
        max_turns=HISTORY_TURN_LIMIT,
        llm=llm,
    )
    post_tok, post_h = measure_synthesize_input_tokens(
        ticker="NVDA", question=question, reports=reports, history=history, max_turns=3, llm=llm
    )

    # Deep thread (8 turns = 16 msgs) keeps all 16 under the old limit; the fresh
    # budget trims to a few. Fewer history messages -> a strictly smaller prompt.
    assert pre_h > post_h, (
        f"fresh budget must keep fewer history msgs ({post_h}) than old ({pre_h})"
    )
    assert post_tok < pre_tok, f"fresh-budget prompt must be smaller ({post_tok} vs {pre_tok})"
    # Reports + system prompt are identical across arms, so the whole delta is history.
    assert pre_tok - post_tok > 0
