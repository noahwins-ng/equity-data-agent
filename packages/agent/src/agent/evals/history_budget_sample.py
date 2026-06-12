"""Deterministic sample A/B for the QNT-232 #13 per-intent history budget.

The prod-window baseline (``agent.evals.langfuse_baseline``) answers "did
synthesize input tokens drop across real traffic?" but is noisy: a thesis turn's
input is dominated by the report bundle, and only deep-thread turns carry enough
history for the trim to bite, so the window average depends on the traffic mix.

This tool isolates the lever instead. It holds the reports and the thread fixed
and varies ONLY the history budget, so the input-token delta is purely the
history-trim effect. Input tokens are a deterministic function of the assembled
prompt, so a single call per arm is exact -- no averaging, no waiting for prod
traffic.

It assembles the synthesize prompt exactly as ``synthesize_node`` does
(``build_synthesis_prompt`` over ``_history_before_current(..., max_turns=)``)
under two budgets:

* **pre-change** -- the uniform ``HISTORY_TURN_LIMIT`` every intent used to get.
* **post-change** -- ``_history_budget("thesis")`` (the trimmed fresh budget).

and reads the provider's own ``usage_metadata`` input-token count for each.

Run (requires the LiteLLM proxy + report API up, like ``make dev-*``)::

    uv run python -m agent.evals.history_budget_sample
    uv run python -m agent.evals.history_budget_sample --ticker AAPL --turns 10

The delta scales with how heavy the trimmed turns are: light prior turns recover
little, full prior thesis surfaces recover more. The synthetic history here uses
representative-weight assistant surfaces; pass real captured transcripts via the
``measure_synthesize_input_tokens`` helper for a traffic-faithful figure.
"""

from __future__ import annotations

import argparse
import sys

from agent.graph import _FRESH_ANALYTICAL_HISTORY_TURNS, _history_before_current
from agent.llm import get_llm
from agent.prompts import HISTORY_TURN_LIMIT, ConversationMessage, build_synthesis_prompt
from agent.tools import default_report_tools

# Representative-weight assistant surface (mirrors what `_assistant_surface`
# stores for a thesis turn: a sentence or two of narrative + the payload ref).
_TOPICS = (
    "margins",
    "RSI",
    "guidance",
    "data center demand",
    "valuation",
    "competition",
    "the buyback",
    "China exposure",
    "gross margin trend",
    "free cash flow",
)


def _synthetic_history(ticker: str, turns: int) -> list[ConversationMessage]:
    """A deep thread of ``turns`` prior user/assistant pairs for ``ticker``."""
    history: list[ConversationMessage] = []
    for i in range(turns):
        topic = _TOPICS[i % len(_TOPICS)]
        history.append({"role": "user", "content": f"What about {ticker}'s {topic}?"})
        history.append(
            {
                "role": "assistant",
                "content": (
                    f"On {ticker}'s {topic}, the read leans constructive given the "
                    f"supplied reports, though the picture is mixed once you weigh the "
                    f"counter-signals. Structured payload: thesis verdict=Neutral"
                ),
            }
        )
    return history


def measure_synthesize_input_tokens(
    *,
    ticker: str,
    question: str,
    reports: dict[str, str],
    history: list[ConversationMessage],
    max_turns: int,
    llm: object,
) -> tuple[int, int]:
    """Return (provider input tokens, history messages) for one budget arm.

    Mirrors ``synthesize_node``: the current user turn is appended to the
    transcript (as ``classify_node`` does) and then stripped by
    ``_history_before_current``, which also applies ``max_turns``. ``llm`` is
    injected so tests can drive this with a stub and no network.
    """
    messages: list[ConversationMessage] = [*history, {"role": "user", "content": question}]
    hist = _history_before_current(messages, question, max_turns=max_turns)
    prompt = build_synthesis_prompt(ticker, question, reports, history=hist)
    response = llm.invoke(prompt)  # type: ignore[attr-defined]
    usage = getattr(response, "usage_metadata", None) or {}
    return int(usage.get("input_tokens", 0)), len(hist)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.evals.history_budget_sample")
    parser.add_argument(
        "--ticker", default="NVDA", help="Ticker for the report bundle (default: NVDA)"
    )
    parser.add_argument(
        "--turns", type=int, default=8, help="Prior turns in the synthetic thread (default: 8)"
    )
    args = parser.parse_args(argv)

    ticker = args.ticker.upper()
    tools = default_report_tools()
    try:
        reports = {
            name: tools[name](ticker) for name in ("company", "technical", "fundamental", "news")
        }
    except Exception as exc:  # noqa: BLE001 -- report API unreachable is the common failure
        print(f"Could not fetch reports for {ticker} (is the API up?): {exc}", file=sys.stderr)
        return 1

    question = f"Give me a full thesis on {ticker}."
    history = _synthetic_history(ticker, args.turns)
    llm = get_llm()  # default 70b via the proxy

    pre_budget = HISTORY_TURN_LIMIT
    post_budget = _FRESH_ANALYTICAL_HISTORY_TURNS
    pre_tok, pre_h = measure_synthesize_input_tokens(
        ticker=ticker,
        question=question,
        reports=reports,
        history=history,
        max_turns=pre_budget,
        llm=llm,
    )
    post_tok, post_h = measure_synthesize_input_tokens(
        ticker=ticker,
        question=question,
        reports=reports,
        history=history,
        max_turns=post_budget,
        llm=llm,
    )

    print(f"Sample: {ticker} thesis on a {args.turns}-turn thread")
    print(f"  report sizes (chars): { {k: len(v) for k, v in reports.items()} }")
    print(f"  pre-change  (budget={pre_budget:>2}): msgs={pre_h:>2}  input tokens={pre_tok}")
    print(f"  post-change (budget={post_budget:>2}): msgs={post_h:>2}  input tokens={post_tok}")
    if pre_tok:
        delta = pre_tok - post_tok
        pct = 100 * delta / pre_tok
        print(f"  delta: {delta} fewer ({pct:.1f}% reduction) -- pure history-trim effect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
