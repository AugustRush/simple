"""Append-only buffer that persists raw conversation turns before promotion."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Optional

import agent as agent_module
from agent import shared

from ._helpers import SQLITE_BUSY_TIMEOUT_MS, _new_id, _now

class StagingBuffer:
    """Append-only buffer that persists raw conversation turns.

    Stores only user/assistant plain-text messages (skips tool calls and
    tool results to avoid noise and oversized entries).

    Lifecycle:
      append()        — called after each user input + assistant reply
      read_all()      — returns all staged messages (for LLM extraction)
      clear_all()     — called after successful consolidation
      count()         — number of staged messages

    Default storage is SQLite under ``<context_dir>/palace.db``. Passing an
    explicit ``path`` keeps the legacy JSONL backend for compatibility with
    orphan recovery and focused file-based tests.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        context_dir: Optional[Path] = None,
        session_id: Optional[str] = None,
    ):
        self.session_id = session_id or _new_id()
        self.context_dir = context_dir or shared.CONTEXT_DIR
        self._sqlite_backed = path is None
        self.path = path or (self.context_dir / "_staging" / f"{self.session_id}.jsonl")
        self._connection_lock = threading.Lock()
        self._thread_connections: dict[int, sqlite3.Connection] = {}
        self._closed = False
        if self._sqlite_backed:
            self.context_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = self.context_dir / "palace.db"
            # Thread-local connections so the background memory worker can safely
            # share the same database file as LTMStore across threads.
            self._local = threading.local()
            self._ensure_sqlite_schema()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._count = self._load_count()

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            with self._connection_lock:
                if self._closed:
                    raise RuntimeError("staging buffer is closed")
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                # WAL permits one writer at a time; without a busy timeout a
                # concurrent writer (background memory worker, or two tools
                # dispatched into the sync-tool pool) fails immediately with
                # "database is locked" instead of waiting its turn.
                conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                self._local.conn = conn
                tid = threading.get_ident()
                old = self._thread_connections.get(tid)
                if old is not None and old is not conn:
                    with contextlib.suppress(Exception):
                        old.close()
                self._thread_connections[tid] = conn
        return self._local.conn

    def close(self) -> None:
        """Close every SQLite connection opened by this staging buffer."""
        if not self._sqlite_backed:
            return
        with self._connection_lock:
            self._closed = True
            connections = list(self._thread_connections.values())
            self._thread_connections.clear()
            self._local = threading.local()
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()

    def _ensure_sqlite_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS staging_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_staging_turns_session_id
                ON staging_turns(session_id, id)
                """
            )

    def _load_count(self) -> int:
        if self._sqlite_backed:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM staging_turns WHERE session_id = ?",
                    (self.session_id,),
                ).fetchone()
            return int(row["count"] if row else 0)
        if not self.path.exists():
            return 0
        with open(self.path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def append(self, role: str, content: str) -> None:
        """Append a plain-text turn (user or assistant only)."""
        if not content or not content.strip():
            return
        entry = {
            "role": role,
            "content": content.strip(),
            "ts": _now(),
        }
        with self._lock:
            if self._sqlite_backed:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO staging_turns (session_id, role, content, ts)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            self.session_id,
                            entry["role"],
                            entry["content"],
                            entry["ts"],
                        ),
                    )
                self._count += 1
                return
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._count += 1

    def read_all(self) -> list[dict]:
        """Return all staged messages in order."""
        with self._lock:
            if self._sqlite_backed:
                with self._connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT role, content, ts
                        FROM staging_turns
                        WHERE session_id = ?
                        ORDER BY id ASC
                        """,
                        (self.session_id,),
                    ).fetchall()
                return [
                    {
                        "role": row["role"],
                        "content": row["content"],
                        "ts": row["ts"],
                    }
                    for row in rows
                ]
            if not self.path.exists():
                return []
            msgs = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
            return msgs

    def read_last(self, n: int) -> list[dict]:
        """Return the last ``n`` staged messages (newest first in result order).

        Significantly cheaper than ``read_all()`` when only the tail of a
        large staging buffer is needed (e.g. prompt injection)."""
        if n <= 0:
            return []
        with self._lock:
            if self._sqlite_backed:
                with self._connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT role, content, ts
                        FROM staging_turns
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (self.session_id, n),
                    ).fetchall()
                result = [
                    {
                        "role": row["role"],
                        "content": row["content"],
                        "ts": row["ts"],
                    }
                    for row in reversed(rows)
                ]
                return result
            if not self.path.exists():
                return []
            msgs = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
            return msgs[-n:]

    def count(self) -> int:
        with self._lock:
            if self._sqlite_backed:
                self._count = self._load_count()
            return self._count

    def clear_all(self) -> None:
        """Delete the staging file after successful consolidation."""
        with self._lock:
            if self._sqlite_backed:
                with self._connect() as conn:
                    conn.execute(
                        "DELETE FROM staging_turns WHERE session_id = ?",
                        (self.session_id,),
                    )
                self._count = 0
                return
            self.path.unlink(missing_ok=True)
            self._count = 0

    def drop_prefix(self, count: int) -> None:
        """Remove the first ``count`` staged turns, preserving newer appends."""
        if count <= 0:
            return
        with self._lock:
            if self._sqlite_backed:
                with self._connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT id
                        FROM staging_turns
                        WHERE session_id = ?
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (self.session_id, count),
                    ).fetchall()
                    ids = [row["id"] for row in rows]
                    if ids:
                        conn.execute(
                            f"DELETE FROM staging_turns WHERE id IN ({','.join('?' for _ in ids)})",
                            ids,
                        )
                self._count = self._load_count()
                return
            if not self.path.exists():
                self._count = 0
                return
            lines = [
                line
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if count >= len(lines):
                self.path.unlink(missing_ok=True)
                self._count = 0
                return
            remaining = lines[count:]
            agent_module._atomic_write_text(
                self.path, "\n".join(remaining) + "\n", encoding="utf-8"
            )
            self._count = len(remaining)
