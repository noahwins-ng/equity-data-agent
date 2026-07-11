"""Numeric-claim hallucination detector (QNT-67, eval type (a)).

For every thesis the agent produces, regex every numeric claim out of the
text and assert each appears in the report strings the agent received as
tool output. Any mismatch is a hallucination — the direct operational
enforcement of ADR-003.

Verbatim vs. value-equivalent:
    The AC says "verbatim". We canonicalise away pure formatting differences
    (leading ``$``, trailing ``%``, comma thousand-separators, trailing
    fractional zeros, and a glued magnitude unit — ``$2.5T``/``$14B``/``20k``)
    so a thesis that writes ``$1,234`` is accepted when the report wrote
    ``1234`` — that is formatting, not arithmetic. Trailing fractional zeros
    are formatting too (QNT-361): ``16.60`` and ``16.6`` are the same value,
    so both canonicalise to ``16.6``. This is exact value equality, NOT
    rounding tolerance — a thesis that writes ``19.4`` against a report that
    wrote ``19.36`` IS still flagged, because changing the value is rounding
    and rounding is arithmetic.

    Magnitude units (QNT-255 follow-up): news reports quote market caps and
    deal sizes with a glued scale suffix (``Breaches $2.5T``, ``$14B AI
    Push``). The model expands these to ``$2.5 trillion`` / ``$14 billion``,
    writing the bare mantissa. The report's ``$2.5T`` token did not extract a
    ``2.5`` (the right-boundary rejected the glued ``T``), so the grounded
    mantissa read as unsupported — a false positive that flagged clean
    TSLA/META news answers (the QNT-255 clean-window finding). ``_canonicalise``
    strips the unit symmetrically (like ``%``) so both forms reduce to the bare
    mantissa ``14`` for value-equality.

    Scale-aware support (QNT-297): stripping the unit for value-equality would,
    on its own, collapse SCALE — an answer's ``$5M`` counted as supported by a
    report's ``$5B`` (both → ``5``). A misquoted scale is a wrong magnitude, not
    idiom, and for a finance product it is the most screenshot-able numeric
    error the agent could ship, so — unlike the sign / window blind spots below
    — we do NOT accept it. ``check`` compares ``(mantissa, scale)`` pairs
    (``_extract_scaled``): a spelled-out scale word immediately after a number
    (``$2.5 trillion``) is folded onto the mantissa so it still matches a
    report's ``$2.5T``, and a bare mantissa with no scale in either text
    compares exactly as before. Only a *positive* conflict flags — the answer
    says ``M`` while every report occurrence of that mantissa says ``B``. A
    claimed bare mantissa still matches any report scale, and a report bare
    mantissa (no glued unit) supports any claimed scale (no evidence of a
    mismatch), so the QNT-255 clean-window fix is preserved.

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
    * Time-window labels the model expands from the report's compact period
      forms — ``5-year low`` (report writes ``5y``), ``52-week high``,
      ``200-day`` — see ``_strip_period_idiom``. The number is an English
      window label, not a claim. Stripped symmetrically from thesis and
      reports, so we are blind to a *fabricated* window ("10-year low" when
      the report said ``5y``); we accept that false-negative for the same
      reason as the sign-magnitude blind spot — a misquoted window is not
      arithmetic, and the per-section structure surfaces gross errors at a
      higher level. (Real TSLA trace ``b7c2187``, 2026-06-09: the report's
      ``near 5y low`` became ``near the 5-year low`` in the answer; the bare
      ``5`` was counted as unsupported and dropped grounding 1.0 → 0.91.)
    * Month-name dates the model paraphrases from the report's ISO form —
      ``July 9`` (report writes ``2026-07-09``) — see ``_strip_date_idiom``.
      Same symmetric-strip blind spot as the window labels: a *fabricated*
      date is not caught. (Real AMD trace ``d59d146f``, 2026-07-11: the
      report's ``2026-07-09`` became ``surged 5.8% on July 9`` and the bare
      ``9`` counted as unsupported.)

False-positive risk:
    Single-digit integers like ``5`` or ``7`` that the model uses as a
    rhetorical count ("the past 7 days"). We accept those FPs as a price
    for catching real hallucinations — investigate via ``--explain``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

# Match: optional sign, optional $, digits with optional comma-thousands or
# plain run, optional decimal portion, optional trailing %, optional glued
# magnitude unit.
# Examples that match: 12, 12.3, 1,234, $1,234.56, 25%, -3.5%, +0.42, $2.5T, $14B
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
    (?:bn|tn|mn|[kmbt])?        # optional glued magnitude unit ($2.5T, $14B,
                                # 20k) — a scale label, not arithmetic. Stripped
                                # in _canonicalise so report "$14B" and a thesis
                                # "$14 billion" both reduce to "14". IGNORECASE.
    (?!\w|\.\d)                 # right boundary: not a word-char and not the
                                # start of another decimal portion. A trailing
                                # sentence "." or comma IS allowed so "72.5."
                                # matches as 72.5, while "1.5.3" still does not
                                # match (avoids version-string partials).
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Markdown / list scaffolding that we don't want to count as numeric claims.
# Matches "## 1.", "## 1)", "1.", "1)", at start-of-line or after whitespace.
_SCAFFOLD_RE = re.compile(r"(?m)^(\s*#+\s*\d+[.)]|\s*\d+[.)])(?=\s)")

# Time-window idioms ("5-year low", "52-week high", "200-day average"). The
# number is part of an English window label, not a numeric claim — see the
# module docstring's "Numbers we deliberately ignore" note. The unit list is
# intentionally closed to period words so genuine hyphenated multiples like
# "20-times earnings" still count.
_PERIOD_IDIOM_RE = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?[-\s](?:year|yr|day|week|wk|month|mo|quarter|qtr)s?\b",
    re.IGNORECASE,
)

# Month-name date idioms ("July 9", "Jan 15th", "September 15, 2026", "9
# July"). The day (and year) are part of an English date label, not numeric
# claims (QNT-361 follow-up 3): reports print dates in ISO form (2026-07-09),
# the model paraphrases to English, and the bare day leaked through as an
# unsupported number (real AMD trace d59d146f, 2026-07-11: "surged 5.8% on
# July 9" flagged "9", dropping grounding 1.0 → 0.78 alongside one real
# catch). Stripped symmetrically like the period idiom, with the same
# accepted blind spot: a FABRICATED date is not caught. The month list is
# closed so a modal "may" needs an adjacent bare number to false-fire —
# ungrammatical in practice. The day is capped at two digits and must not
# open a decimal or percentage, so "May 5.2%" still counts its number.
_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_IDIOM_RE = re.compile(
    rf"""
    \b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?(?![\d.%])  # July 9 / Jan 15th
    |
    (?<![\w.])\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\b(?:,?\s+\d{{4}})?  # 9 July / 15 Sept 2026
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Magnitude unit glued to a number ($2.5T, $14B, 20k). The (?<=\d) lookbehind
# ties it to the digits so a bare "K"/"M" is never stripped. Kept in sync with
# the unit alternation inside _NUMBER_RE.
_MAGNITUDE_UNIT_RE = re.compile(r"(?<=\d)(?:bn|tn|mn|[kmbt])$", re.IGNORECASE)

# Canonical scale letter for every glued-unit form _NUMBER_RE can match
# (QNT-297). Keys are lower-cased; two-letter forms come first so a matched
# "bn"/"tn"/"mn" is never mis-read as a bare "b"/"t"/"m".
_SCALE_MAP = {"k": "K", "m": "M", "mn": "M", "b": "B", "bn": "B", "t": "T", "tn": "T"}

# Spelled-out scale word immediately after a number ("$2.5 trillion", "$14
# billion"). Folded onto the mantissa as the glued single-letter suffix before
# extraction so the answer's expanded form tags the same scale as a report's
# compact "$2.5T". The (?<=\d) lookbehind ties the word to the preceding
# number; trailing plural "s" is tolerated ("2.5 trillions").
_SPELLED_SCALE_RE = re.compile(r"(?<=\d)\s+(thousand|million|billion|trillion)s?\b", re.IGNORECASE)
_SPELLED_MAP = {"thousand": "K", "million": "M", "billion": "B", "trillion": "T"}


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


def _strip_period_idiom(text: str) -> str:
    """Remove time-window labels ("5-year", "52-week") before extraction.

    Replaces with a space so the surrounding text never merges into a new
    token. See the module docstring for the symmetric-strip blind spot.
    """
    return _PERIOD_IDIOM_RE.sub(" ", text)


def _strip_date_idiom(text: str) -> str:
    """Remove month-name dates ("July 9", "15 Sept 2026") before extraction.

    Replaces with a space so the surrounding text never merges into a new
    token. Symmetric strip, same blind spot as the period idiom: a
    fabricated date is not caught — see the ``_DATE_IDIOM_RE`` comment.
    """
    return _DATE_IDIOM_RE.sub(" ", text)


def _glue_spelled_scale(text: str) -> str:
    """Fold a spelled-out scale word onto the number before it (QNT-297).

    ``$2.5 trillion`` → ``$2.5T``, ``$14 billion`` → ``$14B`` so the answer's
    expanded form carries the same glued scale a report writes compactly. A
    no-op for ``extract_numbers``' bare-value output (``$2.5T`` still
    canonicalises to ``2.5``); it is what lets ``_extract_scaled`` tag the
    scale ``T`` on the answer side.
    """
    return _SPELLED_SCALE_RE.sub(lambda m: _SPELLED_MAP[m.group(1).lower()], text)


def _prepare(text: str) -> str:
    """Shared pre-extraction pipeline: strip scaffold + window idiom, then
    fold spelled-out scale words. Used by both ``extract_numbers`` and
    ``_extract_scaled`` so the two see the same token stream."""
    return _glue_spelled_scale(_strip_date_idiom(_strip_period_idiom(_strip_scaffold(text))))


def _canonicalise(token: str) -> str:
    """Normalise a numeric token to its value form.

    Strips leading ``$``, trailing ``%``, a glued magnitude unit
    (``$2.5T`` → ``2.5``, ``$14B`` → ``14``, ``20k`` → ``20``), comma
    thousand-separators, and trailing fractional zeros (``16.60`` → ``16.6``,
    ``12.30`` → ``12.3``, ``5.00`` → ``5``) — none of those are arithmetic
    (QNT-361). Genuine rounding still flags: ``19.36`` and ``19.4`` are
    different values and stay distinct.
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
    # A magnitude unit glued to the digits ($2.5T / $14B / 20k) is a scale
    # label, like %/$ — drop it so the report's "$14B" and a thesis's expanded
    # "$14 billion" both reduce to "14" for value-equality. The (?<=\d) guard
    # keeps it tied to a number. This drops the scale from the bare value on
    # purpose; the scale itself is recovered separately by _split_scale and
    # checked for conflicts in ``check`` (QNT-297), so "$5M" is NOT accepted
    # against a report "$5B".
    cleaned = _MAGNITUDE_UNIT_RE.sub("", cleaned)
    cleaned = cleaned.replace(",", "")
    # Trailing fractional zeros are formatting, not value (QNT-361): report
    # "16.60" and thesis "16.6" are the same number. Guarded on the decimal
    # point so integer zeros ("20", "100") are never touched. Applies to ANY
    # decimal token, not just percentages — still exact value equality, and
    # price/ratio surfaces render at 2dp so it rarely fires off-percentage.
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned


def _split_scale(token: str) -> tuple[str, str | None]:
    """Split a raw numeric token into ``(canonical_value, scale)`` (QNT-297).

    ``canonical_value`` is the ``_canonicalise`` form (sign preserved, like
    ``extract_numbers``); ``scale`` is the canonical letter for a glued
    magnitude unit (``$2.5T`` → ``T``, ``$14B`` → ``B``, ``20k`` → ``K``) or
    ``None`` when the token carried no unit. The unit is read off the ORIGINAL
    token before ``_canonicalise`` strips it, so the two stay in sync.
    """
    unit = _MAGNITUDE_UNIT_RE.search(token)
    scale = _SCALE_MAP[unit.group(0).lower()] if unit else None
    return _canonicalise(token), scale


def _extract_pairs(text: str) -> frozenset[tuple[str, str | None]]:
    """Return the ``(canonical_value, scale)`` pairs in ``text`` in one pass.

    The single extraction primitive: ``extract_numbers`` drops the scale and
    ``_extract_scaled`` sign-strips the value, but both derive from this so
    ``check`` runs ``_prepare`` + ``_NUMBER_RE`` once per text, not twice.
    """
    return frozenset(_split_scale(m) for m in _NUMBER_RE.findall(_prepare(text)))


def extract_numbers(text: str) -> frozenset[str]:
    """Return the set of canonicalised numeric tokens in ``text``.

    Public so tests can introspect what the regex saw and ``--explain`` can
    print it. Idempotent under canonicalisation: ``extract_numbers(text) ==
    extract_numbers(canonicalise_string(text))``.
    """
    return frozenset(value for value, _ in _extract_pairs(text))


def _extract_scaled(text: str) -> frozenset[tuple[str, str | None]]:
    """Return the ``(magnitude, scale)`` pairs in ``text`` (sign stripped).

    Same token stream as ``extract_numbers``, but each token keeps its scale
    tag instead of collapsing to the bare mantissa — the input to ``check``'s
    scale-conflict rule.
    """
    return frozenset((_magnitude(value), scale) for value, scale in _extract_pairs(text))


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

    On top of magnitude support, a claimed scale that positively conflicts
    with the report is flagged (QNT-297): the answer says ``M`` while every
    report occurrence of that mantissa says ``B``. A bare claimed mantissa, or
    a report mantissa written bare (no glued unit), never conflicts — that
    preserves the QNT-255 clean-window fix.
    """
    # One extraction pass per text; every view below is derived from the pairs.
    thesis_pairs = _extract_pairs(thesis)
    report_pairs = _extract_pairs("\n".join(reports))
    thesis_nums = frozenset(value for value, _ in thesis_pairs)
    report_nums = frozenset(value for value, _ in report_pairs)
    report_magnitudes = frozenset(_magnitude(value) for value in report_nums)

    # Scales each magnitude was written with (report) vs claimed (thesis).
    report_scales: dict[str, set[str | None]] = defaultdict(set)
    for value, scale in report_pairs:
        report_scales[_magnitude(value)].add(scale)
    claimed_scales: dict[str, set[str]] = defaultdict(set)
    for value, scale in thesis_pairs:
        if scale is not None:
            claimed_scales[_magnitude(value)].add(scale)

    unsupported_set: set[str] = set()
    for n in thesis_nums:
        magnitude = _magnitude(n)
        if magnitude not in report_magnitudes:
            unsupported_set.add(n)  # value absent from every report
            continue
        claimed = claimed_scales.get(magnitude)
        if not claimed:
            continue  # bare claim → any report scale supports (QNT-255)
        report_scale_set = report_scales.get(magnitude, set())
        if None in report_scale_set:
            continue  # report wrote a bare number → no evidence of mismatch
        if claimed & report_scale_set:
            continue  # a claimed scale matches a report scale
        unsupported_set.add(n)  # positive scale conflict (answer M, report B)
    unsupported = tuple(sorted(unsupported_set))
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
