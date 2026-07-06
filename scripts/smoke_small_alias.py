"""QNT-326 (AC3): liveness smoke for the equity-agent/small alias.

Companion to ``scripts/smoke_openrouter_providers.py`` (QNT-319, which smokes
the pinned OpenRouter providers behind ``equity-agent/default``). This covers the
OTHER live chat-path alias the QNT-319 smoke does not touch: ``equity-agent/small``
(Groq ``gpt-oss-20b``), which classify / plan run on EVERY turn.

Why this exists: the QNT-258 / QNT-317 decommission arc re-anchored only the
synthesize chain. ``equity-agent/small`` still points at ``groq/openai/gpt-oss-20b``
and there is no confirmation it survives the Groq free-tier sunset. If Groq kills
it, the agent does NOT go down -- it silently DEGRADES: classify's BLE001 defaults
every turn to intent=thesis with classifier_source="fallback", keyword floors
become the only RAG signal, and the thesis planner over-fetches all tools. Nothing
pages. This smoke turns that silent drift into a red run: one structured-output
call (the array-bounded ``ThesisPlan``, the exact schema ``plan.py`` runs on this
alias in prod) must succeed. A Groq sunset shows up here as a failed smoke, not as
a ``classifier_source="fallback"`` drift no one reads.

Routes through the LOCAL LiteLLM proxy (which holds the Groq key + applies
``drop_params`` / structured-output normalization), exactly like the agent -- not
direct to Groq. Uses ``with_structured_output(ThesisPlan)`` with NO ``method`` arg,
matching the agent's ``_structured_call``.

Opt-in dev script -- it makes a real Groq call and is NOT collected by the default
CI gate (pytest ``testpaths=["tests"]``). Requires the proxy running
(``make dev-litellm``).

Usage:
    make dev-litellm                                    # terminal 1
    uv run python scripts/smoke_small_alias.py          # terminal 2
    uv run python scripts/smoke_small_alias.py --attempts 5

Exit 0 if the alias serves the structured call in at least one attempt; exit 1 if
it cannot in any attempt (the sunset tripwire); exit 2 if the proxy is unreachable.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

from agent.llm import SMALL_NODE_ALIAS
from agent.structured import ThesisPlan, _build_thesis_plan_prompt
from langchain_openai import ChatOpenAI
from shared.config import settings

# The array-bounded plan schema plan.py runs on equity-agent/small in prod
# (tools = 2-4 report names). A representative single-ticker thesis-plan prompt.
_AVAILABLE_REPORTS = ["company", "fundamental", "technical", "news"]
_PLAN_PROMPT = _build_thesis_plan_prompt(
    "AAPL", "Give me a full investment thesis on Apple.", _AVAILABLE_REPORTS
)

# The small path has the same per-attempt structured-output flake the QNT-319
# smoke documents; a single shot would pass/fail by luck, so N attempts turn it
# into a stable capability verdict (parses at least once) plus a flake ratio.
ATTEMPTS = 3


def _require_proxy() -> None:
    """Exit with a clear message if the LiteLLM proxy is not reachable."""
    url = f"{settings.LITELLM_BASE_URL}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 -- localhost proxy
            if resp.status == 200:
                return
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    print(
        f"LiteLLM proxy not reachable at {settings.LITELLM_BASE_URL}. "
        "Start it first: make dev-litellm",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _build_llm() -> ChatOpenAI:
    """A ChatOpenAI on the small alias, mirroring get_llm(SMALL_NODE_ALIAS)."""
    return ChatOpenAI(
        model=SMALL_NODE_ALIAS,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy holds real keys
        temperature=0.0,
        timeout=60,
    )


def _attempt(llm: ChatOpenAI) -> tuple[bool, str]:
    """One structured ThesisPlan attempt. Returns (clean, evidence)."""
    structured = llm.with_structured_output(ThesisPlan, include_raw=True)
    try:
        result = structured.invoke(_PLAN_PROMPT)
    except Exception as exc:  # noqa: BLE001 -- an alias that cannot serve the schema is the finding
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")
    if parse_error is not None or not isinstance(parsed, ThesisPlan):
        return False, f"schema parse failed: {parse_error}"
    return True, f"tools={parsed.tools}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--attempts", type=int, default=ATTEMPTS, help=f"Attempts (default {ATTEMPTS})."
    )
    args = parser.parse_args()
    attempts = max(1, args.attempts)

    _require_proxy()

    print(f"Smoking {SMALL_NODE_ALIAS} (structured ThesisPlan), {attempts} attempts\n")
    results = [_attempt(_build_llm()) for _ in range(attempts)]
    oks = sum(ok for ok, _ in results)
    sample = next((ev for ok, ev in results if ok), results[-1][1])
    print(f"{oks}/{attempts} clean  {sample}")
    if oks < attempts:
        last_fail = next((ev for ok, ev in reversed(results) if not ok), "")
        if last_fail:
            print(f"  flake/fail: {last_fail}")

    print()
    if oks >= 1:
        print(f"{SMALL_NODE_ALIAS} serves the structured call -- alias is live.")
        return 0
    print(
        f"{SMALL_NODE_ALIAS} could NOT serve the structured call in any attempt. If "
        "Groq has sunset gpt-oss-20b, re-anchor the small tier in litellm_config.yaml "
        "before classify/plan silently degrade to classifier_source='fallback'."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
