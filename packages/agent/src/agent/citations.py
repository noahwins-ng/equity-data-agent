"""Retrieved-source citation-anchor integrity (QNT-305).

QNT-301 shipped claim-anchored retrieved-source citations: a claim drawing on a
folded retrieved hit cites it as ``(source: news R1)`` (or a bare ``[R1]`` tag
in the narrate voice), and the frontend renders that ``Rn`` as a chip that
scrolls to the matching retrieved-sources row (``data-source-id=Rn``). The ids
run ``R1..R{n}`` where ``n`` is the number of rows actually retrieved this turn.

Live prod traces show the synthesis model fabricates OUT-OF-RANGE ids -- it
cites an ``Rn`` larger than the number of rows retrieved (report carried only
``[R1]``/``[R2]`` but the answer cited ``R5``/``R11``). Those ids point at no
row: the chip dangles (a silent no-op click) and, worse, it is a fake footnote
that undermines the one thing the citation layer exists to prove (ADR-003: the
agent only quotes, never computes).

The fix is deterministic, not a prompt-only nudge. This module is the single
Python source of truth for both boundaries:

* :func:`strip_oob_anchors` / :func:`strip_oob_anchors_in_obj` -- the backend
  strip applied to every card payload before the SSE emit.
* :func:`find_oob_anchor_ids` -- the deterministic eval-path check that flags
  any answer citing an out-of-range id.

The frontend carries a mirror of the strip in ``prose-parse.ts`` (defense in
depth for the streamed narrate bubble, which is not a card payload).
"""

from __future__ import annotations

import re
from typing import Any

# A retrieved-source anchor in either shape the prompts / narrate voice emit:
#   (source: news R5)   -- the ``src`` group holds the source label
#   [R5]                -- a bare tag (narrate voice); no source label
# The source class mirrors the frontend chip pattern (letters + the multi-source
# separators ``|`` ``,`` + whitespace) so a multi-source citation still parses.
# The id must sit right before the closing paren -- ``(source: a, b R1)``, one id
# per citation, matching what every prompt emits. A hypothetical per-source form
# (``(source: a R1, b R2)``) is intentionally not matched; if a prompt ever emits
# that shape, extend this pattern rather than relying on the fall-through.
# The bare form optionally captures ONE preceding space (``bsp``) so removing an
# out-of-range tag also swallows the space it sat behind -- no ``a tag .`` orphan
# before punctuation (mirrors the frontend's trailing-space trim on drop).
_ANCHOR_RE = re.compile(
    r"\(source:\s*(?P<src>[A-Za-z|,\s]+?)\s+R(?P<sid>\d+)\)|(?P<bsp> )?\[R(?P<bid>\d+)\]"
)


def find_oob_anchor_ids(text: str, max_id: int) -> list[int]:
    """Return every retrieved-source id cited in ``text`` that exceeds ``max_id``.

    ``max_id`` is the count of retrieved-sources rows for the turn (ids run
    ``R1..R{max_id}``); ``0`` when no rows were retrieved, so any id is out of
    range. In-range ids and canned (id-less) citations are ignored. Duplicates
    are preserved so a caller can count occurrences.
    """
    oob: list[int] = []
    for m in _ANCHOR_RE.finditer(text):
        n = int(m.group("sid") or m.group("bid"))
        if n > max_id:
            oob.append(n)
    return oob


def strip_oob_anchors(text: str, max_id: int) -> str:
    """De-anchor any out-of-range retrieved-source citation in ``text``.

    * ``(source: news R5)`` with ``R5`` out of range -> ``(source: news)``: keep
      the source attribution, drop only the fabricated row id.
    * a bare ``[R5]`` tag out of range -> removed entirely, along with the one
      space it sat behind (there is no source label to fall back to).

    In-range ids and canned citations pass through untouched.
    """

    def repl(m: re.Match[str]) -> str:
        src = m.group("src")
        if src is not None:  # (source: name Rn) form
            if int(m.group("sid")) > max_id:
                return f"(source: {src.strip()})"
            return m.group(0)
        # bare [Rn] tag (``bsp`` = the captured preceding space, if any)
        if int(m.group("bid")) > max_id:
            return ""
        return m.group(0)

    return _ANCHOR_RE.sub(repl, text)


def strip_oob_anchors_in_obj(obj: Any, max_id: int) -> Any:
    """Recursively apply :func:`strip_oob_anchors` to every string in ``obj``.

    ``obj`` is a card payload dict (``model_dump()``); anchors can appear in any
    of its prose fields regardless of the schema shape, so we walk the whole
    tree rather than naming fields per card.
    """
    if isinstance(obj, str):
        return strip_oob_anchors(obj, max_id)
    if isinstance(obj, list):
        return [strip_oob_anchors_in_obj(v, max_id) for v in obj]
    if isinstance(obj, dict):
        return {k: strip_oob_anchors_in_obj(v, max_id) for k, v in obj.items()}
    return obj


__all__ = [
    "find_oob_anchor_ids",
    "strip_oob_anchors",
    "strip_oob_anchors_in_obj",
]
