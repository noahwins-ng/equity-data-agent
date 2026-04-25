"""Tests for tool-call correctness (QNT-67 eval (c))."""

from __future__ import annotations

from agent.evals.tool_calls import ToolCallResult, check, wrap_with_recorder


class TestWrapWithRecorder:
    """The recorder is the only way the tool-call eval can know what the
    graph actually invoked — over-fetching is allowed but missing a tool is
    a fail, so the record must be precise."""

    def test_recorder_captures_invocation_order(self) -> None:
        tools = {
            "technical": lambda t: f"tech-{t}",
            "fundamental": lambda t: f"fund-{t}",
            "news": lambda t: f"news-{t}",
        }
        wrapped, recorder = wrap_with_recorder(tools)
        wrapped["fundamental"]("NVDA")
        wrapped["technical"]("NVDA")
        wrapped["fundamental"]("AAPL")
        assert recorder == ["fundamental", "technical", "fundamental"]

    def test_wrapped_tools_return_underlying_results(self) -> None:
        wrapped, _ = wrap_with_recorder({"x": lambda t: f"out-{t}"})
        assert wrapped["x"]("NVDA") == "out-NVDA"

    def test_no_for_loop_closure_capture_bug(self) -> None:
        # If wrap_with_recorder fell into the closing-over-loop-var trap,
        # every wrapped call would record the LAST tool's name regardless
        # of which key was invoked. Freeze the correct behaviour.
        wrapped, recorder = wrap_with_recorder(
            {f"tool_{i}": (lambda _ticker, i=i: str(i)) for i in range(3)}  # noqa: ARG005
        )
        wrapped["tool_0"]("X")
        wrapped["tool_2"]("X")
        assert recorder == ["tool_0", "tool_2"]


class TestCheck:
    def test_clean_when_all_expected_tools_were_called(self) -> None:
        result = check(["technical", "fundamental"], ["technical", "fundamental"])
        assert isinstance(result, ToolCallResult)
        assert result.ok
        assert result.missing == ()
        assert result.reason() == "clean"

    def test_overfetching_is_allowed(self) -> None:
        # The graph plan node tells the LLM to over-fetch when in doubt;
        # the eval only fails on under-fetching.
        result = check(["technical"], ["technical", "fundamental", "news"])
        assert result.ok

    def test_missing_expected_tool_is_failure(self) -> None:
        result = check(["technical", "fundamental"], ["technical"])
        assert not result.ok
        assert result.missing == ("fundamental",)
        assert "fundamental" in result.reason()

    def test_no_tools_called_at_all(self) -> None:
        result = check(["technical"], [])
        assert not result.ok
        assert "called: none" in result.reason()
