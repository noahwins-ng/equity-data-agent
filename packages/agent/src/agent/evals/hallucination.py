"""Numeric-claim hallucination detector (QNT-67, eval type (a)).

For every thesis the agent produces, regex every numeric claim out of the
text and assert each appears in the report strings the agent received as
tool output. Any mismatch is a hallucination — the direct operational
enforcement of ADR-003.

Verbatim vs. value-equivalent:
    The AC says "verbatim". We canonicalise away pure formatting differences
    (leading ``$``, trailing ``%``, comma thousand-separators) so a thesis
    that writes ``$1,234`` is accepted when the report wrote ``1234`` — that
    is formatting, not arithmetic. Decimal precision is preserved: a thesis
    that writes ``12.30`` against a report that wrote ``12.3`` IS flagged,
    because changing precision is rounding and rounding is arithmetic.

Sign-magnitude support (QNT-128):
    Reports format YoY changes with explicit signs (``Free cash flow:
    -16.09% YoY``). The model naturally moves the sign into English verbs
    (``free cash flow declined 16.09%``) and writes the unsigned magnitude —
    that is idiom, not arithmetic. ``check`` treats a thesis number ``X`` as
    supported if either ``X`` or its sign-flipped form appears in any
    report. Trade-off: this makes us blind to the rare "model inverted the
    sign" failure (report ``+5%``, thesis ``-5%``); we accept that
    false-negative because it's far less common than the false-positive it
    fixed (and the LLM-as-judge / per-section structure already catch
    sign-direction errors at a higher level).

Numbers we deliberately ignore:
    * Section/list scaffolding emitted by the model — Markdown-heading
      numerals (``## 1.``, ``1.``, ``2)``) — see ``_strip_scaffold``.
    * Citation tags ``(source: …)`` are letters, not digits.

False-positive risk:
    Single-digit integers like ``5`` or ``7`` that the model uses as a
    rhetorical count ("the past 7 days"). We accept those FPs as a price
    for catching real hallucinations — investigate via ``--explain``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Match: optional sign, optional $, digits with optional comma-thousands or
# plain run, optional decimal portion, optional trailing %.
# Examples that match: 12, 12.3, 1,234, $1,234.56, 25%, -3.5%, +0.42
_NUMBER_RE = re.compile(
    r"""
    (?<![\w.])                  # left boundary: no preceding word-char or dot
    [-+]?                       # optional sign
    \$?                         # optional dollar
    (?:
        \d{1,3}(?:,\d{3})+      # 1,234 / 12,345,678
        |
        \d+                     # plain run of digits
    )
    (?:\.\d+)?                  # optional decimal part
    %?                          # optional trailing percent
    (?!\w|\.\d)                 # right boundary: not a word-char and not the
                                # start of another decimal portion. A trailing
                                # sentence "." or comma IS allowed so "72.5."
                                # matches as 72.5, while "1.5.3" still does not
                                # match (avoids version-string partials).
    """,
    re.VERBOSE,
)

# Markdown / list scaffolding that we don't want to count as numeric claims.
# Matches "## 1.", "## 1)", "1.", "1)", at start-of-line or after whitespace.
_SCAFFOLD_RE = re.compile(r"(?m)^(\s*#+\s*\d+[.)]|\s*\d+[.)])(?=\s)")


@dataclass(frozen=True)
class HallucinationResult:
    """Outcome of one hallucination check.

    ``unsupported`` lists the canonicalised numbers from the thesis that did
    not appear in any report. Empty == clean. ``thesis_numbers`` and
    ``report_numbers`` are exposed for ``--explain`` so a failing run can
    show which token failed without re-extracting.
    """

    ok: bool
    unsupported: tuple[str, ...]
    thesis_numbers: frozenset[str]
    report_numbers: frozenset[str]

    def reason(self) -> str:
        if self.ok:
            return "clean"
        sample = ", ".join(self.unsupported[:8])
        more = "" if len(self.unsupported) <= 8 else f" (+{len(self.unsupported) - 8} more)"
        return f"unsupported: {sample}{more}"


def _strip_scaffold(text: str) -> str:
    """Remove leading list/heading numerals so they don't count as claims."""
    return _SCAFFOLD_RE.sub("", text)


def _canonicalise(token: str) -> str:
    """Normalise a numeric token to its value form.

    Strips leading ``$``, trailing ``%``, and comma thousand-separators —
    none of those are arithmetic. Decimal precision (trailing zeros) is
    preserved on purpose: rounding is arithmetic, so ``12.30`` ≠ ``12.3``.
    """
    cleaned = token.lstrip("+")
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    elif cleaned.startswith("-$"):
        # ``+$`` is unreachable here because ``lstrip("+")`` already removed
        # the leading ``+``; only the negative-sign-then-dollar form survives.
        cleaned = "-" + cleaned[2:]
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    return cleaned.replace(",", "")


def extract_numbers(text: str) -> frozenset[str]:
    """Return the set of canonicalised numeric tokens in ``text``.

    Public so tests can introspect what the regex saw and ``--explain`` can
    print it. Idempotent under canonicalisation: ``extract_numbers(text) ==
    extract_numbers(canonicalise_string(text))``.
    """
    stripped = _strip_scaffold(text)
    return frozenset(_canonicalise(m) for m in _NUMBER_RE.findall(stripped))


def _magnitude(token: str) -> str:
    """Drop a leading sign so ``-16.09`` and ``16.09`` compare equal.

    Used only for support comparisons in ``check`` — the canonical form
    returned by ``extract_numbers`` keeps the sign so callers (``--explain``,
    tests, future scorers) can still see exactly what was written. See the
    module docstring's "Sign-magnitude support" note for the trade-off.
    """
    return token[1:] if token.startswith(("-", "+")) else token


def check(thesis: str, reports: Iterable[str]) -> HallucinationResult:
    """Return a ``HallucinationResult`` for one (thesis, reports) pair.

    The reports iterable is consumed once into a single corpus before
    extraction so a number that appears in any report (regardless of which)
    is considered supported. Support is compared at the magnitude level
    (sign stripped) — see the module docstring for why.
    """
    thesis_nums = extract_numbers(thesis)
    corpus = "\n".join(reports)
    report_nums = extract_numbers(corpus)
    report_magnitudes = frozenset(_magnitude(n) for n in report_nums)
    unsupported = tuple(sorted(n for n in thesis_nums if _magnitude(n) not in report_magnitudes))
    return HallucinationResult(
        ok=not unsupported,
        unsupported=unsupported,
        thesis_numbers=thesis_nums,
        report_numbers=report_nums,
    )


__all__ = [
    "HallucinationResult",
    "check",
    "extract_numbers",
]
