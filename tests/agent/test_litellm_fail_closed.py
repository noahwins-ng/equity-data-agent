"""LiteLLM fail-closed audit (QNT-161).

The chat agent is fronted by LiteLLM. If a Groq quota-exhaustion (429)
silently fell through to a paid Anthropic provider, every demo-limit
event would convert into an unbounded cost event — exactly the
"availability bomb" QNT-161 exists to prevent. ADR-017 commits the
project to free-tier providers only for the chat path; this test asserts
the LiteLLM config matches the policy.

Two layered checks:

1. **Static config audit** — parse ``litellm_config.yaml`` and assert
   every model alias the agent might call (``equity-agent/default``,
   ``equity-agent/gemini``, and the alias's fallbacks chain) routes to a
   free-tier provider (groq / gemini / google). A new alias accidentally
   pointed at ``anthropic/`` or ``openai/`` would fail this test.
2. **Runtime fail-closed contract** — when ``get_llm()`` raises a Groq
   quota error (RateLimitError-shaped exception), the synthesize node
   must surface a deterministic conversational redirect, NOT propagate.
   The whole graph guarantees this for every intent — see
   ``agent.graph.synthesize_node``'s ``_fallback`` calls. This test
   exercises the thesis path because it is the most-commonly hit one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent.conversational import ConversationalAnswer
from agent.graph import build_graph

# Paid / non-free-tier providers the chat path must never reach. Drawn
# from LiteLLM's provider list — extend if a new paid provider is added
# to LiteLLM's namespace.
_PAID_PROVIDER_PREFIXES = (
    "anthropic/",
    "openai/",  # not "groq/openai/..." — that's groq routing
    "azure/",
    "azure_ai/",
    "bedrock/",
    "vertex_ai/",
    "cohere/",
    "mistral/",
    "deepinfra/",
    "fireworks_ai/",
    "together_ai/",
    "perplexity/",
)

# Free-tier providers permitted on the chat path.
_FREE_PROVIDER_PREFIXES = ("groq/", "gemini/", "google/")


def _model_routes_to_free_tier(model: str) -> bool:
    """Return True iff ``model`` (a LiteLLM model string like
    ``groq/llama-3.3-70b-versatile``) starts with a permitted free-tier
    prefix AND not a paid prefix. Defensive ordering: paid check first
    so a hypothetical ``groq/openai/...`` (Groq's OpenAI-compatible
    namespace) is correctly identified as Groq."""
    for prefix in _FREE_PROVIDER_PREFIXES:
        if model.startswith(prefix):
            return True
    for prefix in _PAID_PROVIDER_PREFIXES:
        if model.startswith(prefix):
            return False
    return False


def _config_path() -> Path:
    """The repo's litellm_config.yaml lives at the repo root."""
    return Path(__file__).resolve().parents[2] / "litellm_config.yaml"


def _load_config() -> dict[str, Any]:
    return yaml.safe_load(_config_path().read_text())


def _models_by_alias(config: dict[str, Any]) -> dict[str, str]:
    """Return ``{alias_name: model_string}`` for every model in the config."""
    out: dict[str, str] = {}
    for entry in config.get("model_list", []):
        alias = entry["model_name"]
        params = entry.get("litellm_params", {})
        out[alias] = params.get("model", "")
    return out


# ─── Static config audit ────────────────────────────────────────────────────


def test_chat_default_alias_routes_to_free_tier_provider() -> None:
    """The agent's primary alias (``equity-agent/default``) must point at
    a free-tier provider. ADR-017 / QNT-161 forbid paid providers on the
    public chat path."""
    config = _load_config()
    models = _models_by_alias(config)
    default_model = models["equity-agent/default"]
    assert _model_routes_to_free_tier(default_model), (
        f"equity-agent/default -> {default_model} is not a permitted "
        f"free-tier provider; chat path would convert quota events into "
        f"cost events on fall-through. See ADR-017."
    )


def test_chat_default_fallback_chain_is_free_tier_only() -> None:
    """LiteLLM's fallback config maps an alias to a list of fallback
    aliases. Every fallback in the chain — for every chat-path alias —
    must also point at a free-tier provider. A future contributor adding
    an Anthropic alias as a fallback would trip this immediately."""
    config = _load_config()
    models = _models_by_alias(config)
    fallbacks_cfg = config.get("litellm_settings", {}).get("fallbacks", [])

    chat_aliases = {"equity-agent/default", "equity-agent/gemini"}
    failures: list[str] = []
    for fb_entry in fallbacks_cfg:
        for alias, fallback_aliases in fb_entry.items():
            if alias not in chat_aliases:
                continue
            for fb_alias in fallback_aliases:
                fb_model = models.get(fb_alias, "")
                if not _model_routes_to_free_tier(fb_model):
                    failures.append(
                        f"alias {alias} falls back to {fb_alias} -> {fb_model}, "
                        f"which is not a free-tier provider"
                    )
    assert not failures, "\n".join(failures)


def test_no_chat_alias_references_paid_provider_directly() -> None:
    """Belt-and-braces: every alias that COULD be reached from the chat
    path (default, gemini, and any alias they fall back to) must route
    to a free-tier provider. The bench-* aliases are explicitly excluded
    — they're invoked only by the QNT-129 bench harness, never by the
    chat path."""
    config = _load_config()
    models = _models_by_alias(config)
    fallbacks_cfg = config.get("litellm_settings", {}).get("fallbacks", [])

    # Build the reachable set: chat aliases + their fallbacks (transitive).
    reachable = {"equity-agent/default", "equity-agent/gemini"}
    for _ in range(3):  # depth bound — chains in this repo are <= 1 hop
        new_reachable = set(reachable)
        for fb_entry in fallbacks_cfg:
            for alias, fallback_aliases in fb_entry.items():
                if alias in reachable:
                    new_reachable.update(fallback_aliases)
        reachable = new_reachable

    bad: list[str] = []
    for alias in sorted(reachable):
        model = models.get(alias, "")
        if not _model_routes_to_free_tier(model):
            bad.append(f"{alias} -> {model}")
    assert not bad, (
        "Chat-path aliases must all route to free-tier providers (groq / "
        "gemini / google). Offending aliases:\n  " + "\n  ".join(bad)
    )


def test_litellm_config_has_no_anthropic_or_openai_string() -> None:
    """Defence-in-depth string scan: an active YAML reference to a paid
    provider — `anthropic/...` anywhere, or `openai/...` not under
    `groq/openai/...` — is forbidden on the chat path. Comments are
    allowed (a contributor referencing a provider in a `# why we don't use
    X` comment shouldn't trip CI), so the regex anchors on lines that
    don't start with `#`.

    Per-match position check: a previous version of this test used
    ``text.find("openai/")`` once and reused that index for every match,
    which silently suppressed `anthropic/` violations whenever ANY
    `groq/openai/` token existed elsewhere in the file. Now we use
    ``re.finditer`` so each match is judged at its OWN position.
    """
    text = _config_path().read_text()
    forbidden = re.compile(
        r"^(?P<lead>\s*[^#\n]*?)\b(?P<provider>anthropic|openai)/",
        re.MULTILINE,
    )
    real_violations: list[str] = []
    for match in forbidden.finditer(text):
        provider = match.group("provider")
        if provider == "openai":
            # Permit "groq/openai/..." — Groq hosts an OpenAI-compatible
            # namespace; the model is still a Groq inference. Inspect the
            # 5 chars immediately preceding THIS match's "openai/".
            start = match.start("provider")
            preceding = text[max(0, start - len("groq/")) : start]
            if preceding.endswith("groq/"):
                continue
        # Capture the surrounding line for a useful failure message.
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        real_violations.append(text[line_start:line_end].strip())
    assert not real_violations, (
        "litellm_config.yaml contains an active reference to a paid "
        f"provider (anthropic anywhere, or openai not under groq/): "
        f"{real_violations}"
    )


# ─── Runtime fail-closed contract ───────────────────────────────────────────


class _GroqQuotaError(Exception):
    """Stand-in for the LiteLLM-surfaced Groq 429 / quota error.

    LiteLLM raises ``litellm.RateLimitError`` for upstream 429s. The
    synthesize node catches ``Exception`` (BLE001 with a fallback
    rationale) so the exact class doesn't matter for the contract — we
    just need ANY exception that escapes the structured-LLM call to land
    in the ``_fallback`` path.
    """


def test_groq_quota_error_falls_back_to_conversational_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM raises a quota error, the graph must produce a
    ``ConversationalAnswer`` (the deterministic redirect), NOT propagate
    the exception. This proves the fail-closed contract holds end-to-end:
    no paid-provider fall-through, no client-visible stack trace, no
    silent retry.
    """
    from agent import graph as graph_module

    # QNT-181: stub the LLM directly. Thesis intent skips the plan-LLM
    # call so the only LLM call left is synthesize (via the structured
    # runnable). Force the structured invoke to raise the quota error so
    # the synthesize fallback path runs end-to-end.
    call_log: list[str] = []

    class _QuotaLLM:
        def invoke(self, _prompt: Any, **_kw: Any) -> Any:
            call_log.append("plan")
            stub = type("Stub", (), {"content": "technical"})()
            return stub

        def with_structured_output(self, _schema: object) -> Any:
            outer = self

            class _StructuredRunnable:
                def invoke(self, _prompt: Any, **_kw: Any) -> Any:
                    call_log.append("synthesize")
                    raise _GroqQuotaError("groq quota exceeded for the day")
                    _ = outer  # keep closure ref for clarity

            return _StructuredRunnable()

    monkeypatch.setattr(graph_module, "get_llm", lambda *_a, **_kw: _QuotaLLM())
    # Force intent=thesis so the synthesize path is the one that crashes.
    monkeypatch.setattr(graph_module, "classify_intent", lambda _q, **_: "thesis")

    # Minimal tool returning a non-empty report so synthesize doesn't
    # short-circuit on "no reports" before reaching the LLM call.
    tools = {"technical": lambda t: f"# tech {t}\nstub line\n"}
    graph = build_graph(tools)

    final_state = graph.invoke({"ticker": "NVDA", "question": "thesis?"})

    # synthesize must have been called — otherwise this test isn't proving
    # the fail-closed contract, just that the graph routes around the LLM.
    assert "synthesize" in call_log

    # The fallback returned a ConversationalAnswer — no exception, no
    # propagation, no thesis (since the LLM crashed).
    conversational = final_state.get("conversational")
    assert isinstance(conversational, ConversationalAnswer)
    assert conversational.answer
    assert final_state.get("thesis") is None
