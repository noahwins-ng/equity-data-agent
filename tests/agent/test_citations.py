"""Unit tests for the retrieved-source anchor-integrity guard (QNT-305).

The deterministic strip that drops a hallucinated out-of-range retrieved id
before it can render as an anchored citation, plus the eval-path detector.
"""

from __future__ import annotations

from agent.citations import (
    find_oob_anchor_ids,
    strip_oob_anchors,
    strip_oob_anchors_in_obj,
)


def test_strip_drops_out_of_range_source_anchor_keeps_attribution() -> None:
    # Only R1/R2 were retrieved; R5 points at no row -> keep the source, drop id.
    text = "Buyback expanded (source: news R5). Margins firm (source: fundamental R1)."
    out = strip_oob_anchors(text, max_id=2)
    assert "(source: news)" in out  # id stripped
    assert "R5" not in out
    assert "(source: fundamental R1)" in out  # in-range control untouched


def test_strip_removes_out_of_range_bare_tag() -> None:
    # The narrate voice appends bare [Rn] tags; an out-of-range one is removed
    # entirely (no source label to keep) with no orphan double space left.
    text = "The Rubin platform (finnhub, 2026-06-27) [R11] is material."
    out = strip_oob_anchors(text, max_id=2)
    assert "[R11]" not in out
    assert "  " not in out
    assert out == "The Rubin platform (finnhub, 2026-06-27) is material."


def test_strip_keeps_in_range_bare_tag() -> None:
    text = "The deal (finnhub, 2026-06-30) [R2] expands reach."
    assert strip_oob_anchors(text, max_id=2) == text


def test_strip_with_zero_rows_drops_every_anchor() -> None:
    # No retrieved sources this turn -> any anchor is out of range.
    text = "A claim (source: news R1) and a tag [R2]."
    out = strip_oob_anchors(text, max_id=0)
    assert out == "A claim (source: news) and a tag."


def test_strip_leaves_canned_citations_untouched() -> None:
    text = "RSI is firm (source: technical). PE inline (source: fundamental)."
    assert strip_oob_anchors(text, max_id=0) == text


def test_find_flags_out_of_range_ids_only() -> None:
    text = "A (source: news R1). B (source: news R5). C [R11]. D (source: technical)."
    assert find_oob_anchor_ids(text, max_id=2) == [5, 11]


def test_find_clean_when_all_in_range() -> None:
    text = "A (source: news R1). B (source: fundamental R2)."
    assert find_oob_anchor_ids(text, max_id=2) == []


def test_strip_in_obj_recurses_card_payload_shape() -> None:
    payload = {
        "verdict": "constructive",
        "supports": ["Buyback expanded (source: news R5)."],
        "aspects": {"news": {"summary": "Deal [R7] closed (source: news R1)."}},
    }
    out = strip_oob_anchors_in_obj(payload, max_id=2)
    assert out["supports"] == ["Buyback expanded (source: news)."]
    assert out["aspects"]["news"]["summary"] == "Deal closed (source: news R1)."
