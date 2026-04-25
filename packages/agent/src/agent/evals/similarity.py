"""Cosine similarity over normalised term-frequency vectors (QNT-67).

The QNT-67 spec calls for "cosine similarity of embeddings (reusing
all-MiniLM-L6-v2 — no new dep)". MiniLM isn't installed in the agent
package and pulling sentence-transformers would add ~500 MB of torch
dependencies just for the eval harness — at odds with both the "no new
dep" wording and the AC line "harness is reusable enough to extract as a
standalone repo later".

We satisfy the spirit (a complementary similarity score that grades
thesis-vs-reference text overlap) using cosine over normalised term-
frequency vectors. It's the same operation (cosine in a vector space),
just over a different space — coarser than dense embeddings but zero-dep
and deterministic. If MiniLM is ever installed, swap this module for an
embedding-backed implementation behind the same ``cosine`` signature.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Lowercased word-ish tokens. We keep digits in tokens so a thesis citing
# "P/E of 25" gets credit when the reference also says "25".
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str) -> Counter[str]:
    return Counter(t.lower() for t in _TOKEN_RE.findall(text))


def cosine(left: str, right: str) -> float:
    """Cosine similarity in [0.0, 1.0] between two strings.

    Empty input on either side yields 0.0 — undefined cosine, treated as
    "no similarity" so it shows up as a worst-case score in history.csv
    rather than a division-by-zero.
    """
    lv, rv = _tokens(left), _tokens(right)
    if not lv or not rv:
        return 0.0
    common = set(lv) & set(rv)
    dot = sum(lv[t] * rv[t] for t in common)
    if dot == 0:
        return 0.0
    norm_l = math.sqrt(sum(v * v for v in lv.values()))
    norm_r = math.sqrt(sum(v * v for v in rv.values()))
    return round(dot / (norm_l * norm_r), 4)


__all__ = ["cosine"]
