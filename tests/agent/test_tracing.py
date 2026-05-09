"""Tests for agent.tracing (QNT-61, QNT-181).

Post-QNT-181 the module is a thin client + CallbackHandler factory; the
``traced_invoke`` wrapper is gone in favour of a single ``CallbackHandler``
attached to the graph at entry. The architectural invariant is now: every
``llm.invoke()`` / ``.ainvoke()`` call inside the agent package must pass a
``config=`` keyword argument so the LangGraph callback handler propagates
through to LLM-level generation observations. The AST scanner below
enforces it at lint time; the runtime contract is asserted by
``test_graph.py::test_llm_calls_carry_runtime_config_for_callback_propagation``.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from agent import tracing


def test_disabled_when_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No keys -> _build_client returns None -> make_callback_handler is None
    so callers fall back to ``config={}`` and the graph runs untraced."""
    from shared.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    # Reload the module so ``langfuse`` is reconstructed against the patched
    # settings (the singleton is built at import time).
    reloaded = importlib.reload(tracing)
    assert reloaded.langfuse is None
    assert reloaded.make_callback_handler() is None


def test_enabled_when_both_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both keys set -> _build_client constructs a Langfuse client and
    make_callback_handler returns a fresh CallbackHandler."""
    from shared.config import settings

    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(tracing, "Langfuse", MagicMock(return_value=MagicMock()))
    fake_handler = MagicMock(name="CallbackHandler-instance")
    monkeypatch.setattr(tracing, "CallbackHandler", MagicMock(return_value=fake_handler))

    reloaded_client = tracing._build_client()
    assert reloaded_client is not None
    langfuse_ctor = cast(MagicMock, tracing.Langfuse)
    langfuse_ctor.assert_called_once_with(
        public_key="pk-test",
        secret_key="sk-test",
        base_url=settings.LANGFUSE_BASE_URL,
    )


def test_make_callback_handler_returns_fresh_instance_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-request handler factory: each call mints a new CallbackHandler so
    long-lived state can't leak across requests."""
    monkeypatch.setattr(tracing, "langfuse", MagicMock(name="langfuse-singleton"))
    monkeypatch.setattr(tracing, "CallbackHandler", MagicMock(side_effect=[object(), object()]))

    a = tracing.make_callback_handler()
    b = tracing.make_callback_handler()
    assert a is not b


def test_flush_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """flush() must not raise when langfuse is None (CLI exits, eval bench
    runs, test sessions all hit this path)."""
    monkeypatch.setattr(tracing, "langfuse", None)
    tracing.flush()  # must not raise


def test_flush_calls_client_flush_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(tracing, "langfuse", fake)
    tracing.flush()
    fake.flush.assert_called_once_with()


# ─── Architectural invariant: llm.invoke must carry config= for tracing ─────


def _is_get_llm_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_llm"
    )


def _contains_get_llm_call(node: ast.AST) -> bool:
    return any(_is_get_llm_call(child) for child in ast.walk(node))


def _find_llm_invokes_missing_config(source: str) -> list[int]:
    """Return line numbers of ``.invoke()`` / ``.ainvoke()`` calls whose
    receiver is bound to (or is itself) ``get_llm()`` AND that omit the
    ``config=`` keyword. The CallbackHandler attached at graph entry only
    propagates to LLM-level generation observations when ``config`` reaches
    the LLM call — a missing kwarg silently drops the trace."""
    tree = ast.parse(source)

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
        if not (is_bound_name or is_inline_get_llm):
            continue
        if not any(kw.arg == "config" for kw in node.keywords):
            offenders.append(node.lineno)
    return offenders


def test_llm_invoke_calls_pass_config_kwarg() -> None:
    """Architectural invariant (QNT-181): every ``llm.invoke()`` /
    ``.ainvoke()`` in the agent package must pass ``config=...`` so the
    LangGraph CallbackHandler propagates. Allowlist:

    * ``tracing.py`` — owns the singleton, no LLM calls of its own.
    * ``evals/`` — eval bench __main__ env-strips Langfuse keys at import
      time so tracing is disabled regardless; no callback to propagate.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    pkg_root = repo_root / "packages" / "agent" / "src" / "agent"
    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        if py.name == "tracing.py" or "evals" in py.relative_to(pkg_root).parts:
            continue
        for lineno in _find_llm_invokes_missing_config(py.read_text()):
            offenders.append(f"{py.relative_to(repo_root)}:{lineno}")

    assert not offenders, (
        "LLM invoke call missing config= kwarg — Langfuse CallbackHandler "
        "won't propagate to the generation observation:\n  " + "\n  ".join(offenders)
    )


def test_ast_scanner_flags_invoke_without_config() -> None:
    source = (
        "from agent.llm import get_llm\n"
        "def run(config):\n"
        "    llm = get_llm()\n"
        "    return llm.invoke('hi')\n"
    )
    assert _find_llm_invokes_missing_config(source) == [4]


def test_ast_scanner_passes_when_config_present() -> None:
    source = (
        "from agent.llm import get_llm\n"
        "def run(config):\n"
        "    llm = get_llm()\n"
        "    return llm.invoke('hi', config=config)\n"
    )
    assert _find_llm_invokes_missing_config(source) == []


def test_ast_scanner_catches_chained_with_structured_output() -> None:
    """``get_llm().with_structured_output(Thesis)`` binds to a runnable that
    still ultimately invokes the LLM — must carry config= just like a
    direct llm.invoke."""
    source = (
        "from agent.llm import get_llm\n"
        "def run(config):\n"
        "    structured = get_llm().with_structured_output(object)\n"
        "    return structured.invoke('hi')\n"
    )
    assert _find_llm_invokes_missing_config(source) == [4]


def test_ast_scanner_catches_ainvoke_without_config() -> None:
    source = (
        "from agent.llm import get_llm\n"
        "async def run(config):\n"
        "    llm = get_llm()\n"
        "    return await llm.ainvoke('hi')\n"
    )
    assert _find_llm_invokes_missing_config(source) == [4]
