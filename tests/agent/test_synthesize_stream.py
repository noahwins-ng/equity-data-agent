"""QNT-494 (AC1): synthesize streams partial cards, then the validated final card.

The structured synthesize call used to be a single non-streamed json_mode request,
so the panel sat blank for the whole ~4-8s generation. It now streams the same
``response_format: json_object`` request, emits ``card_partial`` events carrying
only the fields that have finished generating, and still validates the full text
through the same Pydantic parser -- the final card event and the coerce/redirect
fallback stay authoritative.

These tests patch ``ChatOpenAI._stream`` / ``_generate`` so the real ``get_llm``
request path runs without a proxy.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from agent import graph as graph_module
from agent.conversational import ConversationalAnswer
from agent.nodes.deps import GraphDeps
from agent.nodes.synthesize import synthesize_node
from agent.structured import PartialCardTracker
from agent.thesis import Thesis
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from ._thesis_factory import make_thesis

_THESIS = make_thesis(
    supports=["Uptrend holds, RSI 62 (source: technical)", "Margins widened, too"],
    challenges=["P/E 45.2 sits at a premium (source: fundamental)"],
)
_THESIS_JSON = _THESIS.model_dump_json()


def _chunks(text: str, size: int = 7) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_text: str,
    generate_text: str = "{}",
) -> dict[str, list[dict[str, Any]]]:
    """Patch ChatOpenAI so ``.stream`` yields ``stream_text`` in small chunks and
    ``.invoke`` returns ``generate_text``; record each request payload."""
    calls: dict[str, list[dict[str, Any]]] = {"stream": [], "generate": []}

    def _fake_stream(
        self: ChatOpenAI, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> Iterator[ChatGenerationChunk]:
        # The real ``_stream`` sets ``stream=True`` before building the payload.
        payload = self._get_request_payload(messages, stop=stop, stream=True, **kw)  # noqa: SLF001
        calls["stream"].append(payload)
        for piece in _chunks(stream_text):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    def _fake_generate(
        self: ChatOpenAI, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        calls["generate"].append(self._get_request_payload(messages, stop=stop, **kw))  # noqa: SLF001
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=generate_text))])

    monkeypatch.setattr(ChatOpenAI, "_stream", _fake_stream)
    monkeypatch.setattr(ChatOpenAI, "_generate", _fake_generate)
    return calls


_PROMPT = [SystemMessage(content="You are an analyst."), HumanMessage(content="thesis on NVDA")]


# ─── PartialCardTracker ──────────────────────────────────────────────────────


def _feed_all(text: str, size: int = 3) -> list[dict[str, Any]]:
    tracker = PartialCardTracker()
    out = []
    for piece in _chunks(text, size):
        partial = tracker.feed(piece)
        if partial is not None:
            out.append(partial)
    return out


def _leaves(obj: Any) -> list[Any]:
    if isinstance(obj, dict):
        return [leaf for v in obj.values() for leaf in _leaves(v)]
    if isinstance(obj, list):
        return [leaf for v in obj for leaf in _leaves(v)]
    return [obj]


def test_tracker_emits_only_completed_fields() -> None:
    """Every emitted partial holds only finished values: each string leaf equals
    the corresponding final value's leaf (never a truncated mid-token prefix)."""
    full = json.loads(_THESIS_JSON)
    final_leaves = _leaves(full)
    partials = _feed_all(_THESIS_JSON)

    assert len(partials) >= 5, "expected one partial per completed field, not one total"
    for partial in partials:
        for leaf in _leaves(partial):
            assert leaf in final_leaves, f"incomplete value leaked into a partial: {leaf!r}"
    assert partials[-1] == full


def test_tracker_partials_grow_and_never_repeat() -> None:
    partials = _feed_all(_THESIS_JSON)
    sizes = [len(json.dumps(p)) for p in partials]
    assert sizes == sorted(sizes)
    assert all(a != b for a, b in zip(partials, partials[1:], strict=False))


def test_tracker_ignores_commas_and_braces_inside_strings() -> None:
    text = json.dumps({"a": 'one, two } three ] "quoted"', "b": "x"})
    partials = _feed_all(text, size=1)
    assert partials[0] == {"a": 'one, two } three ] "quoted"'}
    assert partials[-1] == {"a": 'one, two } three ] "quoted"', "b": "x"}


def test_tracker_skips_a_leading_code_fence() -> None:
    partials = _feed_all('```json\n{"a": "x", "b": "y"}\n```')
    assert partials[-1] == {"a": "x", "b": "y"}


# ─── _structured_call streaming ladder ───────────────────────────────────────


def test_structured_call_streams_partials_then_returns_validated_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_llm(monkeypatch, stream_text=_THESIS_JSON)
    partials: list[dict[str, Any]] = []

    result = graph_module._structured_call(
        Thesis, _PROMPT, {}, "system-prompt", linked=False, on_partial=partials.append
    )

    assert result == _THESIS
    assert partials and partials[-1] == json.loads(_THESIS_JSON)
    assert not calls["generate"], "a clean stream must not re-issue a non-streamed call"
    payload = calls["stream"][0]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is True
    for key in ("tools", "tool_choice", "parallel_tool_calls"):
        assert key not in payload
    system = payload["messages"][0]["content"]
    assert json.dumps(Thesis.model_json_schema(), separators=(",", ":")) in system


def test_structured_call_without_on_partial_does_not_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_llm(monkeypatch, stream_text="", generate_text=_THESIS_JSON)

    result = graph_module._structured_call(Thesis, _PROMPT, {}, "system-prompt", linked=False)

    assert result == _THESIS
    assert not calls["stream"]


def test_invalid_json_stream_falls_back_to_the_non_streamed_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truncated = _THESIS_JSON[: len(_THESIS_JSON) // 2]
    calls = _patch_llm(monkeypatch, stream_text=truncated, generate_text=_THESIS_JSON)
    partials: list[dict[str, Any]] = []

    result = graph_module._structured_call(
        Thesis, _PROMPT, {}, "system-prompt", linked=False, on_partial=partials.append
    )

    assert result == _THESIS
    assert partials, "fields completed before the truncation still streamed"
    assert len(calls["generate"]) == 1


def test_invalid_stream_and_invalid_ladder_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, stream_text='{"company": ', generate_text="not json")

    result = graph_module._structured_call(
        Thesis, _PROMPT, {}, "system-prompt", linked=False, on_partial=lambda _c: None
    )

    assert result is None


# ─── synthesize_node event ordering ──────────────────────────────────────────


def _deps(events: list[tuple[str, dict[str, Any]]]) -> GraphDeps:
    return GraphDeps(
        tools={},
        event_emitter=lambda e, d: events.append((e, dict(d))),
        compact_company_tool=None,
        comparison_metrics_tool=None,
        active_retrievals=(),
    )


_STATE: dict[str, Any] = {
    "ticker": "NVDA",
    "question": "Give me a thesis on NVDA",
    "intent": "thesis",
    "plan": ["technical"],
    "reports": {"technical": "## Technical\nRSI 62, Uptrend\n"},
}


def test_synthesize_emits_partials_before_the_final_validated_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_llm(monkeypatch, stream_text=_THESIS_JSON)
    events: list[tuple[str, dict[str, Any]]] = []

    result = synthesize_node(_STATE, {}, _deps(events))  # pyright: ignore[reportArgumentType]

    names = [e for e, _ in events]
    assert names[-1] == "thesis"
    assert names.count("thesis") == 1
    partials = [d for e, d in events if e == "card_partial"]
    assert partials, "no card_partial events streamed before the final card"
    assert set(names[:-1]) == {"card_partial"}
    assert all(p["slot"] == "thesis" for p in partials)
    assert events[-1][1] == _THESIS.model_dump()
    assert result["answer"] == _THESIS


def test_synthesize_invalid_stream_ends_in_the_redirect_not_a_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_llm(monkeypatch, stream_text='{"company": {"summary": "a"}, ', generate_text="nope")
    events: list[tuple[str, dict[str, Any]]] = []

    result = synthesize_node(_STATE, {}, _deps(events))  # pyright: ignore[reportArgumentType]

    assert isinstance(result["answer"], ConversationalAnswer)
    assert "thesis" not in [e for e, _ in events]


def test_partial_failure_never_voids_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A display-only partial bug stops partials but keeps the streamed result --
    it must not discard a good stream and re-run the unstreamed ladder."""
    calls = _patch_llm(monkeypatch, stream_text=_THESIS_JSON)

    def _boom(_card: dict[str, Any]) -> None:
        raise RuntimeError("emit broke")

    result = graph_module._structured_call(
        Thesis, _PROMPT, {}, "system-prompt", linked=False, on_partial=_boom
    )

    assert result == _THESIS
    assert not calls["generate"]


def test_streamed_call_usage_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streamed results carry usage on the message's usage_metadata, not
    llm_output -- the budget tracker must count it rather than log zero usage."""
    from agent.llm import TokenUsageTracker, reset_token_tracker, set_token_tracker

    def _fake_stream(self: ChatOpenAI, *_a: Any, **_kw: Any) -> Iterator[ChatGenerationChunk]:
        for piece in _chunks(_THESIS_JSON):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata={"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
            )
        )

    monkeypatch.setattr(ChatOpenAI, "_stream", _fake_stream)
    tracker = TokenUsageTracker()
    token = set_token_tracker(tracker)
    try:
        result = graph_module._structured_call(
            Thesis, _PROMPT, {}, "system-prompt", linked=False, on_partial=lambda _c: None
        )
    finally:
        reset_token_tracker(token)

    assert result == _THESIS
    assert tracker.total == 1500
    assert tracker.zero_usage_calls == 0
