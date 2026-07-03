"""Retrieved-source citation-anchor integrity (QNT-305 + corpus follow-up).

QNT-301 shipped claim-anchored retrieved-source citations: a claim drawing on a
folded retrieved hit cites it as ``(source: news R1)`` (or a bare ``[R1]`` tag in
the narrate voice), and the frontend renders that ``Rn`` as a chip that scrolls
to the matching retrieved-sources row. The ids run ``R1..R{n}`` where ``n`` is
the number of rows retrieved this turn, and each row carries the ``corpus`` it
came from (``news`` or ``earnings``).

A retrieved anchor is only trustworthy when BOTH hold:

* **in range** -- ``Rk`` with ``k <= n`` (QNT-305: the model fabricates ids past
  the row count -- a fake footnote pointing at no row); and
* **corpus-consistent** -- the source NAME must match the corpus of that id. A
  news-corpus hit folds into the ``news`` report, an earnings-corpus hit into the
  ``fundamental`` report (QNT-263), so ``news Rk`` is valid only when row ``Rk``
  is a news hit and ``fundamental Rk`` only when it is an earnings hit. The model
  also mis-staples an in-range news id onto a canned fundamental figure
  (``fundamental R1`` where R1 is a news headline) -- in range, so QNT-305's
  count check misses it, but the anchor still points at an unrelated row.

Both checks live here, the single Python source of truth for both boundaries:

* :func:`strip_bad_anchors` / :func:`strip_bad_anchors_in_obj` -- the backend
  strip applied to every card payload before the SSE emit.
* :func:`find_bad_anchors` -- the deterministic eval-path check.

The frontend carries a mirror in ``prose-parse.ts`` (defense in depth for the
streamed narrate bubble, which is not a card payload).

A row with no ``corpus`` tag falls back to the range check only, so callers with
corpus-less rows (older data, stubbed tests) keep the QNT-305 behaviour.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# A retrieved-source anchor in either shape the prompts / narrate voice emit:
#   (source: news R5)   -- the ``src`` group holds the source label
#   [R5]                -- a bare tag (narrate voice); no source label
# The source class mirrors the frontend chip pattern (letters + the multi-source
# separators ``|`` ``,`` + whitespace) so a multi-source citation still parses.
# The id must sit right before the closing paren -- one id per citation, matching
# what every prompt emits. The bare form optionally captures ONE preceding space
# (``bsp``) so removing an out-of-range tag also swallows the space it sat behind
# -- no ``a tag .`` orphan before punctuation (mirrors the frontend's
# trailing-space trim on drop).
_ANCHOR_RE = re.compile(
    r"\(source:\s*(?P<src>[A-Za-z|,\s]+?)\s+R(?P<sid>\d+)\)|(?P<bsp> )?\[R(?P<bid>\d+)\]"
)

# A retrieved hit folds into a report by corpus, so an anchor's source NAME
# implies which corpus its id must belong to. ``technical`` / ``company`` are
# never fed by retrieval, so an id on them is never valid.
_NAME_CORPUS: dict[str, str] = {"news": "news", "fundamental": "earnings"}


def _corpus_at(id_num: int, sources: Sequence[Mapping[str, Any]]) -> str | None:
    """Corpus of the ``R{id_num}`` row, or None when out of range / untagged.

    Positional: ids are ``R1..Rn`` in retrieved order (contiguity invariant), so
    row ``Rk`` is ``sources[k - 1]`` -- robust to rows that omit the ``id`` key.
    """
    if id_num < 1 or id_num > len(sources):
        return None
    row = sources[id_num - 1]
    corpus = row.get("corpus") if isinstance(row, Mapping) else None
    return corpus if isinstance(corpus, str) and corpus else None


def _anchor_is_valid(source_name: str, id_num: int, sources: Sequence[Mapping[str, Any]]) -> bool:
    """True when a retrieved anchor may keep its id: in range AND corpus-consistent.

    A bare tag (empty ``source_name``) or a row with no corpus tag is checked on
    range alone. When both a source name and a corpus are present, at least one
    named source must map to that corpus (``news``/``fundamental``; a multi-source
    citation keeps the id if any of its names matches).
    """
    if id_num < 1 or id_num > len(sources):
        return False
    corpus = _corpus_at(id_num, sources)
    if corpus is None or not source_name.strip():
        return True
    names = [n.strip().lower() for n in re.split(r"[|,]", source_name)]
    return any(_NAME_CORPUS.get(n) == corpus for n in names)


def find_bad_anchors(text: str, sources: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return every retrieved anchor in ``text`` that is out of range or points at
    the wrong corpus, rendered as it was cited (``"fundamental R1"``, ``"R11"``).

    Deterministic (no LLM), so it runs on every prod chat as a regression guard.
    An empty list means every cited anchor is trustworthy (or none was cited).
    """
    bad: list[str] = []
    for m in _ANCHOR_RE.finditer(text):
        src = m.group("src")
        if src is not None:
            if not _anchor_is_valid(src, int(m.group("sid")), sources):
                bad.append(f"{src.strip()} R{m.group('sid')}")
        elif not _anchor_is_valid("", int(m.group("bid")), sources):
            bad.append(f"R{m.group('bid')}")
    return bad


def strip_bad_anchors(text: str, sources: Sequence[Mapping[str, Any]]) -> str:
    """De-anchor any out-of-range OR corpus-mismatched retrieved citation.

    * ``(source: news R5)`` (out of range) or ``(source: fundamental R1)`` (R1 is
      a news row) -> ``(source: news)`` / ``(source: fundamental)``: keep the
      source attribution, drop only the bad row id.
    * a bare ``[R5]`` tag out of range -> removed entirely, with the one space it
      sat behind (there is no source label to fall back to).

    Valid anchors (in range + corpus-consistent) pass through untouched.
    """

    def repl(m: re.Match[str]) -> str:
        src = m.group("src")
        if src is not None:  # (source: name Rk) form
            if not _anchor_is_valid(src, int(m.group("sid")), sources):
                return f"(source: {src.strip()})"
            return m.group(0)
        # bare [Rk] tag -- range-only (no source name to check corpus against)
        if not _anchor_is_valid("", int(m.group("bid")), sources):
            return ""
        return m.group(0)

    return _ANCHOR_RE.sub(repl, text)


def strip_bad_anchors_in_obj(obj: Any, sources: Sequence[Mapping[str, Any]]) -> Any:
    """Recursively apply :func:`strip_bad_anchors` to every string in ``obj``.

    ``obj`` is a card payload dict (``model_dump()``); anchors can appear in any
    of its prose fields regardless of the schema shape, so we walk the whole tree
    rather than naming fields per card.
    """
    if isinstance(obj, str):
        return strip_bad_anchors(obj, sources)
    if isinstance(obj, list):
        return [strip_bad_anchors_in_obj(v, sources) for v in obj]
    if isinstance(obj, dict):
        return {k: strip_bad_anchors_in_obj(v, sources) for k, v in obj.items()}
    return obj


__all__ = [
    "find_bad_anchors",
    "strip_bad_anchors",
    "strip_bad_anchors_in_obj",
]
