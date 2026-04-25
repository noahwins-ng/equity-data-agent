"""Tool-call correctness (QNT-67, eval type (c)).

For each golden-set question, assert the expected tools were called by the
graph. Catches the prompt-regression class where the agent reaches for the
wrong reports — e.g. answering a fundamental question with only the news
tool, or skipping the technical tool on a chart-shaped question.

Over-fetching is OK by design: ``agent.graph._build_plan_prompt`` instructs
the planner to "include every report that is even marginally relevant", so
``actual_tools`` is allowed to be a strict superset of ``expected_tools``.
The check fails only when an expected tool is missing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

ToolFn = Callable[[str], str]


@dataclass(frozen=True)
class ToolCallResult:
    """Outcome of one tool-call check.

    ``missing`` is the set of expected tools that were never invoked.
    ``actual`` records every tool the graph did call (in invocation order)
    so failed runs surface what the planner reached for instead.
    """

    ok: bool
    missing: tuple[str, ...]
    actual: tuple[str, ...]
    expected: tuple[str, ...]

    def reason(self) -> str:
        if self.ok:
            return "clean"
        return f"missing: {', '.join(self.missing)} (called: {', '.join(self.actual) or 'none'})"


def wrap_with_recorder(
    tools: dict[str, ToolFn],
    *,
    recorder: list[str] | None = None,
) -> tuple[dict[str, ToolFn], list[str]]:
    """Return ``(wrapped_tools, recorder)`` where each invocation appends to
    the recorder.

    Pass an existing ``recorder`` list to share state across multiple wraps
    (e.g. unit tests that build several graphs around the same recorder);
    otherwise a fresh list is allocated.
    """
    log: list[str] = recorder if recorder is not None else []

    def _wrap(name: str, fn: ToolFn) -> ToolFn:
        # Bind ``name`` and ``fn`` as defaults so the closure captures the
        # current loop values rather than the last-iteration values — the
        # classic Python "for-loop closure capture" footgun.
        def recorded(ticker: str, _name: str = name, _fn: ToolFn = fn) -> str:
            log.append(_name)
            return _fn(ticker)

        return recorded

    wrapped = {name: _wrap(name, fn) for name, fn in tools.items()}
    return wrapped, log


def check(expected: Iterable[str], actual: Iterable[str]) -> ToolCallResult:
    """Return a ``ToolCallResult`` from the expected and actually-called
    tool-name iterables.

    ``actual`` may contain duplicates (e.g. retried tool calls); the check
    is set-based on the right side and ordered on the left only for the
    error message.
    """
    expected_tup = tuple(expected)
    actual_tup = tuple(actual)
    actual_set = set(actual_tup)
    missing = tuple(name for name in expected_tup if name not in actual_set)
    return ToolCallResult(
        ok=not missing,
        missing=missing,
        actual=actual_tup,
        expected=expected_tup,
    )


__all__ = [
    "ToolCallResult",
    "check",
    "wrap_with_recorder",
]
