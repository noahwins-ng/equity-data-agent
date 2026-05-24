"""Tests for agent.memory (QNT-209).

Covers the sidecar thread_last_seen table + the prune cascade (deletes
matching rows from checkpoints, writes, AND thread_last_seen — leaving
unrelated thread_ids untouched). The AC13 prune-loop integration test
exercises the asyncio loop in api.main against a tmp SqliteSaver to
prove the loop actually fires + logs + deletes within the configured
interval.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from agent.memory import init_thread_metadata, prune_stale_threads, touch_thread
from shared.config import settings


def _setup_db() -> sqlite3.Connection:
    """Build an in-memory SQLite with the LangGraph + sidecar tables.

    Mirrors SqliteSaver.setup() — we don't import SqliteSaver here because
    the prune SQL is the unit under test and a real saver isn't needed to
    exercise the DELETE cascade.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            checkpoint BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        CREATE TABLE writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            value BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        );
        """
    )
    init_thread_metadata(conn)
    return conn


def _insert_checkpoint(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) VALUES (?, ?, ?)",
        (thread_id, "cp1", b"x"),
    )
    conn.execute(
        "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel, value) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, "cp1", "t1", 0, "messages", b"x"),
    )
    conn.commit()


def test_init_thread_metadata_idempotent() -> None:
    """Calling init twice must not raise — startup runs it every boot."""
    conn = sqlite3.connect(":memory:")
    init_thread_metadata(conn)
    init_thread_metadata(conn)
    # Sidecar table exists and is queryable.
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert ("thread_last_seen",) in rows


def test_touch_thread_upserts_last_seen() -> None:
    conn = sqlite3.connect(":memory:")
    init_thread_metadata(conn)
    touch_thread(conn, "session:TSLA")
    first = conn.execute(
        "SELECT last_seen FROM thread_last_seen WHERE thread_id = ?", ("session:TSLA",)
    ).fetchone()
    assert first is not None
    # A second touch updates rather than duplicating the row.
    time.sleep(1)
    touch_thread(conn, "session:TSLA")
    rows = conn.execute(
        "SELECT thread_id, last_seen FROM thread_last_seen WHERE thread_id = ?",
        ("session:TSLA",),
    ).fetchall()
    assert len(rows) == 1
    # The second touch's last_seen is >= the first (monotonic non-decreasing).
    assert rows[0][1] >= first[0]


def test_prune_ttl_zero_wipes_everything() -> None:
    """ttl_days=0 ⇒ now - 0 cutoff; every row whose last_seen <= cutoff is
    pruned. We backdate so the equality boundary is well-defined."""
    conn = _setup_db()
    touch_thread(conn, "session:TSLA")
    _insert_checkpoint(conn, "session:TSLA")
    # Backdate by 1 day so "last_seen < (now - 0)" is true.
    conn.execute(
        "UPDATE thread_last_seen SET last_seen = strftime('%s','now') - 86400 WHERE thread_id = ?",
        ("session:TSLA",),
    )
    conn.commit()

    pruned = prune_stale_threads(conn, ttl_days=0)
    assert pruned == 1
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM thread_last_seen").fetchone()[0] == 0


def test_prune_keeps_fresh_rows() -> None:
    """ttl_days=7, row touched now ⇒ no pruning."""
    conn = _setup_db()
    touch_thread(conn, "session:NVDA")
    _insert_checkpoint(conn, "session:NVDA")
    pruned = prune_stale_threads(conn, ttl_days=7)
    assert pruned == 0
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM thread_last_seen").fetchone()[0] == 1


def test_prune_selective() -> None:
    """Mix of stale + fresh: only the stale thread's rows are removed across
    checkpoints AND writes AND thread_last_seen."""
    conn = _setup_db()
    # Fresh thread (touched now).
    touch_thread(conn, "session:NVDA")
    _insert_checkpoint(conn, "session:NVDA")
    # Stale thread (backdated 10 days).
    touch_thread(conn, "session:TSLA")
    _insert_checkpoint(conn, "session:TSLA")
    conn.execute(
        "UPDATE thread_last_seen SET last_seen = strftime('%s','now') - (10 * 86400) "
        "WHERE thread_id = ?",
        ("session:TSLA",),
    )
    conn.commit()

    pruned = prune_stale_threads(conn, ttl_days=7)
    assert pruned == 1

    # Fresh thread untouched in all three tables.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", ("session:NVDA",)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM writes WHERE thread_id = ?", ("session:NVDA",)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thread_last_seen WHERE thread_id = ?", ("session:NVDA",)
        ).fetchone()[0]
        == 1
    )
    # Stale thread wiped from all three tables.
    for table in ("checkpoints", "writes", "thread_last_seen"):
        assert (
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", ("session:TSLA",)
            ).fetchone()[0]
            == 0
        )


# ─── AC13: prune-loop integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_loop_fires_and_deletes_stale_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC13: with TTL=0 and a 1s interval, the asyncio prune loop in
    api.main fires within the configured window, logs the pruned count,
    and removes stale rows from the sidecar.

    The docker-flavoured AC reads ``AGENT_THREAD_TTL_DAYS=0`` and
    ``AGENT_THREAD_PRUNE_INTERVAL_SECONDS=10`` then waits 30s — same
    semantic, faster cadence here.
    """
    from api import main as main_module
    from api.routers import agent_chat as chat_module

    # Override settings + reset the module-level checkpointer singleton so
    # the test's tmp_path is the file the saver opens.
    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(settings, "AGENT_DB_PATH", str(db_path))
    monkeypatch.setattr(settings, "AGENT_THREAD_TTL_DAYS", 0)
    monkeypatch.setattr(settings, "AGENT_THREAD_PRUNE_INTERVAL_SECONDS", 1)
    monkeypatch.setattr(chat_module, "_CHECKPOINTER_SINGLETON", None)
    monkeypatch.setattr(chat_module, "_CHECKPOINTER_CONN", None)

    # Build the saver via the same accessor the prune loop uses. This
    # also creates the LangGraph tables + the thread_last_seen sidecar.
    chat_module.get_checkpointer()
    conn = chat_module.get_checkpointer_conn()
    assert conn is not None

    # Stage a stale thread that ttl_days=0 will catch (backdated 1 day).
    touch_thread(conn, "ttl:smoke")  # type: ignore[arg-type]
    conn.execute(  # type: ignore[attr-defined]
        "UPDATE thread_last_seen SET last_seen = strftime('%s','now') - 86400 WHERE thread_id = ?",
        ("ttl:smoke",),
    )
    conn.commit()  # type: ignore[attr-defined]

    # Spin up the prune loop manually (lifespan would do this in prod).
    task = asyncio.create_task(main_module._agent_thread_prune_loop())
    try:
        with caplog.at_level("INFO", logger=main_module.__name__):
            # Loop sleeps 1s, then prunes. Give it 2.5s for one full cycle.
            await asyncio.sleep(2.5)
        # AC13.a: the log line landed.
        assert any("agent prune: pruned" in r.getMessage() for r in caplog.records), (
            f"prune log line missing; got: {[r.getMessage() for r in caplog.records]}"
        )
        # AC13.b: the sidecar row for the staged thread_id is gone.
        rows = conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) FROM thread_last_seen WHERE thread_id = ?",
            ("ttl:smoke",),
        ).fetchone()
        assert rows[0] == 0
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
