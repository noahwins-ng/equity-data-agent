"""QNT-351: smoke the deepseek-first pin for (1) prefix caching and (2) strict json_schema.

Two receipts the QNT-351 provider-pin reorder needs, both through the LOCAL LiteLLM
proxy (``equity-agent/default``) -- the prod path, where ``drop_params`` +
structured-output normalization change what actually reaches the provider:

  AC3 -- cache smoke. Two back-to-back synthesize-shaped calls (the real
  ``build_synthesis_prompt`` -> ``Thesis`` shape ``synthesize`` runs) pinned to
  first-party ``deepseek`` -- the only OpenRouter endpoint reporting
  supports_implicit_caching=TRUE. The second call must report cached prompt
  tokens > 0: proof the sticky first provider warms and reuses the large stable
  prefix (SYSTEM_PROMPT + force-injected reports) that stayed cold under the old
  Novita-first pin (prod 07-04..08: 27/33 structured calls cached_tokens=0).

  AC4 -- strict json_schema smoke. Repeated clarify- and conversational-shaped
  calls into ``ConversationalAnswer`` forced through ``method="json_schema"`` with
  ``strict=True`` (the mode QNT-258 abandoned for ``function_calling`` when
  DeepSeek returned bare prose). Counts prose escapes / json_invalid across N
  attempts. Zero escapes across the run = strict enforcement holds on the pinned
  provider, which would justify reverting the ``function_calling`` workaround as a
  follow-up (NOT done here -- see QNT-351 out-of-scope). Any escape is recorded so
  the result stands either way.

Opt-in dev script -- real paid OpenRouter calls (~a few cents). NOT in the CI gate
(pytest ``testpaths=["tests"]``). Requires the LiteLLM proxy running
(``make dev-litellm``), which holds the provider keys.

Usage:
    make dev-litellm                                              # terminal 1
    uv run python scripts/smoke_deepseek_cache_schema.py          # terminal 2
    uv run python scripts/smoke_deepseek_cache_schema.py --attempts 5

Exit 0 if the second cache call shows cached_tokens > 0 AND the strict-schema run
has zero prose escapes; exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from typing import Any

from agent.conversational import ConversationalAnswer
from agent.graph import Thesis, build_synthesis_prompt
from agent.prompts import build_clarify_prompt, build_conversational_prompt
from langchain_openai import ChatOpenAI
from shared.config import settings
from shared.tickers import TICKERS

# First provider under the QNT-351 pin: the only implicit-caching OpenRouter
# endpoint for deepseek-v4-flash. Pinned explicitly here so the cache receipt is
# unambiguously "via the new first provider".
FIRST_PROVIDER = "deepseek"
ALIAS = "equity-agent/default"

# Real prod synthesize shape (mirrors scripts/smoke_openrouter_providers.py): a
# fixed four-report bundle -> build_synthesis_prompt -> Thesis. The large stable
# prefix is what the provider prefix-cache is expected to warm on.
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

# Clarify- and conversational-shaped prompts -- the two ConversationalAnswer paths
# that carry method="function_calling" in prod (clarify.py / synthesize.py). Both
# are re-run here under strict json_schema to measure whether enforcement holds.
_CLARIFY_PROMPT = build_clarify_prompt(
    ambiguity_kind="needs_ticker",
    question="how's it doing?",
    ticker="",
    tickers=TICKERS,
)
_CONVERSATIONAL_PROMPT = build_conversational_prompt("what can you help me with?", history=[])


def _require_proxy() -> None:
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
    """ChatOpenAI on the alias, pinned to first-party deepseek, reasoning off.

    ``extra_body.provider`` overrides the config's ordered set; re-passing
    ``reasoning`` guards against LiteLLM replacing (not merging) the config
    ``extra_body`` block, so the arm stays reasoning-off either way.
    """
    return ChatOpenAI(
        model=ALIAS,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm-proxy",  # pyright: ignore[reportArgumentType]  # proxy holds real keys
        temperature=0.0,
        timeout=90,
        extra_body={
            "provider": {
                "only": [FIRST_PROVIDER],
                "allow_fallbacks": False,
                "data_collection": "allow",
                "require_parameters": True,
            },
            "reasoning": {"enabled": False},
        },
    )


def _cached_tokens(raw: Any) -> int:
    """Cached prompt tokens from a LangChain AIMessage, however the provider surfaced them.

    Preferred path: ``usage_metadata.input_token_details.cache_read`` (LangChain's
    normalized field). Fallback: the raw OpenAI ``prompt_tokens_details.cached_tokens``
    on ``response_metadata.token_usage``. Returns 0 when neither is present.
    """
    usage = getattr(raw, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read")
    if isinstance(cache_read, int) and cache_read > 0:
        return cache_read
    meta = getattr(raw, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or {}
    ptd = token_usage.get("prompt_tokens_details") or {}
    cached = ptd.get("cached_tokens")
    return int(cached) if isinstance(cached, int) else 0


def _run_cache_smoke(llm: ChatOpenAI) -> bool:
    """AC3: two back-to-back synthesize calls; the 2nd must show cached_tokens > 0."""
    print(f"[AC3] cache smoke -- 2x synthesize(Thesis) back-to-back via {FIRST_PROVIDER}")
    structured = llm.with_structured_output(Thesis, include_raw=True)
    cached_seq: list[int] = []
    for i in (1, 2):
        result = structured.invoke(_THESIS_PROMPT)
        raw = result.get("raw")
        cached = _cached_tokens(raw)
        cached_seq.append(cached)
        prompt_tokens = (getattr(raw, "usage_metadata", None) or {}).get("input_tokens", "?")
        print(f"    call {i}: prompt_tokens={prompt_tokens}  cached_tokens={cached}")
    ok = cached_seq[1] > 0
    print(f"    -> {'PASS' if ok else 'FAIL'} (call 2 cached_tokens={cached_seq[1]}, need > 0)\n")
    return ok


def _run_schema_smoke(llm: ChatOpenAI, attempts: int) -> bool:
    """AC4: repeated clarify/conversational calls under strict json_schema; count escapes."""
    print(f"[AC4] strict json_schema smoke -- ConversationalAnswer, strict=True, {attempts}x each")
    structured = llm.with_structured_output(
        ConversationalAnswer, method="json_schema", strict=True, include_raw=True
    )
    escapes = 0
    total = 0
    for label, prompt in (("clarify", _CLARIFY_PROMPT), ("conversational", _CONVERSATIONAL_PROMPT)):
        clean = 0
        last_detail = ""
        for _ in range(attempts):
            total += 1
            try:
                result = structured.invoke(prompt)
            except Exception as exc:  # noqa: BLE001 -- a prose escape / json_invalid is the finding
                escapes += 1
                last_detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                continue
            parsed = result.get("parsed")
            parse_error = result.get("parsing_error")
            if parse_error is not None or not isinstance(parsed, ConversationalAnswer):
                escapes += 1
                last_detail = f"escape: parsing_error={parse_error}"
            else:
                clean += 1
        detail = f"  | last: {last_detail}" if last_detail else ""
        print(f"    {label:14} {clean}/{attempts} clean{detail}")
    ok = escapes == 0
    print(f"    -> {'PASS' if ok else 'FAIL'} ({escapes}/{total} prose escapes / json_invalid)\n")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--attempts", type=int, default=3, help="Strict-schema attempts per shape (default 3)."
    )
    args = parser.parse_args()
    attempts = max(1, args.attempts)

    _require_proxy()
    llm = _build_llm()

    cache_ok = _run_cache_smoke(llm)
    schema_ok = _run_schema_smoke(llm, attempts)

    if cache_ok and schema_ok:
        print("Both receipts pass: prefix cache warms on the deepseek-first pin, and strict")
        print("json_schema holds. Note the function_calling revert as a QNT-351 follow-up.")
        return 0
    if not cache_ok:
        print("AC3 FAIL: no cached tokens on call 2 -- the sticky prefix did not warm.")
    if not schema_ok:
        print("AC4 result: strict json_schema escaped to prose -- the function_calling")
        print("workaround stays; record the escape rate in the ADR-027 amendment.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
