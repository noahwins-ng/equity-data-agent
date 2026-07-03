"""Unit tests for the retrieved-source anchor-integrity guard (QNT-305 + corpus).

The deterministic strip that drops an untrustworthy retrieved anchor -- out of
range (a fake footnote) OR corpus-mismatched (a source name that does not match
the id's corpus) -- before it can render, plus the eval-path detector.

Rows are passed as ``{"corpus": ...}`` dicts. Corpus-less rows exercise the
range-only check (the original QNT-305 behaviour); tagged rows exercise the
corpus check.
"""

from __future__ import annotations

from agent.citations import (
    find_bad_anchors,
    strip_bad_anchors,
    strip_bad_anchors_in_obj,
)

NEWS2 = [{"corpus": "news"}, {"corpus": "news"}]  # R1, R2 both news
BARE2 = [{}, {}]  # 2 rows, no corpus -> range-only


def test_strip_drops_out_of_range_source_anchor_keeps_attribution() -> None:
    # Only R1/R2 exist; R5 points at no row -> keep the source, drop the id.
    text = "Buyback expanded (source: news R5). Margins firm (source: news R1)."
    out = strip_bad_anchors(text, NEWS2)
    assert "(source: news)" in out  # id stripped
    assert "R5" not in out
    assert "(source: news R1)" in out  # in-range + right corpus untouched


def test_strip_removes_out_of_range_bare_tag() -> None:
    text = "The Rubin platform (finnhub, 2026-06-27) [R11] is material."
    out = strip_bad_anchors(text, NEWS2)
    assert "[R11]" not in out
    assert "  " not in out
    assert out == "The Rubin platform (finnhub, 2026-06-27) is material."


def test_strip_keeps_in_range_bare_tag() -> None:
    text = "The deal (finnhub, 2026-06-30) [R2] expands reach."
    assert strip_bad_anchors(text, NEWS2) == text


def test_strip_with_zero_rows_drops_every_anchor() -> None:
    text = "A claim (source: news R1) and a tag [R2]."
    out = strip_bad_anchors(text, [])
    assert out == "A claim (source: news) and a tag."


def test_strip_leaves_canned_citations_untouched() -> None:
    text = "RSI is firm (source: technical). PE inline (source: fundamental)."
    assert strip_bad_anchors(text, []) == text


# ─── corpus-consistency (in range but wrong corpus) ───────────────────────


def test_strip_drops_corpus_mismatched_fundamental_on_news_row() -> None:
    # R1 is a NEWS row; ``fundamental R1`` mis-staples it. In range, so only the
    # corpus check catches it: keep the plain ``fundamental`` chip, drop R1.
    text = "Growth is strong (source: fundamental R1)."
    out = strip_bad_anchors(text, [{"corpus": "news"}])
    assert out == "Growth is strong (source: fundamental)."


def test_strip_keeps_corpus_matched_news_on_news_row() -> None:
    text = "Deal closed (source: news R1)."
    assert strip_bad_anchors(text, [{"corpus": "news"}]) == text


def test_strip_keeps_fundamental_on_earnings_row() -> None:
    # Earnings hits fold into the fundamental report, so ``fundamental Rk`` is
    # valid when Rk is an earnings row.
    text = "Guidance raised (source: fundamental R1)."
    assert strip_bad_anchors(text, [{"corpus": "earnings"}]) == text


def test_strip_drops_never_retrieval_backed_name() -> None:
    # No corpus feeds ``technical`` / ``company``, so an id on them is never valid.
    text = "RSI firm (source: technical R1)."
    out = strip_bad_anchors(text, [{"corpus": "news"}])
    assert out == "RSI firm (source: technical)."


def test_corpus_less_row_falls_back_to_range_only() -> None:
    # A row without a corpus tag can't be corpus-checked -> range-only, so an
    # in-range ``fundamental R1`` is kept (QNT-305 original behaviour preserved).
    text = "Growth strong (source: fundamental R1)."
    assert strip_bad_anchors(text, BARE2) == text


# ─── detector ─────────────────────────────────────────────────────────────


def test_find_flags_out_of_range_and_corpus_mismatch() -> None:
    text = "A (source: news R1). B (source: news R5). C (source: fundamental R1)."
    # R1 news ok; R5 out of range; fundamental R1 wrong corpus (R1 is news).
    assert find_bad_anchors(text, [{"corpus": "news"}]) == ["news R5", "fundamental R1"]


def test_find_clean_when_all_valid() -> None:
    text = "A (source: news R1). B (source: news R2)."
    assert find_bad_anchors(text, NEWS2) == []


def test_strip_in_obj_recurses_card_payload_shape() -> None:
    payload = {
        "verdict": "constructive",
        "supports": ["Buyback expanded (source: news R5)."],
        "aspects": {"news": {"summary": "Deal [R1] closed (source: fundamental R1)."}},
    }
    out = strip_bad_anchors_in_obj(payload, [{"corpus": "news"}])
    assert out["supports"] == ["Buyback expanded (source: news)."]
    # bare [R1] kept (in range, corpus-agnostic); fundamental R1 dropped (news row).
    assert out["aspects"]["news"]["summary"] == "Deal [R1] closed (source: fundamental)."
