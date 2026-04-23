"""Drop the Qdrant ``equity_news`` collection so the next ``news_embeddings``
tick re-creates it with the QNT-120 namespaced point-ID scheme.

The ID change from ``blake2b(url)`` to ``blake2b(f"{ticker}:{url_id}")`` is
**not backwards-compatible** — old points use the un-namespaced ID so they
remain in place after the fix deploys and orphan the collection. Option 2
from QNT-120 (drop + recreate) avoids the 7-day orphan window; this script
performs that drop against whichever environment's Qdrant Cloud credentials
are in scope.

Run with explicit confirmation (destructive — drops every vector in the
collection):

    uv run --package dagster-pipelines python scripts/drop_qdrant_news_collection.py --yes

Without ``--yes`` the script exits 2 without contacting Qdrant, so a stray
invocation in the wrong shell can't silently nuke the collection. Requires
``QDRANT_URL`` and ``QDRANT_API_KEY`` in the environment (loaded from
``.env`` by ``shared.config.settings``). Idempotent — deleting a non-existent
collection is a no-op.
"""

from __future__ import annotations

import argparse
import sys

from qdrant_client import QdrantClient
from shared.config import settings

COLLECTION = "equity_news"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"Confirm destructive drop of Qdrant collection {COLLECTION!r}.",
    )
    args = parser.parse_args(argv)

    if not args.yes:
        print(
            f"Refusing to drop {COLLECTION!r} without --yes. "
            "This is a destructive, one-shot migration.",
            file=sys.stderr,
        )
        return 2

    if not settings.QDRANT_URL or not settings.QDRANT_API_KEY:
        print("QDRANT_URL / QDRANT_API_KEY not set — aborting.", file=sys.stderr)
        return 1

    client = QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
        timeout=30,
    )

    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION not in existing:
        print(f"Collection {COLLECTION!r} not present — nothing to drop.")
        return 0

    client.delete_collection(collection_name=COLLECTION)
    print(
        f"Dropped collection {COLLECTION!r}. "
        f"Next news_embeddings tick will recreate it via ensure_collection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
