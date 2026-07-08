"""QNT-319: smoke each pinned OpenRouter provider on the reasoning-off structured path.

Hardens QNT-318 / ADR-027 -- the ordered top-6 OpenRouter provider pin on
``equity-agent/default`` (order novita, deepinfra, gmicloud, deepseek, alibaba,
baidu; ``allow_fallbacks: false``) that keeps prefix caching sticky. QNT-318 only
exercised the three lead providers (novita/deepinfra/gmicloud); the three
deep-fallback providers (deepseek/alibaba/baidu) were never verified on the
reasoning-off + forced structured-output path -- the exact axis that produced the
QNT-258 "DeepSeek prose json_invalid" regression, where a provider returned bare
prose instead of the JSON envelope. If the three lead providers are ever all
unavailable and routing lands on baidu/alibaba, a schema-format regression could
surface silently on a rarely-hit path with no coverage. graph.py ``_structured_call``
fail-closes a malformed payload to a deterministic answer, so the blast radius is
bounded -- but we would rather know each fallback works before we need it.

For each of the 6 pinned providers this pins it individually (``provider.only``)
and, with reasoning disabled, sends three arms (each run ``--attempts`` times, default
3, because the reasoning-off structured path has a per-attempt prose-flake -- see
ATTEMPTS below):

  1. synthesize(Thesis) -- the REAL production structured-output shape on this alias.
     ``synthesize`` calls ``_structured_call(Thesis, ...)`` with no ``llm=`` override,
     so it runs on ``equity-agent/default`` (these pinned providers). This is the
     schema a fallback-provider regression would actually corrupt (the QNT-258
     blast-radius shape). A clean attempt = finish_reason == "stop" AND a valid Thesis.
  2. structured(ThesisPlan) -- the array-bounded schema the ticket names
     (``tools`` = 2-4 report names). NOTE: in prod ThesisPlan runs on the Groq
     ``equity-agent/small`` alias (``plan.py`` passes ``llm=get_llm(SMALL_NODE_ALIAS)``),
     NOT on these OpenRouter providers -- so this arm is a cheap array-bounded
     *capability probe* of each provider, not the prod routing path. Kept because the
     ticket asks for it and it stresses the array constraint the richer Thesis lacks.
  3. narrate -- a free-text request; a clean attempt = finish_reason == "stop" AND
     non-empty content.

A provider PASSES an arm if it produces at least one clean attempt (proves it CAN
serve it reasoning-off); the k/N clean ratio surfaces the per-attempt flake.

Routes through the LOCAL LiteLLM proxy (``equity-agent/default``), NOT direct to
OpenRouter: the agent always talks to the proxy, and the proxy's ``drop_params`` +
structured-output normalization change what actually reaches the provider (measured:
novita returns a hard 400 on a direct json_schema request but prose through the
proxy). Pinning ``provider.only`` per provider in ``extra_body`` overrides the
config's ordered set so each provider is forced in turn. Each structured arm uses
``with_structured_output(schema)`` with NO ``method`` arg -- exactly what the agent's
``_structured_call`` passes for the Thesis/ThesisPlan schemas.

Opt-in dev script -- it makes real paid OpenRouter calls (6 providers x 3 arms x N
attempts; ~54 at the default N=3, ~a dime) and is NOT collected by the default CI
gate (pytest ``testpaths=["tests"]``). Requires the LiteLLM proxy running
(``make dev-litellm``), which holds the provider keys.

Usage:
    make dev-litellm                                            # terminal 1
    uv run python scripts/smoke_openrouter_providers.py         # terminal 2
    uv run python scripts/smoke_openrouter_providers.py --providers novita,baidu

Exit 0 if every exercised provider passes all arms; exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.graph import Thesis, build_synthesis_prompt
from agent.structured import ThesisPlan, _build_thesis_plan_prompt
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from shared.config import settings

# The ordered top-6 provider set pinned on equity-agent/default in
# litellm_config.yaml (QNT-318 / ADR-027; reordered QNT-351 to deepseek-first —
# the only implicit-caching endpoint). Kept in pin order for readability; this
# smoke pins each one INDIVIDUALLY, so order does not affect the result.
PINNED_PROVIDERS = ["deepseek", "baidu", "alibaba", "novita", "gmicloud", "deepinfra"]

# The proxy alias the agent uses; resolves to openrouter/deepseek/deepseek-v4-flash
# with reasoning disabled + the QNT-318 provider pin.
ALIAS = "equity-agent/default"

# The REAL production synthesize request on this alias: the four-report bundle fed to
# ``build_synthesis_prompt`` (the exact builder ``synthesize`` uses), forced into the
# ``Thesis`` schema via ``_structured_call(Thesis, ...)``. Static placeholder reports
# keep the request deterministic; content correctness is not under test -- provider
# schema-serving is.
_AVAILABLE_REPORTS = ["company", "fundamental", "technical", "news"]
_THESIS_REPORTS = {
    "company": "Apple Inc. designs consumer electronics; segments iPhone, Mac, Services. "
    "Key competitors: Samsung, Google.",
    "fundamental": "Revenue $391B TTM, gross margin 46%, P/E 31, services revenue +13% YoY.",
    "technical": "Price above the 50- and 200-day SMA; RSI 58; uptrend intact.",
    "news": "Services hit an all-time high; EU regulatory scrutiny; new product cycle expected.",
}
_THESIS_PROMPT = build_synthesis_prompt(
    "AAPL", "Give me a full investment thesis on Apple.", _THESIS_REPORTS, history=[]
)

# A representative single-ticker thesis PLAN prompt -- the array-bounded ThesisPlan
# capability probe (this schema runs on the Groq small alias in prod, not here).
_PLAN_PROMPT = _build_thesis_plan_prompt(
    "AAPL", "Give me a full investment thesis on Apple.", _AVAILABLE_REPORTS
)

# A minimal narrate-shaped free-text request: 1-4 sentence analyst-voice wrap, no
# structured output. Exercises the plain-completion arm reasoning-off.
_NARRATE_SYSTEM = (
    "You are an equity analyst. Answer in 1-4 concise, analyst-voice sentences. "
    "No preamble, no markdown headers."
)
_NARRATE_USER = (
    "In one short paragraph, summarize why an investor might watch Apple's services "
    "revenue growth as a signal for the overall thesis."
)


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


def _build_llm(provider: str) -> ChatOpenAI:
    """A ChatOpenAI on the proxy alias, pinned to a single provider, reasoning off.

    ``extra_body.provider`` overrides the config's ordered set; re-passing
    ``reasoning`` guards against LiteLLM replacing (rather than merging) the
    config's ``extra_body`` block, so the arm stays reasoning-off either way.
    """
    return ChatOpenAI(
        model=ALIAS,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy holds real keys
        temperature=0.0,
        timeout=90,
        extra_body={
            "provider": {
                "only": [provider],
                "allow_fallbacks": False,
                "data_collection": "allow",
            },
            "reasoning": {"enabled": False},
        },
    )


# Attempts per arm. The reasoning-off structured path has a known per-attempt
# prose-flake on OpenRouter (a provider intermittently returns prose instead of the
# JSON envelope -- the QNT-196 / QNT-258 phenomenon), so a single shot would pass or
# fail by luck. Running N attempts turns the result into a stable capability verdict
# (parses at least once) plus a visible flake ratio. Matches the spirit of the
# agent's own ``_structured_call`` retry ladder (``stop_after_attempt=2``), which is
# what bounds the flake in prod before a fail-closed deterministic answer.
ATTEMPTS = 3


@dataclass
class AttemptResult:
    ok: bool
    finish_reason: str
    detail: str


def _finish_reason(raw: object) -> str:
    meta = getattr(raw, "response_metadata", None) or {}
    return str(meta.get("finish_reason") or "?")


def _attempt_schema(
    llm: ChatOpenAI, schema: type, prompt: Any, detail_fn: Callable[[Any], str]
) -> AttemptResult:
    """One structured-output attempt with ``with_structured_output(schema)`` (no method).

    Mirrors the agent's ``_structured_call``, which passes no ``method`` -- so this is
    the json_schema envelope the Thesis/ThesisPlan paths actually use. A provider that
    does not reliably enforce the response_format returns prose instead of the JSON
    envelope (the QNT-258 failure mode), which surfaces here as the OpenAI parse
    raising before a value comes back, or as a set ``parsing_error``.
    """
    structured = llm.with_structured_output(schema, include_raw=True)
    try:
        result = structured.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 -- a provider that cannot serve the schema is a finding
        return AttemptResult(False, "?", f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
    raw = result.get("raw")
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")
    finish = _finish_reason(raw)
    if parse_error is not None or not isinstance(parsed, schema):
        return AttemptResult(False, finish, f"schema parse failed: {parse_error}")
    if finish != "stop":
        return AttemptResult(False, finish, f"finish_reason={finish!r} (parsed ok)")
    return AttemptResult(True, finish, detail_fn(parsed))


def _attempt_thesis(llm: ChatOpenAI) -> AttemptResult:
    """Real prod synthesize shape: Thesis schema + build_synthesis_prompt on this alias."""
    return _attempt_schema(llm, Thesis, _THESIS_PROMPT, lambda t: f"verdict={t.verdict}")


def _attempt_thesisplan(llm: ChatOpenAI) -> AttemptResult:
    """Array-bounded ThesisPlan capability probe (prod runs this on the Groq small alias)."""
    return _attempt_schema(llm, ThesisPlan, _PLAN_PROMPT, lambda p: f"tools={p.tools}")


def _attempt_narrate(llm: ChatOpenAI) -> AttemptResult:
    """One free-text narrate attempt: assert a clean completion with non-empty content."""
    try:
        msg = llm.invoke([SystemMessage(_NARRATE_SYSTEM), HumanMessage(_NARRATE_USER)])
    except Exception as exc:  # noqa: BLE001
        return AttemptResult(False, "?", f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
    finish = _finish_reason(msg)
    content = (msg.content if isinstance(msg.content, str) else str(msg.content)).strip()
    if not content:
        return AttemptResult(False, finish, "empty content")
    if finish != "stop":
        return AttemptResult(False, finish, f"finish_reason={finish!r} (content present)")
    return AttemptResult(True, finish, f"{len(content)} chars")


def _run_arm(
    attempt_fn: Callable[[ChatOpenAI], AttemptResult], llm: ChatOpenAI, attempts: int
) -> tuple[bool, str]:
    """Run one arm ``attempts`` times. Returns (capable, evidence).

    ``capable`` = at least one clean attempt (proves the provider CAN serve it under
    reasoning-off); the k/N ratio + a sample finish_reason + the last failure are the
    schema-parse evidence AC1 asks for.
    """
    results = [attempt_fn(llm) for _ in range(attempts)]
    oks = sum(r.ok for r in results)
    sample = next((r for r in results if r.ok), results[-1])
    evidence = f"{oks}/{attempts} clean  finish={sample.finish_reason:<10} {sample.detail}"
    if oks < attempts:
        last_fail = next((r for r in reversed(results) if not r.ok), None)
        if last_fail is not None:
            evidence += f"  | flake: {last_fail.detail}"
    return oks >= 1, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        default=",".join(PINNED_PROVIDERS),
        help="Comma-separated subset to exercise (default: all 6 pinned).",
    )
    parser.add_argument(
        "--attempts", type=int, default=ATTEMPTS, help=f"Attempts per arm (default {ATTEMPTS})."
    )
    args = parser.parse_args()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    attempts = max(1, args.attempts)

    unknown = [p for p in providers if p not in PINNED_PROVIDERS]
    if unknown:
        print(
            f"Unknown provider(s) {unknown}; pick from {PINNED_PROVIDERS}.",
            file=sys.stderr,
        )
        return 2

    _require_proxy()

    # (label, attempt_fn) per arm. synthesize(Thesis) is the real prod structured shape
    # on this alias; structured(ThesisPlan) is the array-bounded capability probe.
    arms: list[tuple[str, Callable[[ChatOpenAI], AttemptResult]]] = [
        ("synthesize(Thesis)  ", _attempt_thesis),
        ("structured(ThesisPlan)", _attempt_thesisplan),
        ("narrate             ", _attempt_narrate),
    ]

    print(
        f"Smoking {len(providers)} provider(s) via {ALIAS} (reasoning-off), "
        f"{attempts} attempts x {len(arms)} arms each\n"
    )
    all_capable = True
    for provider in providers:
        llm = _build_llm(provider)
        arm_results = [(label, *_run_arm(fn, llm, attempts)) for label, fn in arms]
        provider_ok = all(ok for _, ok, _ in arm_results)
        all_capable = all_capable and provider_ok
        print(f"[{'PASS' if provider_ok else 'FAIL'}] {provider}")
        for label, ok, evidence in arm_results:
            print(f"    {label} {'ok  ' if ok else 'FAIL'} {evidence}")

    print()
    if all_capable:
        print(
            f"All {len(providers)} provider(s) can serve every arm reasoning-off. "
            "Any k/N < N above is per-attempt flake -- bounded in prod by the "
            "_structured_call retry + deterministic fallback (QNT-196)."
        )
        return 0
    print("At least one provider could NOT serve an arm in any attempt -- per AC2, either")
    print("reorder it out of the reachable set in litellm_config.yaml, or record the")
    print("finding in the ADR-027 Watch note.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
