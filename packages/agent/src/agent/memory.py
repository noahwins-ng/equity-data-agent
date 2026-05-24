"""Session-memory sidecar table + TTL prune (QNT-209).

LangGraph's ``SqliteSaver`` schema (``checkpoints``, ``writes``) has no
reliable last-used-at column we can lean on for cleanup. Owning a sidecar
table avoids coupling to checkpoint_id internals.

The chat handler calls :func:`touch_thread` on every request. The api
lifespan task calls :func:`prune_stale_threads` once per
``AGENT_THREAD_PRUNE_INTERVAL_SECONDS`` to drop threads whose ``last_seen``
is older than ``AGENT_THREAD_TTL_DAYS``. Pruning deletes the matching rows
from ``checkpoints`` AND ``writes`` AND ``thread_last_seen`` (sidecar
deleted LAST so the other two can still resolve thread_ids).
"""

from __future__ import annotations

import sqlite3


def init_thread_metadata(conn: sqlite3.Connection) -> None:
    """Create the ``thread_last_seen`` sidecar table if it doesn't exist.

    Idempotent — safe to call on every app startup.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_last_seen (
            thread_id TEXT PRIMARY KEY,
            last_seen INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def touch_thread(conn: sqlite3.Connection, thread_id: str) -> None:
    """Upsert ``thread_id``'s ``last_seen`` to the current epoch second."""
    conn.execute(
        "INSERT OR REPLACE INTO thread_last_seen (thread_id, last_seen) "
        "VALUES (?, strftime('%s','now'))",
        (thread_id,),
    )
    conn.commit()


def prune_stale_threads(conn: sqlite3.Connection, ttl_days: int) -> int:
    """Delete checkpoints, writes, and sidecar rows older than ``ttl_days``.

    ``ttl_days`` is interpreted as ``now - ttl_days * 86400``. ttl_days=0
    wipes everything that has any last_seen row (matches the AC12 ``ttl_days=0``
    semantics — "stale immediately" rather than "keep nothing if older
    than 0 seconds").

    Returns the count of thread_ids pruned (one row per stale thread in
    ``thread_last_seen``). The cascaded deletes against the LangGraph
    tables happen first so the sidecar can still resolve thread_ids when
    they fire.
    """
    cutoff_sql = "strftime('%s','now') - ?"
    cutoff_seconds = ttl_days * 86_400
    stale_subselect = f"SELECT thread_id FROM thread_last_seen WHERE last_seen < ({cutoff_sql})"
    # Cascaded deletes first; sidecar last so the subselect can still resolve.
    conn.execute(
        f"DELETE FROM checkpoints WHERE thread_id IN ({stale_subselect})",
        (cutoff_seconds,),
    )
    conn.execute(
        f"DELETE FROM writes WHERE thread_id IN ({stale_subselect})",
        (cutoff_seconds,),
    )
    cur = conn.execute(
        f"DELETE FROM thread_last_seen WHERE last_seen < ({cutoff_sql})",
        (cutoff_seconds,),
    )
    pruned = cur.rowcount
    conn.commit()
    return pruned if pruned is not None else 0


__all__ = ["init_thread_metadata", "prune_stale_threads", "touch_thread"]
