"""Analyst-voice deterministic guardrails (QNT-303, D-6).

The dialogue judge (an LLM) scores voice qualitatively, but soft filler slips
past it turn to turn. This module is the cheap, permanent, deterministic twin
of the QNT-156 conversational digit check (:meth:`ConversationalAnswer.
has_numeric_claims`): a banned-phrase regex set that flags the stock analyst
filler a senior desk never writes -- "it's important to note", a sentence-
leading "Overall,", "indicating potential for". It is enforced in the dialogue
eval path (``_apply_deterministic_filler_gate``) so a filler phrase caps the
``voice_match`` axis at 0 regardless of what the judge thought -- a no-regret
regression guard that holds even when the current corpus is clean.

The list is deliberately high-precision: each pattern targets a padding phrase
that carries no analytical content, anchored so it does not fire on the same
words used substantively (a sentence-leading ``Overall,`` is filler; ``the
overall signal is bullish`` is not).
"""

from __future__ import annotations

import re

# Each pattern matches a padding phrase with no analytical payload. Anchored
# tightly to avoid firing on the same words used substantively -- see the
# module docstring. ``(?im)`` is applied per-pattern at compile time so
# sentence-leading anchors (``^``) match any line, case-insensitively.
_BANNED_FILLER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "it's important to note" / "it is important to note" / "worth noting".
    # The leading \b is load-bearing: without it, a word ending in "it"
    # ("Deficit is important to note", "Profit's important to note") false-fires.
    re.compile(r"\bit['’]?s?\s+(?:is\s+)?important\s+to\s+note\b", re.I),
    re.compile(r"\b(?:it['’]?s|it\s+is)\s+worth\s+noting\b", re.I),
    # "indicating potential for" -- the v1 padding tell (agent-analyst-quality
    # 2026-05-18, headline finding on RSI "indicating potential for growth").
    re.compile(r"\bindicating\s+potential\s+for\b", re.I),
    # Sentence-leading throat-clearing adverbs.
    re.compile(r"(?im)^\s*overall,\s", re.I),
    re.compile(r"(?im)^\s*in\s+conclusion,\s", re.I),
    re.compile(r"(?im)^\s*in\s+summary,\s", re.I),
    re.compile(r"(?im)^\s*needless\s+to\s+say,?\s", re.I),
    # "On balance" as an opener -- already banned in ANALYST_VOICE_BLOCK prose,
    # pinned here so a regression is caught deterministically, not just by taste.
    # Negative lookahead excludes "On balance sheet ..." -- a legit line about
    # the balance sheet, not the filler hedge.
    re.compile(r"(?im)^\s*on\s+balance,?\s(?!sheets?\b)", re.I),
    # Empty hedges that state nothing.
    re.compile(r"\bthat\s+being\s+said,\s", re.I),
)


# QNT-359: report-scaffolding leak patterns. A closed-vocab label (Premium /
# Inline / Discounted, Uptrend / Sideways / Downtrend) is a machine token that
# drives the frontend pill and the deterministic verdict math -- it was never
# meant to be a word the user reads. The prompt teaches the model to translate it
# to analyst prose; this set is the hard, permanent tripwire that catches the
# leak shapes when a prompt hope fails: naming the label token as a common noun
# ("carries a Premium label", "the fundamental label stays Premium", a bare
# "Premium label") and calling a report by name as scaffolding ("the fundamental
# report"). High-precision by construction: every anchor pins the scaffolding
# word ("label" / "report") next to a closed-vocab or domain token, so a bare
# adjective ("trading at a premium", "carries a richer multiple", "the trend is
# up") never fires.
_CLOSED_VOCAB = r"(?:premium|inline|discounted|uptrend|sideways|downtrend)"

# The linking verbs the falsifier-rule leak puts between "label" and the token
# ("label stays Premium", "label moves to Inline", "label is Uptrend"). Anchoring
# on these -- rather than an open word window -- is what keeps "Bulls would label
# this a fair premium" (a verb use of "label" next to an unrelated adjective) from
# false-firing.
_LABEL_LINK_VERB = (
    r"(?:stays?|is|are|was|were|remains?|becomes?|"
    r"moves?\s+to|flips?\s+to|shifts?\s+to|turns?\s+to|goes?\s+to)"
)

_BANNED_SCAFFOLDING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "carries a Premium label" / "carry an Inline label" / "carried a ... label".
    re.compile(r"\bcarr(?:ies|y|ied)\s+an?\s+\w+\s+label\b", re.I),
    # A closed-vocab token used as a noun with "label": "Premium label".
    re.compile(rf"\b{_CLOSED_VOCAB}\s+label\b", re.I),
    # "the label stays Premium" / "label moves to Inline" -- the falsifier leak.
    re.compile(rf"\blabel\s+{_LABEL_LINK_VERB}\s+(?:the\s+)?{_CLOSED_VOCAB}\b", re.I),
    # "the fundamental/technical/news/company report" -- or "... label" -- as a
    # scaffolding noun ("the fundamental label", "per the technical report").
    re.compile(r"\bthe\s+(?:fundamental|technical|news|company)\s+(?:report|label)\b", re.I),
)


def find_scaffolding_leak(text: str) -> list[str]:
    """Return the verbatim report-scaffolding phrases in ``text`` (QNT-359).

    Flags a machine label token or report name leaking into user-facing prose --
    "carries a Premium label", "the fundamental report". Empty when clean.
    """
    if not text:
        return []
    hits: list[str] = []
    for pattern in _BANNED_SCAFFOLDING_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(match.group(0).strip())
    return hits


def has_scaffolding_leak(text: str) -> bool:
    """Return True if ``text`` leaks a machine label token or a report name."""
    return bool(find_scaffolding_leak(text))


def find_filler(text: str) -> list[str]:
    """Return the verbatim filler phrases found in ``text`` (empty if clean).

    Used by the dialogue eval gate and pinned by unit fixtures. Returns the
    matched substrings (not the patterns) so a failure message can quote what
    tripped it.
    """
    if not text:
        return []
    hits: list[str] = []
    for pattern in _BANNED_FILLER_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(match.group(0).strip())
    return hits


def has_filler(text: str) -> bool:
    """Return True if ``text`` contains any banned analyst-voice filler phrase."""
    return bool(find_filler(text))


__all__ = [
    "find_filler",
    "find_scaffolding_leak",
    "has_filler",
    "has_scaffolding_leak",
]
