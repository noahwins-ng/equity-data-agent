"""Tests for agent.tracing (QNT-61).

Covers the LangfuseResource wrapper, the traced_invoke helper, and the
architectural contract that every agent LLM call must route through the
tracing helper (so no code path can emit an untraced LLM call).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from agent import tracing
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


@pytest.fixture
def enabled_resource(monkeypatch: pytest.MonkeyPatch) -> tracing.LangfuseResource:
    """Return a LangfuseResource whose `enabled` flag is True but whose
    Langfuse client is stubbed out — so we exercise the tracing branch of
    `traced_invoke` without hitting the network."""
    resource = tracing.LangfuseResource.__new__(tracing.LangfuseResource)
    resource.enabled = True
    resource._client = MagicMock()
    generation = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=generation)
    cm.__exit__ = MagicMock(return_value=False)
    resource._client.start_as_current_observation.return_value = cm
    return resource


def _ai_message(content: str) -> AIMessage:
    msg = AIMessage(content=content)
    msg.usage_metadata = {  # type: ignore[attr-defined]
        "input_tokens": 42,
        "output_tokens": 17,
        "total_tokens": 59,
    }
    msg.response_metadata = {"model_name": "equity-agent/default"}
    return msg


def test_disabled_when_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    resource = tracing.LangfuseResource()
    assert resource.enabled is False


def test_enabled_when_both_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")
    # Stub the SDK constructor so this test doesn't hit the network just to
    # validate our enabled/disabled branch.
    monkeypatch.setattr(tracing, "Langfuse", MagicMock(return_value=MagicMock()))
    resource = tracing.LangfuseResource()
    assert resource.enabled is True


def test_traced_invoke_passthrough_when_disabled() -> None:
    """Disabled resource must not touch the Langfuse client and must return
    the LLM response unchanged."""
    resource = tracing.LangfuseResource.__new__(tracing.LangfuseResource)
    resource.enabled = False
    resource._client = None

    llm = MagicMock()
    expected = _ai_message("hello")
    llm.invoke.return_value = expected

    result = resource.traced_invoke(llm, "prompt", name="synthesize")

    assert result is expected
    llm.invoke.assert_called_once_with("prompt")


def test_traced_invoke_creates_generation_span(
    enabled_resource: tracing.LangfuseResource,
) -> None:
    """Enabled path opens a generation span with the node name + prompt,
    then updates it with the LLM output, model, and token usage."""
    llm = MagicMock()
    llm.invoke.return_value = _ai_message("thesis text")

    result = enabled_resource.traced_invoke(llm, "prompt text", name="synthesize")

    assert result.content == "thesis text"
    client = cast(MagicMock, enabled_resource._client)
    client.start_as_current_observation.assert_called_once_with(
        as_type="generation",
        name="synthesize",
        input="prompt text",
    )
    cm = client.start_as_current_observation.return_value
    generation = cm.__enter__.return_value
    generation.update.assert_called_once_with(
        output="thesis text",
        model="equity-agent/default",
        usage_details={"input": 42, "output": 17, "total": 59},
    )


def test_traced_invoke_records_pydantic_response_as_json(
    enabled_resource: tracing.LangfuseResource,
) -> None:
    """QNT-133: ``with_structured_output(Thesis)`` returns a pydantic model
    (not an ``AIMessage``). The traced output field must be the JSON dump,
    not ``str(thesis)`` which is a Python ``repr`` and useless in the UI."""
    from agent.thesis import Thesis

    thesis = Thesis(
        setup="setup",
        bull_case=["b1"],
        bear_case=[],
        verdict_stance="constructive",
        verdict_action="hold",
    )
    llm = MagicMock()
    llm.invoke.return_value = thesis

    enabled_resource.traced_invoke(llm, "prompt", name="synthesize")

    client = cast(MagicMock, enabled_resource._client)
    cm = client.start_as_current_observation.return_value
    generation = cm.__enter__.return_value
    # Output must be a JSON string the dashboard can render — verify by
    # checking the call carried the canonical setup/stance fields.
    call_kwargs = generation.update.call_args.kwargs
    output = call_kwargs["output"]
    assert isinstance(output, str)
    assert '"setup":"setup"' in output
    assert '"verdict_stance":"constructive"' in output
    # Token / model metadata is unavailable for a structured response — must
    # surface as None rather than a stale value pulled from a non-existent
    # AIMessage.
    assert call_kwargs["model"] is None
    assert call_kwargs["usage_details"] is None


def test_traced_invoke_passes_messages_list_to_span(
    enabled_resource: tracing.LangfuseResource,
) -> None:
    """QNT-58 review fix: synthesize node now passes ``[SystemMessage, HumanMessage]``
    rather than a flat string. The enabled path must hand the list to
    ``llm.invoke`` unchanged AND record it as ``input`` on the Langfuse span
    so the dashboard shows system + user turns separately. Regression guard
    for a latent serialization bug — a future Langfuse SDK that rejects
    pydantic models from ``input=`` would silently drop trace fidelity."""
    llm = MagicMock()
    llm.invoke.return_value = _ai_message("thesis text")
    messages = [
        SystemMessage(content="rules"),
        HumanMessage(content="task"),
    ]

    enabled_resource.traced_invoke(llm, messages, name="synthesize")

    llm.invoke.assert_called_once_with(messages)
    client = cast(MagicMock, enabled_resource._client)
    client.start_as_current_observation.assert_called_once_with(
        as_type="generation",
        name="synthesize",
        input=messages,
    )


def test_traced_invoke_passthrough_accepts_messages_list_when_disabled() -> None:
    """Companion to the enabled-path test: the disabled (no-Langfuse) path
    must also accept the messages list unchanged."""
    resource = tracing.LangfuseResource.__new__(tracing.LangfuseResource)
    resource.enabled = False
    resource._client = None

    llm = MagicMock()
    expected = _ai_message("hi")
    llm.invoke.return_value = expected
    messages = [SystemMessage(content="rules"), HumanMessage(content="task")]

    result = resource.traced_invoke(llm, messages, name="synthesize")

    assert result is expected
    llm.invoke.assert_called_once_with(messages)


def test_traced_invoke_tags_span_and_reraises_on_error(
    enabled_resource: tracing.LangfuseResource,
) -> None:
    """If llm.invoke raises, the generation span must be tagged ERROR with
    the exception message before the exception propagates — otherwise the
    dashboard shows a hanging empty generation."""
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("groq 429")

    with pytest.raises(RuntimeError, match="groq 429"):
        enabled_resource.traced_invoke(llm, "prompt", name="synthesize")

    client = cast(MagicMock, enabled_resource._client)
    cm = client.start_as_current_observation.return_value
    generation = cm.__enter__.return_value
    generation.update.assert_called_once_with(
        level="ERROR",
        status_message="RuntimeError: groq 429",
    )


def test_usage_from_response_returns_none_when_missing() -> None:
    msg = AIMessage(content="x")
    assert tracing._usage_from_response(msg) is None


def test_model_from_response_prefers_model_name() -> None:
    msg = AIMessage(content="x")
    msg.response_metadata = {"model_name": "a", "model": "b"}
    assert tracing._model_from_response(msg) == "a"


def test_flush_is_noop_when_disabled() -> None:
    resource = tracing.LangfuseResource.__new__(tracing.LangfuseResource)
    resource.enabled = False
    resource._client = None
    resource.flush()  # must not raise


def _is_get_llm_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_llm"
    )


def _contains_get_llm_call(node: ast.AST) -> bool:
    """Return True if ``node`` is — or transitively contains — a ``get_llm()``
    call. Catches chained-call forms like ``get_llm().with_structured_output(
    Thesis)`` (QNT-133): the resulting runnable is still tied to ``get_llm()``,
    so its ``.invoke()`` must route through ``traced_invoke``. A bare
    ``_is_get_llm_call`` check only matches the top-level node, which is why
    structured-output bindings used to slip past CI before this generalisation.
    """
    return any(_is_get_llm_call(child) for child in ast.walk(node))


def _find_raw_llm_invokes(source: str) -> list[int]:
    """Return line numbers of raw ``.invoke()`` / ``.ainvoke()`` calls in
    ``source`` whose receiver is (a) a name bound to an expression containing
    ``get_llm()`` (e.g. ``llm = get_llm()`` or
    ``structured = get_llm().with_structured_output(...)``) or
    (b) an inline expression that itself contains ``get_llm()``."""
    tree = ast.parse(source)

    # Collect names bound to any expression containing ``get_llm()`` —
    # ``x = get_llm()`` (ast.Assign), ``x: ChatOpenAI = get_llm()`` (ast.AnnAssign),
    # AND ``x = get_llm().with_structured_output(...)`` (chained, QNT-133).
    llm_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _contains_get_llm_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    llm_names.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _contains_get_llm_call(node.value)
            and isinstance(node.target, ast.Name)
        ):
            llm_names.add(node.target.id)

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"invoke", "ainvoke"}
        ):
            continue
        receiver = node.func.value
        is_bound_name = isinstance(receiver, ast.Name) and receiver.id in llm_names
        is_inline_get_llm = _contains_get_llm_call(receiver)
        if is_bound_name or is_inline_get_llm:
            offenders.append(node.lineno)
    return offenders


def test_no_raw_llm_invoke_in_agent_package() -> None:
    """Architectural invariant: no agent code path emits an LLM call without
    a Langfuse trace. Allowlist: ``tracing.py`` owns the only legitimate raw
    invocations inside ``traced_invoke``; tests are exempt."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    pkg_root = repo_root / "packages" / "agent" / "src" / "agent"
    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        if py.name == "tracing.py":
            continue
        for lineno in _find_raw_llm_invokes(py.read_text()):
            offenders.append(f"{py.relative_to(repo_root)}:{lineno}")

    assert not offenders, (
        "Raw llm.invoke()/.ainvoke() calls bypass Langfuse tracing. "
        "Route through agent.tracing.langfuse.traced_invoke instead:\n  " + "\n  ".join(offenders)
    )


def test_ast_scanner_catches_annotated_assignment() -> None:
    """`x: ChatOpenAI = get_llm()` is ast.AnnAssign — regression guard for
    the bare ast.Assign gap flagged in review."""
    source = (
        "from agent.llm import get_llm\n"
        "def run():\n"
        "    llm: object = get_llm()\n"
        "    return llm.invoke('hi')\n"
    )
    assert _find_raw_llm_invokes(source) == [4]


def test_ast_scanner_catches_chained_call() -> None:
    """`get_llm().invoke(...)` one-liner has no assignment target — regression
    guard for the chained-call gap flagged in review."""
    source = "from agent.llm import get_llm\nresult = get_llm().invoke('hi')\n"
    assert _find_raw_llm_invokes(source) == [2]


def test_ast_scanner_catches_chained_with_structured_output_assignment() -> None:
    """QNT-133 regression guard: ``x = get_llm().with_structured_output(Thesis)``
    binds ``x`` to a runnable that still invokes the LLM. ``x.invoke(...)``
    outside ``traced_invoke`` must be flagged just like ``llm.invoke(...)``.

    Before this fixture the scanner only walked the top-level RHS for
    ``get_llm()``, so a chained call slipped past CI."""
    source = (
        "from agent.llm import get_llm\n"
        "def run():\n"
        "    structured = get_llm().with_structured_output(object)\n"
        "    return structured.invoke('hi')\n"
    )
    assert _find_raw_llm_invokes(source) == [4]


def test_ast_scanner_catches_chained_with_structured_output_inline() -> None:
    """Inline form: ``get_llm().with_structured_output(...).invoke(...)`` has no
    intermediate binding; the receiver is a Call whose RHS contains ``get_llm()``.
    The scanner must still flag it."""
    source = (
        "from agent.llm import get_llm\n"
        "result = get_llm().with_structured_output(object).invoke('hi')\n"
    )
    assert _find_raw_llm_invokes(source) == [2]


def test_ast_scanner_catches_ainvoke() -> None:
    """Async path is covered too — QNT-60's SSE streaming will use ainvoke."""
    source = (
        "from agent.llm import get_llm\n"
        "async def run():\n"
        "    llm = get_llm()\n"
        "    return await llm.ainvoke('hi')\n"
    )
    assert _find_raw_llm_invokes(source) == [4]
