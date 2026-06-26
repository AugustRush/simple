from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

import agent as agent_module
from agent import shared
from agent.lexical import LATIN_TOKEN_RE, count_cjk_chars, lexical_terms
_FACT_SOURCE_PRECEDENCE = {
    "user_statement": 0,
    "direct_user": 0,
    "correction": 1,
    "bootstrap": 2,
    "manual_write": 3,
    "conversation_turn": 4,
    "consolidation_extract": 5,
    "summary_extract": 6,
}
_FACT_QUERY_SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "assistant": (
        "assistant",
        "agent",
        "bot",
        "you",
        "your",
        "你",
        "你自己",
        "你的",
        "助手",
        "机器人",
    ),
    "user": (
        "user",
        "the user",
        "me",
        "my",
        "我",
        "我的",
        "用户",
    ),
}
_FACT_QUERY_PREDICATE_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "your name",
        "my name",
        "名字",
        "叫什么",
        "叫啥",
        "称呼",
    ),
    "role": (
        "role",
        "who are you",
        "what are you",
        "身份",
        "角色",
        "你是谁",
        "你是什么",
    ),
}
_ASSISTANT_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:以后)?你(?:就)?叫([^\s，。,！!?？；;:：]{1,40})"),
    re.compile(r"你的名字是([^\s，。,！!?？；;:：]{1,40})"),
    re.compile(r"助手的名字是([^\s，。,！!?？；;:：]{1,40})"),
    re.compile(r"agent(?:'s)? name is\s+([A-Za-z0-9_-]{1,40})", re.I),
    re.compile(r"我叫([^\s，。,！!?？；;:：]{1,40})"),
    re.compile(r"我的名字是([^\s，。,！!?？；;:：]{1,40})"),
    re.compile(r"(?:my name is|you can call me|i am)\s+([A-Za-z0-9_-]{1,40})", re.I),
    re.compile(r"(?:your name is|i(?:'ll| will) call you)\s+([A-Za-z0-9_-]{1,40})", re.I),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _emit_consolidation(phase: str, **fields: Any) -> None:
    """Emit a consolidation lifecycle event into the active EventCollector."""
    try:
        from agent.core.output import _active_event_collector
        collector = _active_event_collector.get()
        if collector is not None:
            collector.emit(f"consolidation_{phase}", **fields)
    except Exception:
        pass  # never let event emission break maintenance


def normalize_memory_chapter(chapter: str, aliases: dict[str, str]) -> str:
    chapter = str(chapter).strip().lower()
    return aliases.get(chapter, chapter)


def _normalize_fact_part(value: str, default: str = "") -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized or default


def _fact_key(subject: str, predicate: str, scope: str) -> str:
    return json.dumps(
        [
            _normalize_fact_part(subject),
            _normalize_fact_part(predicate),
            _normalize_fact_part(scope, "global"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _fact_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (dict, list)):
        return "json"
    return "string"


def _dump_fact_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_fact_value(value_json: str) -> Any:
    return json.loads(value_json)


def _extract_assistant_name(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    for pattern in _ASSISTANT_NAME_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        candidate = match.group(1).strip().strip("“”\"'.,!?，。！？：:；;()[]{}")
        if candidate:
            return candidate
    return ""


def _lexical_terms(text: str) -> list[str]:
    return lexical_terms(text)

class MemoryPalace:
    """Facade for all memory operations."""

    def __init__(
        self,
        tidy_interval: int = shared.MEMORY_TIDY_INTERVAL,
        tidy_threshold: int = shared.MEMORY_TIDY_FILE_THRESHOLD,
        base_dir: Path = shared.MEMORY_DIR,
        context_dir: Path = shared.CONTEXT_DIR,
        store: Optional["LTMStore"] = None,
    ):
        self.store = store or LTMStore(context_dir=context_dir, memory_dir=base_dir)
        self.base_dir = self.store.memory_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._export_path = self.base_dir / "memory.jsonl"
        self._export_dirty = True
        self._last_tidy: float = 0
        self._files_since_tidy: int = 0
        self._tidy_interval = tidy_interval
        self._tidy_threshold = tidy_threshold

    def write(self, chapter: str, name: str, content: str, append: bool = False):
        chapter = normalize_memory_chapter(chapter, shared.LEGACY_MEMORY_ALIASES)
        self.store.upsert_manual_note(chapter, name, content, append=append)
        self._files_since_tidy += 1
        self._export_dirty = True

    def read(self, chapter: str, name: str) -> str:
        chapter = normalize_memory_chapter(chapter, shared.LEGACY_MEMORY_ALIASES)
        note = self.store.read_manual_note(chapter, name)
        if note:
            return note.content
        entries = self.store.read_entries_for_entity(chapter, name)
        if not entries:
            return ""
        lines = [f"# {chapter}/{name}", ""]
        for entry in entries:
            lines.append(f"- ({entry.memory_type}) {entry.content}")
        return "\n".join(lines)

    def search(self, query: str) -> list[dict]:
        results = []
        for entry in self.store.search_entries(query, limit=20):
            anchor = (
                f"{entry.category}/{entry.entity}" if entry.entity else entry.category
            )
            results.append({"path": anchor, "snippet": entry.content[:120]})
        return results

    def read_index(self) -> str:
        path = self.export_jsonl()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def export_jsonl(self, path: Optional[Path] = None) -> Path:
        path = path or self._export_path
        if path == self._export_path and path.exists() and not self._export_dirty:
            return path
        entries = sorted(
            self.store.all_entries(),
            key=lambda entry: (str(entry.updated_at), str(entry.id)),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(entry.to_dict(), ensure_ascii=False)
            for entry in entries
        ]
        path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        if path == self._export_path:
            self._export_dirty = False
        return path

    def should_tidy(self) -> bool:
        if self._files_since_tidy >= self._tidy_threshold:
            return True
        if self._tidy_interval > 0 and self._last_tidy > 0:
            if time.time() - self._last_tidy >= self._tidy_interval:
                return True
        return False

    def force_tidy(self) -> None:
        """Mark the palace as due for maintenance without exposing internals."""
        self._last_tidy = 0
        self._files_since_tidy = self._tidy_threshold

    async def tidy(self, client: Any, model: str):
        """Local maintenance pass: apply retention and refresh JSONL export."""
        shared.CONSOLE.print("[dim]Tidying memory palace...[/dim]")
        self.store.apply_retention()
        snapshot = self.store.maintenance_snapshot(limit=20)
        if snapshot:
            self.store.add_entry(
                LTMEntry(
                    id=_new_id(),
                    content=snapshot,
                    importance=0.4,
                    category="archive",
                    entity="maintenance",
                    memory_type="maintenance_report",
                    source_session="manual_tidy",
                    confidence=1.0,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        self.export_jsonl()
        self._last_tidy = time.time()
        self._files_since_tidy = 0
        shared.CONSOLE.print("[dim]Memory tidy complete.[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# 2.5. CONTEXT MANAGER — LTM + Retrieval + Consolidation
# ─────────────────────────────────────────────────────────────────────────────


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
        context_dir: Path = shared.CONTEXT_DIR,
        session_id: Optional[str] = None,
    ):
        self.session_id = session_id or _new_id()
        self.context_dir = context_dir
        self._sqlite_backed = path is None
        self.path = path or (context_dir / "_staging" / f"{self.session_id}.jsonl")
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
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

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


@dataclass
class LTMEntry:
    """A single long-term memory entry with importance scoring."""

    id: str
    content: str
    importance: float  # 0.0 – 1.0
    category: str
    created_at: str
    updated_at: str
    entity: str = ""
    memory_type: str = "fact"
    scope: str = "global"
    status: str = "active"
    source_session: str = ""
    confidence: float = 1.0

    def decay(self, factor: float = shared.DECAY_FACTOR) -> None:
        self.importance = max(0.0, self.importance * factor)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "importance": self.importance,
            "category": self.category,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "entity": self.entity,
            "memory_type": self.memory_type,
            "scope": self.scope,
            "status": self.status,
            "source_session": self.source_session,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LTMEntry":
        return cls(**d)


@dataclass(frozen=True)
class ConversationTurn:
    """A durable event-log row for one plain-text conversation message."""

    id: int
    session_id: str
    role: str
    content: str
    channel: str = ""
    message_id: str = ""
    reply_to_id: str = ""
    metadata: dict[str, Any] | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "channel": self.channel,
            "message_id": self.message_id,
            "reply_to_id": self.reply_to_id,
            "metadata": self.metadata or {},
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SessionWorkingState:
    """Durable, model-readable working context for one channel session."""

    session_id: str
    state: dict[str, Any]
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AgentRuntimeEvent:
    """Append-only runtime fact for one channel session."""

    id: int
    session_id: str
    event_type: str
    payload: dict[str, Any]
    turn_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass
class FactAssertion:
    """An append-only normalized fact claim derived from some evidence source."""

    id: str
    subject: str
    predicate: str
    value: Any
    value_type: str = ""
    scope: str = "global"
    source_kind: str = "manual_write"
    source_id: str = ""
    source_session: str = ""
    channel: str = ""
    confidence: float = 1.0
    status: str = "active"
    valid_from: str = ""
    valid_to: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class ResolvedFact:
    """The current best belief for a canonical fact key."""

    fact_key: str
    subject: str
    predicate: str
    value: Any
    value_type: str = ""
    scope: str = "global"
    winning_assertion_id: str = ""
    resolution_reason: str = "resolved"
    confidence: float = 1.0
    resolved_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass(frozen=True)
class QueryPlan:
    """A lightweight plan for exact-fact versus freeform retrieval."""

    query_type: str = "freeform_context"
    scope: str = "global"
    target_subjects: tuple[str, ...] = ()
    target_predicates: tuple[str, ...] = ()
    lexical_terms: tuple[str, ...] = ()
    allow_freeform_fallback: bool = True


@dataclass
class LTMCategory:
    """Metadata for a long-term memory category."""

    name: str
    entry_count: int = 0
    avg_importance: float = 0.0
    last_updated: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entry_count": self.entry_count,
            "avg_importance": self.avg_importance,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LTMCategory":
        return cls(**d)


@dataclass
class ConsolidationResult:
    success: bool
    compressed_messages: list[dict]
    stored_entries: int = 0


class LTMStore:
    """SQLite-backed long-term memory with JSON and markdown projections."""

    def __init__(
        self,
        context_dir: Path = shared.CONTEXT_DIR,
        max_categories: int = shared.MAX_CATEGORIES,
        memory_dir: Path = shared.MEMORY_DIR,
    ):
        self.dir = context_dir
        self.max_categories = max_categories
        self.memory_dir = memory_dir
        self._meta_path = context_dir / "_meta.json"
        self._db_path = context_dir / "palace.db"
        self._local = threading.local()  # thread-local connection storage
        self._thread_connections: dict[
            int, sqlite3.Connection
        ] = {}  # thread-id → connection; bounded by concurrent thread count
        self.dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._ensure_fts_index()
        self._cleanup_legacy_artifacts()
        self._meta = {"categories": [], "total_entries": 0}
        self._refresh_indexes()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Return a thread-local singleton connection with WAL mode enabled.

        SQLite connections are not safe to share across threads, so we keep one
        per thread. WAL mode allows concurrent readers alongside a single writer,
        which is critical when the background memory worker reads while the main
        loop writes.
        """
        # Thread-local storage ensures each thread gets its own connection.
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            tid = threading.get_ident()
            # If thread ID was reused, close the stale connection first.
            old = self._thread_connections.get(tid)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self._thread_connections[tid] = conn
        return self._local.conn

    def close(self) -> None:
        """Close all thread-local SQLite connections explicitly."""
        for conn in self._thread_connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._thread_connections = {}
        self._local = threading.local()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL,
                    category TEXT NOT NULL,
                    entity TEXT NOT NULL DEFAULT '',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    scope TEXT NOT NULL DEFAULT 'global',
                    status TEXT NOT NULL DEFAULT 'active',
                    source_session TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_category_status
                    ON memory_items(category, status);
                CREATE INDEX IF NOT EXISTS idx_memory_entity_status
                    ON memory_items(entity, status);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts
                    USING fts5(
                        memory_id UNINDEXED,
                        content,
                        entity,
                        category,
                        tokenize='unicode61'
                    );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    reply_to_id TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_turns_session_id
                    ON conversation_turns(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_conversation_turns_created_at
                    ON conversation_turns(created_at);
                CREATE TABLE IF NOT EXISTS session_working_state (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_session_id
                    ON agent_events(session_id, id);
                CREATE TABLE IF NOT EXISTS fact_assertions (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    value_type TEXT NOT NULL DEFAULT 'string',
                    scope TEXT NOT NULL DEFAULT 'global',
                    source_kind TEXT NOT NULL DEFAULT 'manual_write',
                    source_id TEXT NOT NULL DEFAULT '',
                    source_session TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'active',
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_to TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fact_assertions_lookup
                    ON fact_assertions(subject, predicate, scope, created_at, id);
                CREATE TABLE IF NOT EXISTS resolved_facts (
                    fact_key TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    value_type TEXT NOT NULL DEFAULT 'string',
                    scope TEXT NOT NULL DEFAULT 'global',
                    winning_assertion_id TEXT NOT NULL,
                    resolution_reason TEXT NOT NULL DEFAULT 'resolved',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    resolved_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_resolved_facts_lookup
                    ON resolved_facts(subject, predicate, scope);
                """
            )

    def _ensure_fts_index(self) -> None:
        with self._connect() as conn:
            mismatch = conn.execute(
                """
                SELECT 1
                FROM (
                    SELECT m.id AS memory_id
                    FROM memory_items m
                    WHERE m.status NOT IN ('archived', 'superseded')
                    EXCEPT
                    SELECT f.memory_id
                    FROM memory_items_fts f
                )
                UNION ALL
                SELECT 1
                FROM (
                    SELECT f.memory_id
                    FROM memory_items_fts f
                    EXCEPT
                    SELECT m.id
                    FROM memory_items m
                    WHERE m.status NOT IN ('archived', 'superseded')
                )
                LIMIT 1
                """
            ).fetchone()
            if mismatch is None:
                return
            conn.execute("DELETE FROM memory_items_fts")
            rows = conn.execute(
                """
                SELECT id, content, entity, category
                FROM memory_items
                WHERE status NOT IN ('archived', 'superseded')
                """
            ).fetchall()
            conn.executemany(
                """
                INSERT INTO memory_items_fts (memory_id, content, entity, category)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (row["id"], row["content"], row["entity"], row["category"])
                    for row in rows
                ],
            )

    def _save_meta(self) -> None:
        """Compatibility no-op: category stats are derived from SQLite."""

    def _cleanup_legacy_artifacts(self) -> None:
        self._meta_path.unlink(missing_ok=True)
        (self.memory_dir / "INDEX.md").unlink(missing_ok=True)

    @staticmethod
    def normalize_category_name(name: str) -> str:
        """Collapse external category input into a safe, stable storage key."""
        normalized = re.sub(r"[\\/]+", " ", str(name).strip().lower())
        normalized = normalized.replace("..", " ")
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("._")
        return normalized or "general"

    def _category_path(self, name: str) -> Path:
        safe_name = self.normalize_category_name(name)
        path = (self.dir / f"{safe_name}.json").resolve()
        root = self.dir.resolve()
        if root not in path.parents:
            raise ValueError(f"Category path escaped context dir: {name}")
        return path

    def _is_palace_locus(self, category: str) -> bool:
        return self.normalize_category_name(category) in shared.PALACE_LOCI

    def _normalize_entity(self, entity: str, category: str) -> str:
        raw = str(entity).strip()
        if raw:
            return self.normalize_category_name(raw)
        if self._is_palace_locus(category):
            return (
                "user"
                if self.normalize_category_name(category) == "identity"
                else "general"
            )
        return self.normalize_category_name(category)

    def _projection_path(self, category: str, entity: str) -> Path:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        return self.memory_dir / category / f"{entity}.md"

    def _row_to_entry(self, row: sqlite3.Row) -> LTMEntry:
        return LTMEntry(
            id=row["id"],
            content=row["content"],
            importance=row["importance"],
            category=row["category"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            entity=row["entity"],
            memory_type=row["memory_type"],
            scope=row["scope"],
            status=row["status"],
            source_session=row["source_session"],
            confidence=row["confidence"],
        )

    def _row_to_conversation_turn(self, row: sqlite3.Row) -> ConversationTurn:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return ConversationTurn(
            id=int(row["id"]),
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            channel=row["channel"],
            message_id=row["message_id"],
            reply_to_id=row["reply_to_id"],
            metadata=metadata,
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_session_working_state(row: sqlite3.Row) -> SessionWorkingState:
        try:
            state = json.loads(row["state_json"] or "{}")
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        return SessionWorkingState(
            session_id=row["session_id"],
            state=state,
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_agent_runtime_event(row: sqlite3.Row) -> AgentRuntimeEvent:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        return AgentRuntimeEvent(
            id=int(row["id"]),
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            event_type=row["event_type"],
            payload=payload,
            created_at=row["created_at"],
        )

    def _row_to_fact_assertion(self, row: sqlite3.Row) -> FactAssertion:
        return FactAssertion(
            id=row["id"],
            subject=row["subject"],
            predicate=row["predicate"],
            value=_load_fact_value(row["value_json"]),
            value_type=row["value_type"],
            scope=row["scope"],
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            source_session=row["source_session"],
            channel=row["channel"],
            confidence=float(row["confidence"] or 1.0),
            status=row["status"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_resolved_fact(self, row: sqlite3.Row) -> ResolvedFact:
        return ResolvedFact(
            fact_key=row["fact_key"],
            subject=row["subject"],
            predicate=row["predicate"],
            value=_load_fact_value(row["value_json"]),
            value_type=row["value_type"],
            scope=row["scope"],
            winning_assertion_id=row["winning_assertion_id"],
            resolution_reason=row["resolution_reason"],
            confidence=float(row["confidence"] or 1.0),
            resolved_at=row["resolved_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _normalize_content_key(content: str) -> str:
        normalized = re.sub(r"\s+", " ", content.strip().lower())
        return normalized

    def _stable_merge_key(self, entry: LTMEntry) -> Optional[str]:
        category = self.normalize_category_name(entry.category)
        entity = self._normalize_entity(entry.entity, category)
        memory_type = (entry.memory_type or "fact").strip().lower()

        if category in {"episodes", "archive", "concepts"}:
            return None
        if category == "tasks":
            normalized_content = self._normalize_content_key(entry.content)
            return f"{category}|{entity}|{memory_type}|{normalized_content}"
        if category not in {"identity", "projects", "people", "procedures"}:
            return None

        normalized_content = self._normalize_content_key(entry.content)
        return f"{category}|{entity}|{memory_type}|{normalized_content}"

    def _match_existing_entry_id(
        self, conn: sqlite3.Connection, entry: LTMEntry
    ) -> Optional[str]:
        merge_key = self._stable_merge_key(entry)
        if not merge_key:
            return None

        category = self.normalize_category_name(entry.category)
        entity = self._normalize_entity(entry.entity, category)
        memory_type = (entry.memory_type or "fact").strip().lower()

        normalized_content = self._normalize_content_key(entry.content)
        rows = conn.execute(
            """
            SELECT * FROM memory_items
            WHERE category = ? AND entity = ? AND memory_type = ?
              AND status NOT IN ('archived', 'superseded')
            ORDER BY updated_at DESC, id ASC
            """,
            (category, entity, memory_type),
        ).fetchall()
        for row in rows:
            if self._normalize_content_key(row["content"]) == normalized_content:
                return row["id"]
        return None

    def _delete_fts_rows(
        self,
        conn: sqlite3.Connection,
        entry_ids: list[str] | set[str] | tuple[str, ...],
    ) -> None:
        ids = [entry_id for entry_id in entry_ids if entry_id]
        if not ids:
            return
        conn.execute(
            f"DELETE FROM memory_items_fts WHERE memory_id IN ({','.join('?' for _ in ids)})",
            ids,
        )

    def _sync_fts_row(self, conn: sqlite3.Connection, entry_id: str) -> None:
        self._delete_fts_rows(conn, [entry_id])
        row = conn.execute(
            """
            SELECT id, content, entity, category, status
            FROM memory_items
            WHERE id = ?
            LIMIT 1
            """,
            (entry_id,),
        ).fetchone()
        if row is None or row["status"] in {"archived", "superseded"}:
            return
        conn.execute(
            """
            INSERT INTO memory_items_fts (memory_id, content, entity, category)
            VALUES (?, ?, ?, ?)
            """,
            (row["id"], row["content"], row["entity"], row["category"]),
        )

    def _write_entry_row(self, conn: sqlite3.Connection, entry: LTMEntry) -> set[str]:
        original_category = self.normalize_category_name(entry.category)
        original_entity = str(entry.entity or "")
        entry.category = self._coerce_category_for_storage(conn, original_category)
        if entry.category == "concepts" and original_category not in shared.PALACE_LOCI:
            entry.entity = (
                self.normalize_category_name(original_entity)
                if original_entity.strip()
                else original_category
            )
        entry.category = self.normalize_category_name(entry.category)
        entry.entity = self._normalize_entity(entry.entity, entry.category)
        entry.memory_type = entry.memory_type or "fact"
        entry.scope = entry.scope or "global"
        entry.status = entry.status or "active"
        entry.source_session = entry.source_session or ""
        entry.confidence = float(entry.confidence or 1.0)
        existing_row = self._find_existing_entry_row(conn, entry)
        affected_categories = {entry.category}
        if existing_row:
            entry.id = existing_row["id"]
            entry.created_at = existing_row["created_at"]
            affected_categories.add(
                self.normalize_category_name(existing_row["category"])
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_items (
                id, content, importance, category, entity, memory_type, scope,
                status, source_session, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.content,
                float(entry.importance),
                entry.category,
                entry.entity,
                entry.memory_type,
                entry.scope,
                entry.status,
                entry.source_session,
                float(entry.confidence),
                entry.created_at,
                entry.updated_at,
            ),
        )
        self._sync_fts_row(conn, entry.id)
        return {
            self.normalize_category_name(category) for category in affected_categories
        }

    def _find_existing_entry_row(
        self, conn: sqlite3.Connection, entry: LTMEntry
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            "SELECT * FROM memory_items WHERE id = ? LIMIT 1",
            (entry.id,),
        ).fetchone()
        if row:
            return row
        existing_id = self._match_existing_entry_id(conn, entry)
        if not existing_id:
            return None
        return conn.execute(
            "SELECT * FROM memory_items WHERE id = ? LIMIT 1",
            (existing_id,),
        ).fetchone()

    def _coerce_category_for_storage(
        self, conn: sqlite3.Connection, category: str
    ) -> str:
        normalized = self.normalize_category_name(category)
        if normalized in shared.PALACE_LOCI:
            return normalized
        dynamic_categories = self._dynamic_category_names(conn)
        if normalized in dynamic_categories:
            return normalized
        if len(dynamic_categories) < self.max_categories:
            return normalized
        return "concepts"

    def _dynamic_category_names(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            """
            SELECT DISTINCT category FROM memory_items
            WHERE status = 'active'
            """
        ).fetchall()
        return {row["category"] for row in rows if row["category"] not in shared.PALACE_LOCI}

    def _refresh_indexes(self) -> None:
        self._meta = self._category_stats()

    def _category_stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT category, COUNT(*) AS entry_count,
                       AVG(importance) AS avg_importance,
                       MAX(updated_at) AS last_updated
                FROM memory_items
                WHERE status = 'active'
                GROUP BY category
                ORDER BY category
                """
            ).fetchall()
        categories = [
            {
                "name": row["category"],
                "entry_count": int(row["entry_count"]),
                "avg_importance": float(row["avg_importance"] or 0.0),
                "last_updated": row["last_updated"] or "",
            }
            for row in rows
        ]
        return {
            "categories": categories,
            "total_entries": sum(int(row["entry_count"]) for row in rows),
        }

    def _sync_after_mutation(self, categories: set[str]) -> None:
        self._meta = self._category_stats()

    def _sync_category_snapshot(self, category: str) -> None:
        """Compatibility no-op: user-visible memory is exported as JSONL."""

    def _sync_projection(self, category: str) -> None:
        """Compatibility no-op: memory palace loci are internal categories."""

    def _remove_category(self, category: str) -> None:
        category = self.normalize_category_name(category)
        with self._connect() as conn:
            row_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM memory_items WHERE category = ?",
                    (category,),
                ).fetchall()
            ]
            conn.execute("DELETE FROM memory_items WHERE category = ?", (category,))
            self._delete_fts_rows(conn, row_ids)
        self._sync_after_mutation({category})

    # ── Category helpers ──────────────────────────────────────────────────────

    def list_categories(self) -> list[LTMCategory]:
        return [LTMCategory.from_dict(c) for c in self._category_stats()["categories"]]

    def category_count(self) -> int:
        return len(self.list_categories())

    def dynamic_category_count(self) -> int:
        return len(
            [
                category
                for category in self.list_categories()
                if category.name not in shared.PALACE_LOCI
            ]
        )

    # ── Entry CRUD ────────────────────────────────────────────────────────────

    def read_entries(self, category: str) -> list[LTMEntry]:
        category = self.normalize_category_name(category)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE category = ? AND status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """,
                (category,),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def read_entries_for_entity(self, category: str, entity: str) -> list[LTMEntry]:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE category = ? AND entity = ?
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """,
                (category, entity),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def read_manual_note(self, category: str, entity: str) -> Optional[LTMEntry]:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE category = ? AND entity = ? AND memory_type = 'note'
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """,
                (category, entity),
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def _fact_assertions_from_entry(self, entry: LTMEntry) -> list[FactAssertion]:
        facts: list[FactAssertion] = []
        category = self.normalize_category_name(entry.category)
        entity = self._normalize_entity(entry.entity, category)
        memory_type = str(entry.memory_type or "").strip().lower()
        if category == "identity" and entity == "assistant":
            name = _extract_assistant_name(entry.content)
            if name:
                facts.append(
                    FactAssertion(
                        id=_new_id(),
                        subject="assistant",
                        predicate="name",
                        value=name,
                        source_kind="consolidation_extract",
                        source_id=entry.id,
                        source_session=entry.source_session,
                        confidence=float(entry.confidence or 1.0),
                        created_at=entry.created_at or _now(),
                        updated_at=entry.updated_at or _now(),
                    )
                )
            elif memory_type in {"self_identity", "assistant_identity"}:
                facts.append(
                    FactAssertion(
                        id=_new_id(),
                        subject="assistant",
                        predicate="identity_note",
                        value=entry.content,
                        source_kind="consolidation_extract",
                        source_id=entry.id,
                        source_session=entry.source_session,
                        confidence=float(entry.confidence or 1.0),
                        created_at=entry.created_at or _now(),
                        updated_at=entry.updated_at or _now(),
                    )
                )
        return facts

    def write_entries(self, category: str, entries: list[LTMEntry]) -> None:
        category = self.normalize_category_name(category)
        if not entries:
            self._remove_category(category)
            return

        affected_categories = {category}
        with self._connect() as conn:
            row_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM memory_items WHERE category = ?",
                    (category,),
                ).fetchall()
            ]
            conn.execute("DELETE FROM memory_items WHERE category = ?", (category,))
            self._delete_fts_rows(conn, row_ids)
            for entry in entries:
                entry.category = category
                affected_categories.update(self._write_entry_row(conn, entry))
        self._sync_after_mutation(affected_categories)

    def add_entry(self, entry: LTMEntry) -> None:
        entry.category = self.normalize_category_name(entry.category)
        entry.entity = self._normalize_entity(entry.entity, entry.category)
        with self._connect() as conn:
            affected_categories = self._write_entry_row(conn, entry)
        self._sync_after_mutation(affected_categories)
        for fact in self._fact_assertions_from_entry(entry):
            self.add_fact_assertion(fact)

    def add_entries(self, entries: list[LTMEntry]) -> None:
        """Batch-insert multiple entries in a single transaction and one sync pass.

        Preferred over calling add_entry() in a loop: consolidation writes all
        extracted facts at once, avoiding N separate SQL transactions and N
        rounds of _meta + snapshot + projection file I/O.
        """
        if not entries:
            return
        affected_categories: set[str] = set()
        with self._connect() as conn:
            for entry in entries:
                entry.category = self.normalize_category_name(entry.category)
                entry.entity = self._normalize_entity(entry.entity, entry.category)
                affected_categories.update(self._write_entry_row(conn, entry))
        self._sync_after_mutation(affected_categories)
        for entry in entries:
            for fact in self._fact_assertions_from_entry(entry):
                self.add_fact_assertion(fact)

    def append_conversation_turn(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        channel: str = "",
        message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> Optional[ConversationTurn]:
        """Append one durable conversation event without affecting staging."""
        clean_content = str(content or "").strip()
        if not clean_content:
            return None
        clean_role = str(role or "").strip().lower()
        if clean_role not in {"user", "assistant"}:
            return None
        payload = metadata or {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO conversation_turns (
                    session_id, role, content, channel, message_id, reply_to_id,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session_id or "").strip() or "default",
                    clean_role,
                    clean_content,
                    str(channel or ""),
                    str(message_id or ""),
                    str(reply_to_id or ""),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at or _now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM conversation_turns WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return self._row_to_conversation_turn(row) if row else None

    def recent_conversation_turns(
        self,
        *,
        session_id: Optional[str] = None,
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> list[ConversationTurn]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            sql = "SELECT * FROM conversation_turns"
            params: list[Any] = []
            if session_id:
                sql += " WHERE session_id = ?"
                params.append(session_id)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_conversation_turn(row) for row in reversed(rows)]

    def load_session_working_state(
        self,
        session_id: str,
    ) -> Optional[SessionWorkingState]:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_working_state WHERE session_id = ?",
                (clean_session_id,),
            ).fetchone()
        return self._row_to_session_working_state(row) if row else None

    def save_session_working_state(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        updated_at: Optional[str] = None,
    ) -> SessionWorkingState:
        clean_session_id = str(session_id or "").strip() or "default"
        clean_state = state if isinstance(state, dict) else {"value": state}
        ts = updated_at or _now()
        payload = json.dumps(clean_state, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_working_state (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (clean_session_id, payload, ts),
            )
            row = conn.execute(
                "SELECT * FROM session_working_state WHERE session_id = ?",
                (clean_session_id,),
            ).fetchone()
        return self._row_to_session_working_state(row)

    def append_agent_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        turn_id: str = "",
        created_at: Optional[str] = None,
    ) -> AgentRuntimeEvent:
        clean_session_id = str(session_id or "").strip() or "default"
        clean_event_type = str(event_type or "").strip() or "event"
        clean_payload = payload if isinstance(payload, dict) else {}
        ts = created_at or _now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_events (
                    session_id, turn_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    clean_session_id,
                    str(turn_id or ""),
                    clean_event_type,
                    json.dumps(clean_payload, ensure_ascii=False, sort_keys=True),
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_events WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return self._row_to_agent_runtime_event(row)

    def recent_agent_events(
        self,
        *,
        session_id: str,
        limit: int = 20,
    ) -> list[AgentRuntimeEvent]:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return []
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (clean_session_id, limit),
            ).fetchall()
        return [self._row_to_agent_runtime_event(row) for row in reversed(rows)]

    @staticmethod
    def _fact_assertion_core_score(assertion: FactAssertion) -> tuple[int, float, str]:
        precedence = _FACT_SOURCE_PRECEDENCE.get(str(assertion.source_kind or "").strip().lower(), 9)
        return (-precedence, float(assertion.confidence or 0.0), assertion.created_at or "")

    @classmethod
    def _fact_assertion_total_score(cls, assertion: FactAssertion) -> tuple[int, float, str, str]:
        return (*cls._fact_assertion_core_score(assertion), assertion.id)

    def add_fact_assertion(self, assertion: FactAssertion) -> FactAssertion:
        subject = _normalize_fact_part(assertion.subject)
        predicate = _normalize_fact_part(assertion.predicate)
        scope = _normalize_fact_part(assertion.scope, "global")
        if not subject or not predicate:
            raise ValueError("fact assertions require non-empty subject and predicate")

        normalized = FactAssertion(
            id=assertion.id or _new_id(),
            subject=subject,
            predicate=predicate,
            value=assertion.value,
            value_type=assertion.value_type or _fact_value_type(assertion.value),
            scope=scope,
            source_kind=_normalize_fact_part(assertion.source_kind, "manual_write"),
            source_id=str(assertion.source_id or "").strip(),
            source_session=str(assertion.source_session or "").strip(),
            channel=str(assertion.channel or "").strip(),
            confidence=float(assertion.confidence or 0.0),
            status=_normalize_fact_part(assertion.status, "active"),
            valid_from=str(assertion.valid_from or "").strip(),
            valid_to=str(assertion.valid_to or "").strip(),
            created_at=assertion.created_at or _now(),
            updated_at=assertion.updated_at or _now(),
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fact_assertions (
                    id, subject, predicate, value_json, value_type, scope,
                    source_kind, source_id, source_session, channel, confidence,
                    status, valid_from, valid_to, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.id,
                    normalized.subject,
                    normalized.predicate,
                    _dump_fact_value(normalized.value),
                    normalized.value_type,
                    normalized.scope,
                    normalized.source_kind,
                    normalized.source_id,
                    normalized.source_session,
                    normalized.channel,
                    normalized.confidence,
                    normalized.status,
                    normalized.valid_from,
                    normalized.valid_to,
                    normalized.created_at,
                    normalized.updated_at,
                ),
            )

        self.resolve_fact(normalized.subject, normalized.predicate, normalized.scope)
        return normalized

    def read_fact_assertions(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[FactAssertion]:
        sql = "SELECT * FROM fact_assertions WHERE 1 = 1"
        params: list[Any] = []
        if subject is not None:
            sql += " AND subject = ?"
            params.append(_normalize_fact_part(subject))
        if predicate is not None:
            sql += " AND predicate = ?"
            params.append(_normalize_fact_part(predicate))
        if scope is not None:
            sql += " AND scope = ?"
            params.append(_normalize_fact_part(scope, "global"))
        sql += " ORDER BY created_at ASC, id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_fact_assertion(row) for row in rows]

    def resolve_fact(
        self,
        subject: str,
        predicate: str,
        scope: str = "global",
    ) -> Optional[ResolvedFact]:
        normalized_subject = _normalize_fact_part(subject)
        normalized_predicate = _normalize_fact_part(predicate)
        normalized_scope = _normalize_fact_part(scope, "global")
        key = _fact_key(normalized_subject, normalized_predicate, normalized_scope)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM fact_assertions
                WHERE subject = ? AND predicate = ? AND scope = ?
                  AND status != 'archived'
                ORDER BY created_at ASC, id ASC
                """,
                (normalized_subject, normalized_predicate, normalized_scope),
            ).fetchall()
            assertions = [self._row_to_fact_assertion(row) for row in rows]
            if not assertions:
                conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
                return None

            active = [assertion for assertion in assertions if not assertion.valid_to]
            if not active:
                conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
                return None

            best_core_score = max(
                self._fact_assertion_core_score(assertion) for assertion in active
            )
            top_assertions = [
                assertion
                for assertion in active
                if self._fact_assertion_core_score(assertion) == best_core_score
            ]
            top_values = {
                _dump_fact_value(assertion.value) for assertion in top_assertions
            }

            if len(top_values) > 1:
                now = _now()
                conn.execute(
                    """
                    UPDATE fact_assertions
                    SET status = 'conflicted',
                        updated_at = ?
                    WHERE subject = ? AND predicate = ? AND scope = ?
                      AND status != 'archived'
                    """,
                    (now, normalized_subject, normalized_predicate, normalized_scope),
                )
                conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
                return None

            winning_value_json = next(iter(top_values))
            winner = max(
                [
                    assertion
                    for assertion in active
                    if _dump_fact_value(assertion.value) == winning_value_json
                ],
                key=self._fact_assertion_total_score,
            )
            now = _now()
            for assertion in assertions:
                status = "active" if assertion.id == winner.id else "superseded"
                conn.execute(
                    "UPDATE fact_assertions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, assertion.id),
                )

            resolved = ResolvedFact(
                fact_key=key,
                subject=normalized_subject,
                predicate=normalized_predicate,
                value=winner.value,
                value_type=winner.value_type or _fact_value_type(winner.value),
                scope=normalized_scope,
                winning_assertion_id=winner.id,
                resolution_reason="resolved",
                confidence=float(winner.confidence or 0.0),
                resolved_at=now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO resolved_facts (
                    fact_key, subject, predicate, value_json, value_type, scope,
                    winning_assertion_id, resolution_reason, confidence,
                    resolved_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved.fact_key,
                    resolved.subject,
                    resolved.predicate,
                    _dump_fact_value(resolved.value),
                    resolved.value_type,
                    resolved.scope,
                    resolved.winning_assertion_id,
                    resolved.resolution_reason,
                    resolved.confidence,
                    resolved.resolved_at,
                    resolved.updated_at,
                ),
            )
        return resolved

    def read_resolved_facts(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[ResolvedFact]:
        sql = "SELECT * FROM resolved_facts WHERE 1 = 1"
        params: list[Any] = []
        if subject is not None:
            sql += " AND subject = ?"
            params.append(_normalize_fact_part(subject))
        if predicate is not None:
            sql += " AND predicate = ?"
            params.append(_normalize_fact_part(predicate))
        if scope is not None:
            sql += " AND scope = ?"
            params.append(_normalize_fact_part(scope, "global"))
        sql += " ORDER BY subject ASC, predicate ASC, scope ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_resolved_fact(row) for row in rows]

    def has_conflicted_fact(
        self,
        subject: str,
        predicate: str,
        scope: str = "global",
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM fact_assertions
                WHERE subject = ? AND predicate = ? AND scope = ?
                  AND status = 'conflicted'
                LIMIT 1
                """,
                (
                    _normalize_fact_part(subject),
                    _normalize_fact_part(predicate),
                    _normalize_fact_part(scope, "global"),
                ),
            ).fetchone()
        return row is not None

    def upsert_manual_note(
        self, category: str, entity: str, content: str, append: bool = False
    ) -> LTMEntry:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        existing = self.read_manual_note(category, entity)
        if existing:
            existing.content = (
                f"{existing.content.rstrip()}\n{content.strip()}" if append else content
            ).strip()
            existing.updated_at = _now()
            entry = existing
        else:
            entry = LTMEntry(
                id=_new_id(),
                content=content.strip(),
                importance=0.8,
                category=category,
                entity=entity,
                memory_type="note",
                scope="global",
                status="active",
                source_session="manual_memory_write",
                confidence=1.0,
                created_at=_now(),
                updated_at=_now(),
            )
        with self._connect() as conn:
            affected_categories = self._write_entry_row(conn, entry)
        self._sync_after_mutation(affected_categories)
        return entry

    def all_entries(self) -> list[LTMEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def search_entries(
        self,
        query: str,
        categories: Optional[list[str]] = None,
        limit: int = shared.RETRIEVAL_TOP_K,
    ) -> list[LTMEntry]:
        query_terms = _lexical_terms(query)
        if not query_terms:
            with self._connect() as conn:
                sql = """
                    SELECT * FROM memory_items
                    WHERE status NOT IN ('archived', 'superseded')
                """
                params: list[Any] = []
                if categories:
                    cats = [self.normalize_category_name(c) for c in categories]
                    sql += f" AND category IN ({','.join('?' for _ in cats)})"
                    params.extend(cats)
                sql += " ORDER BY importance DESC, updated_at DESC, id ASC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            return [self._row_to_entry(row) for row in rows]

        latin_terms = [term for term in query_terms if LATIN_TOKEN_RE.fullmatch(term)]
        cjk_terms = [term for term in query_terms if term not in latin_terms]
        results_by_id: dict[str, LTMEntry] = {}

        def _merge(rows: list[sqlite3.Row]) -> None:
            for row in rows:
                entry = self._row_to_entry(row)
                results_by_id.setdefault(entry.id, entry)

        normalized_categories = (
            [self.normalize_category_name(c) for c in categories] if categories else []
        )

        with self._connect() as conn:
            if latin_terms:
                escaped_tokens = [token.replace('"', '""') for token in latin_terms]
                match_query = " OR ".join(f'"{token}"*' for token in escaped_tokens)
                sql = """
                    SELECT m.*
                    FROM memory_items_fts
                    JOIN memory_items AS m
                      ON m.id = memory_items_fts.memory_id
                    WHERE memory_items_fts MATCH ?
                      AND m.status NOT IN ('archived', 'superseded')
                """
                params: list[Any] = [match_query]
                if normalized_categories:
                    sql += f" AND m.category IN ({','.join('?' for _ in normalized_categories)})"
                    params.extend(normalized_categories)
                sql += """
                    ORDER BY bm25(memory_items_fts), m.importance DESC,
                             m.updated_at DESC, m.id ASC
                    LIMIT ?
                """
                params.append(limit * 6)
                _merge(conn.execute(sql, params).fetchall())

            if cjk_terms:
                like_clauses = []
                params = []
                for term in cjk_terms[:12]:
                    pattern = f"%{term}%"
                    like_clauses.append(
                        "(content LIKE ? OR entity LIKE ? OR category LIKE ?)"
                    )
                    params.extend([pattern, pattern, pattern])
                sql = """
                    SELECT *
                    FROM memory_items
                    WHERE status NOT IN ('archived', 'superseded')
                """
                if like_clauses:
                    sql += " AND (" + " OR ".join(like_clauses) + ")"
                if normalized_categories:
                    sql += f" AND category IN ({','.join('?' for _ in normalized_categories)})"
                    params.extend(normalized_categories)
                sql += """
                    ORDER BY importance DESC, updated_at DESC, id ASC
                    LIMIT ?
                """
                params.append(limit * 6)
                _merge(conn.execute(sql, params).fetchall())

        return list(results_by_id.values())[: limit * 6]

    # ── Maintenance ───────────────────────────────────────────────────────────

    def apply_decay(self, factor: float = shared.DECAY_FACTOR) -> None:
        """Decay importance of all entries; prune those below shared.MIN_IMPORTANCE."""
        affected_categories: set[str] = set()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE status NOT IN ('archived', 'superseded')"
            ).fetchall()
            for row in rows:
                entry = self._row_to_entry(row)
                affected_categories.add(self.normalize_category_name(entry.category))
                entry.decay(factor)
                entry.updated_at = _now()
                if entry.importance < shared.MIN_IMPORTANCE:
                    conn.execute("DELETE FROM memory_items WHERE id = ?", (entry.id,))
                    self._delete_fts_rows(conn, [entry.id])
                else:
                    affected_categories.update(self._write_entry_row(conn, entry))
        self._sync_after_mutation(affected_categories)

    def apply_retention(self) -> None:
        """Apply locus-aware retention: decay episodes in-database, leave others untouched.

        Previous implementation fetched ALL active rows into Python just to
        skip ~90% of them.  This version pushes the work into SQL, touching
        only the rows that need to change.
        """
        now = _now()
        with self._connect() as conn:
            # Step 1: decay importance for all active episodes in a single UPDATE.
            conn.execute(
                """
                UPDATE memory_items
                SET importance = importance * ?,
                    updated_at  = ?
                WHERE category = 'episodes'
                  AND status NOT IN ('archived', 'superseded')
                """,
                (shared.DECAY_FACTOR, now),
            )
            # Step 2: archive episodes that fell below the importance floor.
            archived_ids = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE category = 'episodes'
                      AND importance < ?
                      AND status NOT IN ('archived', 'superseded')
                    """,
                    (shared.MIN_IMPORTANCE,),
                ).fetchall()
            ]
            if archived_ids:
                conn.execute(
                    f"""
                    UPDATE memory_items
                    SET status     = 'archived',
                        updated_at = ?
                    WHERE id IN ({",".join("?" for _ in archived_ids)})
                    """,
                    (now, *archived_ids),
                )
                self._delete_fts_rows(conn, archived_ids)
        self._sync_after_mutation({"episodes"})

    def maintenance_snapshot(self, limit: int = 20) -> str:
        """Return a text summary derived from the structured store, not markdown projections."""
        entries = self.all_entries()[:limit]
        lines = []
        for entry in entries:
            anchor = (
                f"{entry.category}/{entry.entity}" if entry.entity else entry.category
            )
            lines.append(f"- [{anchor}] ({entry.memory_type}) {entry.content}")
        return "\n".join(lines)

    def merge_categories(self, cat_a: str, cat_b: str, merged_name: str) -> None:
        """Merge cat_a and cat_b into merged_name, delete originals."""
        cat_a = self.normalize_category_name(cat_a)
        cat_b = self.normalize_category_name(cat_b)
        merged_name = self.normalize_category_name(merged_name)
        with self._connect() as conn:
            row_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM memory_items WHERE category IN (?, ?)",
                    (cat_a, cat_b),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE memory_items SET category = ?, updated_at = ? WHERE category IN (?, ?)",
                (merged_name, _now(), cat_a, cat_b),
            )
            for entry_id in row_ids:
                self._sync_fts_row(conn, entry_id)
        self._sync_after_mutation({cat_a, cat_b, merged_name})


class LocalRetriever:
    """BM25-lite retrieval with importance boosting. Pure stdlib, no external deps."""

    K1: float = 1.5
    B: float = 0.75

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """Language-aware tokenizer for lexical recall and reranking."""
        return _lexical_terms(text)

    def score(
        self, query: str, entries: list[LTMEntry]
    ) -> list[tuple[LTMEntry, float]]:
        """Score entries against query using BM25-lite + importance boost."""
        if not entries:
            return []
        query_terms = self.tokenize(query)
        if not query_terms:
            return [(e, e.importance) for e in entries]

        N = len(entries)
        df: dict[str, int] = {}
        tokenized: list[list[str]] = []

        for entry in entries:
            tokens = self.tokenize(
                f"{entry.content} {entry.entity} {entry.category} {entry.memory_type}"
            )
            tokenized.append(tokens)
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        avg_dl = sum(len(t) for t in tokenized) / N if N else 1.0

        scored: list[tuple[LTMEntry, float]] = []
        for i, entry in enumerate(entries):
            tokens = tokenized[i]
            dl = len(tokens)
            tf_map: dict[str, int] = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            bm25 = 0.0
            for term in query_terms:
                if term not in tf_map:
                    continue
                idf = math.log(
                    (N - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1
                )
                tf = tf_map[term]
                tf_norm = (
                    tf
                    * (self.K1 + 1)
                    / (tf + self.K1 * (1 - self.B + self.B * dl / avg_dl))
                )
                bm25 += idf * tf_norm

            # Importance acts as a multiplicative boost
            scored.append((entry, bm25 * (1.0 + entry.importance)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def retrieve(
        self, query: str, entries: list[LTMEntry], top_k: int = shared.RETRIEVAL_TOP_K
    ) -> list[LTMEntry]:
        """Return top-K most relevant entries (score > 0 only)."""
        scored = self.score(query, entries)
        return [entry for entry, s in scored[:top_k] if s > 0]


class ConsolidationEngine:
    """LLM-driven context consolidation — the 'sleep' mechanism.

    Triggered when working memory exceeds shared.SLEEP_TOKEN_RATIO of max_tokens.
    Extracts structured facts from conversation, stores in LTM, applies decay,
    and compresses ctx.messages to the most recent entries.
    """

    def __init__(
        self,
        store: LTMStore,
        max_categories: int = shared.MAX_CATEGORIES,
        decay_factor: float = shared.DECAY_FACTOR,
        sleep_token_ratio: float = shared.SLEEP_TOKEN_RATIO,
        keep_last_messages: int = 6,
        max_source_tokens: int = shared.CONSOLIDATION_MAX_SOURCE_TOKENS,
        chars_per_token: float = float(shared.CHARS_PER_TOKEN),
        cjk_chars_per_token: float = 1.0,
    ):
        self.store = store
        self.max_categories = max_categories
        self.decay_factor = decay_factor
        self.sleep_token_ratio = sleep_token_ratio
        self.keep_last_messages = keep_last_messages
        self.max_source_tokens = max(1, int(max_source_tokens))
        # Token estimation ratios — configurable for different script systems.
        # chars_per_token:     non-CJK chars per token (Latin/ASCII, default 4)
        # cjk_chars_per_token: CJK chars per token (Hanzi/Kana/Hangul, default 1)
        self.chars_per_token: float = max(0.1, float(chars_per_token))
        self.cjk_chars_per_token: float = max(0.1, float(cjk_chars_per_token))

    # ── Trigger ───────────────────────────────────────────────────────────────

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Token estimate with CJK-awareness.

        Non-CJK text:    ``len(text) / chars_per_token``   (default 4 chars/token)
        CJK characters:  ``len(cjk) / cjk_chars_per_token`` (default 1 char/token)

        Both ratios are configurable via ``context.consolidation.token_estimation``
        in config.json so they can be tuned for different languages and model
        tokenisers.  Without the CJK distinction the estimate for Chinese
        conversations is ~4x too low, causing the compact trigger to fire far
        later than intended.  Also counts tool_use ``input`` payloads which the
        previous implementation silently ignored.
        """
        def _count(text: str) -> int:
            cjk = count_cjk_chars(text)
            non_cjk = len(text) - cjk
            return int(cjk / self.cjk_chars_per_token) + int(
                non_cjk / self.chars_per_token
            )

        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += _count(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    # text / tool_result content
                    text_val = block.get("text", "") or block.get("content", "")
                    total += _count(str(text_val))
                    # tool_use input JSON (was previously ignored)
                    inp = block.get("input")
                    if inp is not None:
                        total += _count(json.dumps(inp, ensure_ascii=False))
        return total

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        return self.estimate_tokens(messages) >= int(
            max_tokens * self.sleep_token_ratio
        )

    def _message_lines_for_llm(self, messages: list[dict]) -> list[str]:
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = [
                    block.get("text", "") or block.get("content", "")
                    for block in content
                    if isinstance(block, dict)
                ]
                content = " ".join(str(p) for p in parts)
            lines.append(f"{role}: {content}")
        return lines

    def _chunk_messages_for_llm(self, messages: list[dict]) -> list[str]:
        """Return bounded conversation chunks for extraction prompts.

        We use a conservative one-char-per-token upper bound so no chunk can
        exceed the configured budget, even for CJK-heavy inputs.
        """
        max_chars = self.max_source_tokens
        chunks: list[str] = []
        current_lines: list[str] = []
        current_len = 0

        for line in self._message_lines_for_llm(messages):
            segments = [line[i : i + max_chars] for i in range(0, len(line), max_chars)]
            if not segments:
                segments = [line]
            for segment in segments:
                extra_len = len(segment) if not current_lines else len(segment) + 2
                if current_lines and current_len + extra_len > max_chars:
                    chunks.append("\n\n".join(current_lines))
                    current_lines = [segment]
                    current_len = len(segment)
                else:
                    current_lines.append(segment)
                    current_len += extra_len

        if current_lines:
            chunks.append("\n\n".join(current_lines))
        return chunks

    def _build_consolidation_prompt(
        self,
        conversation_text: str,
        source_label: str,
        chunk_index: int,
        chunk_count: int,
    ) -> str:
        chunk_label = (
            f"{source_label}, chunk {chunk_index}/{chunk_count}"
            if chunk_count > 1
            else source_label
        )
        return (
            f"Analyze this conversation and extract important facts worth remembering.\n"
            f"For each item output JSON on its own line (no markdown fences):\n"
            f'{{"locus": "<one of {", ".join(shared.PALACE_LOCI)}>", "entity": "<anchor>", '
            f'"memory_type": "<type>", "content": "<fact>", "importance": <0.1-1.0>, "confidence": <0.1-1.0>}}\n\n'
            f"Rules:\n"
            f"- Use only the fixed loci listed above; never invent new top-level loci\n"
            f"- identity: durable identity facts; use entity='user' for user facts and entity='assistant' for agent self-identity\n"
            f"- projects: project decisions/state/risks\n"
            f"- people: person-specific facts\n"
            f"- concepts: durable domain knowledge\n"
            f"- tasks: commitments, next steps, open loops\n"
            f"- procedures: repeatable workflows and preferred methods\n"
            f"- archive: only if the memory is historical or superseded\n"
            f"- If the user gives the agent a stable name/role/identity, store it under identity with entity='assistant' and memory_type='self_identity'\n"
            f"- Do not output episodes; session summary is generated separately\n"
            f"- Be selective: max 8 items, 1-2 sentences each\n\n"
            f"Conversation ({chunk_label}):\n{conversation_text}"
        )

    # ── Main consolidation ────────────────────────────────────────────────────

    async def consolidate(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
        keep_last: Optional[int] = None,
        staging: Optional["StagingBuffer"] = None,
    ) -> ConsolidationResult:
        """One sleep cycle: extract → classify → store → decay → compress.

        Source priority for LLM extraction:
          1. staging buffer (if non-empty) — full, clean conversation history
          2. ctx.messages fallback          — used only when staging is absent
        After extraction the staging buffer is cleared.
        """
        if keep_last is None:
            keep_last = self.keep_last_messages
        shared.CONSOLE.print("[dim]💤 Context consolidation (sleep)...[/dim]")

        # Choose extraction source
        staged = staging.read_all() if staging else []
        source = staged if staged else messages
        if not source:
            compressed = (
                messages[-keep_last:] if len(messages) > keep_last else messages
            )
            if messages:  # only print if there was something to compress
                shared.CONSOLE.print(
                    f"[dim]💤 Messages compressed: {len(messages)} → {len(compressed)}[/dim]"
                )
            return ConsolidationResult(success=True, compressed_messages=compressed)
        source_label = (
            f"staging ({len(staged)} turns)"
            if staged
            else f"messages ({len(messages)})"
        )
        conversation_chunks = self._chunk_messages_for_llm(source)

        try:
            raw_responses: list[str] = []
            for idx, chunk_text in enumerate(conversation_chunks, start=1):
                prompt = self._build_consolidation_prompt(
                    chunk_text, source_label, idx, len(conversation_chunks)
                )
                if api_format == "anthropic":
                    resp = await client.messages.create(
                        model=model,
                        max_tokens=1024,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw_responses.append(resp.content[0].text)
                else:
                    resp = await client.chat.completions.create(
                        model=model,
                        max_tokens=1024,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw_responses.append(resp.choices[0].message.content or "")

            entries = [
                self._build_episode_entry(source, staging.session_id if staging else "")
            ]
            for raw in raw_responses:
                entries.extend(self._parse_entries(raw))
            self.store.add_entries(entries)

            self.store.apply_retention()

            # Clear staging after successful extraction
            if staging and staged:
                staging.drop_prefix(len(staged))

            shared.CONSOLE.print(
                f"[dim]💤 Stored {len(entries)} entries from {source_label} "
                f"across {len(conversation_chunks)} chunk(s). "
                f"Dynamic categories: {self.store.dynamic_category_count()}/{self.max_categories}[/dim]"
            )
            success = True
        except Exception as e:
            shared.CONSOLE.print(f"[dim]Sleep extraction error: {e}[/dim]")
            success = False

        compressed = messages[-keep_last:] if len(messages) > keep_last else messages
        if messages:
            # Only print when there is actual working memory to compress; skip the
            # "0 → 0" line that appears when consolidate() is called from the
            # background job path (which passes messages=[]).
            shared.CONSOLE.print(
                f"[dim]💤 Messages compressed: {len(messages)} → {len(compressed)}[/dim]"
            )
        return ConsolidationResult(
            success=success,
            compressed_messages=compressed,
            stored_entries=len(entries) if success else 0,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _format_messages_for_llm(self, messages: list[dict]) -> str:
        return "\n\n".join(self._message_lines_for_llm(messages))

    @staticmethod
    def _infer_identity_entity(content: str, memory_type: str, entity: str) -> str:
        explicit = str(entity or "").strip().lower()
        if explicit:
            return explicit
        normalized_type = str(memory_type or "").strip().lower()
        if normalized_type in {"self_identity", "assistant_identity"}:
            return "assistant"

        lowered = str(content or "").strip().lower()
        assistant_markers = (
            "assistant",
            "agent",
            "bot",
            "your name",
            "你叫",
            "你的名字",
            "助手",
            "机器人",
        )
        user_markers = (
            "user",
            "the user",
            "用户",
            "我喜欢",
            "我通常",
            "prefers",
        )
        assistant_hit = any(marker in lowered for marker in assistant_markers)
        user_hit = any(marker in lowered for marker in user_markers)
        if assistant_hit and not user_hit:
            return "assistant"
        return "user"

    def _parse_entries(self, raw: str) -> list[LTMEntry]:
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                content = data.get("content", "").strip()
                if not content:
                    continue
                category = data.get("locus") or data.get("category") or "concepts"
                normalized_category = self.store.normalize_category_name(category)
                memory_type = str(data.get("memory_type", "fact")).strip() or "fact"
                entity = str(data.get("entity", "")).strip()
                if normalized_category == "identity":
                    entity = self._infer_identity_entity(content, memory_type, entity)
                if normalized_category not in shared.PALACE_LOCI:
                    entity = entity or normalized_category
                    normalized_category = "concepts"
                entries.append(
                    LTMEntry(
                        id=_new_id(),
                        content=content,
                        importance=float(data.get("importance", 0.5)),
                        category=normalized_category,
                        created_at=_now(),
                        updated_at=_now(),
                        entity=entity,
                        memory_type=memory_type,
                        source_session=str(data.get("source_session", "")).strip(),
                        confidence=float(data.get("confidence", 1.0)),
                    )
                )
            except Exception:
                continue
        return entries

    def _build_episode_entry(
        self, messages: list[dict], session_id: str = ""
    ) -> LTMEntry:
        snippets = []
        for msg in messages[-6:]:
            role = str(msg.get("role", "unknown")).upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(block.get("text", "") or block.get("content", ""))
                    for block in content
                    if isinstance(block, dict)
                )
            content = str(content).strip()
            if content:
                snippets.append(f"{role}: {content[:180]}")
        summary = " | ".join(snippets)[:1200] or "Session summary unavailable."
        return LTMEntry(
            id=_new_id(),
            content=summary,
            importance=0.7,
            category="episodes",
            entity=session_id or "session",
            memory_type="session_summary",
            source_session=session_id,
            confidence=1.0,
            created_at=_now(),
            updated_at=_now(),
        )


class ContextManager:
    """Orchestrates LTM storage, retrieval, and consolidation.

    Trigger rules (all require _needs_consolidation == True):
      1. Token-ratio trigger  — working memory > token_ratio × max_tokens
      2. Idle trigger         — no activity for idle_seconds (background task)
      3. Session-end trigger  — explicit call when the interactive loop exits
    After each sleep() the flag is cleared; mark_activity() re-arms it.

    Staging buffer: every user/assistant turn is appended to a per-session
    staging buffer. The default backend is SQLite under ``palace.db`` with a
    legacy JSONL fallback for explicit file-based callers. This ensures
    consolidation always has a complete source even if the session ends before
    the token threshold fires, without mixing raw turns across unrelated
    sessions. Buffer is drained only after confirmed successful consolidation.
    """

    def __init__(
        self,
        store: LTMStore,
        retriever: LocalRetriever,
        consolidation: ConsolidationEngine,
        idle_seconds: int = 300,
        min_messages: int = 4,
        staging: Optional[StagingBuffer] = None,
        staging_turn_threshold: int = shared.STAGING_TURN_THRESHOLD,
        staging_token_threshold: int = shared.STAGING_TOKEN_THRESHOLD,
        route_keywords: Optional[dict[str, list[str] | tuple[str, ...]]] = None,
    ):
        self.store = store
        self.retriever = retriever
        self.consolidation = consolidation
        self.idle_seconds = idle_seconds
        self.min_messages = min_messages
        self.staging_turn_threshold = staging_turn_threshold
        self.staging_token_threshold = staging_token_threshold
        self.staging: StagingBuffer = staging or StagingBuffer()
        source_keywords = route_keywords or shared.DEFAULT_ROUTE_KEYWORDS
        self.route_keywords = {
            category: tuple(str(keyword).lower() for keyword in keywords)
            for category, keywords in source_keywords.items()
        }
        self._needs_consolidation: bool = False
        self._last_activity: float = 0.0
        self._lock = threading.RLock()
        self._jobs: deque[dict[str, Any]] = deque()
        self._processing_job = False

    @staticmethod
    def _coerce_consolidation_result(result: Any) -> ConsolidationResult:
        if isinstance(result, ConsolidationResult):
            return result
        if isinstance(result, list):
            return ConsolidationResult(success=True, compressed_messages=result)
        return ConsolidationResult(success=bool(result), compressed_messages=[])

    def spawn_session(self, session_id: Optional[str] = None) -> "ContextManager":
        """Create a session-scoped manager that shares durable memory primitives.

        Channel transports may multiplex many independent chats through one
        process. Those chats should share the same long-term store and
        consolidation rules, but they must not share staging buffers, idle
        timers, or dirty flags.
        """
        if self.staging.path.parent.name == "_staging":
            context_dir = self.staging.path.parent.parent
        else:
            context_dir = self.staging.path.parent
        staging = StagingBuffer(context_dir=context_dir, session_id=session_id)
        return ContextManager(
            store=self.store,
            retriever=self.retriever,
            consolidation=self.consolidation,
            idle_seconds=self.idle_seconds,
            min_messages=self.min_messages,
            staging=staging,
            staging_turn_threshold=self.staging_turn_threshold,
            staging_token_threshold=self.staging_token_threshold,
            route_keywords=dict(self.route_keywords),
        )

    # ── Activity tracking ─────────────────────────────────────────────────────

    def mark_activity(self) -> None:
        """Call after each user message to arm consolidation and reset idle timer."""
        with self._lock:
            self._last_activity = time.time()
            self._needs_consolidation = True

    @staticmethod
    def _matched_fact_alias_terms(
        lowered_query: str,
        aliases: dict[str, tuple[str, ...]],
    ) -> tuple[list[str], set[str]]:
        targets: list[str] = []
        matched_terms: set[str] = set()
        for key, variants in aliases.items():
            hits = [variant for variant in variants if variant in lowered_query]
            if not hits:
                continue
            targets.append(key)
            for hit in hits:
                terms = _lexical_terms(hit)
                if terms:
                    matched_terms.update(terms)
                else:
                    matched_terms.add(hit)
        return targets, matched_terms

    def _plan_query(self, query: str) -> QueryPlan:
        lexical_terms = tuple(_lexical_terms(query))
        if self._is_episode_recall_query(query):
            return QueryPlan(
                query_type="event_recall",
                scope="current_session",
                lexical_terms=lexical_terms,
                allow_freeform_fallback=False,
            )

        lowered_query = str(query or "").strip().lower()
        subjects, subject_terms = self._matched_fact_alias_terms(
            lowered_query,
            _FACT_QUERY_SUBJECT_ALIASES,
        )
        predicates, predicate_terms = self._matched_fact_alias_terms(
            lowered_query,
            _FACT_QUERY_PREDICATE_ALIASES,
        )
        if not subjects and predicates:
            if "你" in lowered_query or "your" in lowered_query or "assistant" in lowered_query:
                subjects = ["assistant"]

        if not subjects and not predicates:
            return QueryPlan(
                query_type="freeform_context",
                scope="global",
                lexical_terms=lexical_terms,
                allow_freeform_fallback=True,
            )

        residual_terms = tuple(
            term
            for term in lexical_terms
            if term not in subject_terms and term not in predicate_terms
        )
        return QueryPlan(
            query_type="mixed" if residual_terms else "fact_lookup",
            scope="global",
            target_subjects=tuple(subjects),
            target_predicates=tuple(predicates),
            lexical_terms=lexical_terms,
            allow_freeform_fallback=True,
        )

    def _extract_turn_fact_assertions(
        self,
        *,
        role: str,
        content: str,
        session_id: str,
        channel: str,
        source_id: str = "",
    ) -> list[FactAssertion]:
        name = _extract_assistant_name(content)
        if not name:
            return []
        normalized_role = str(role or "").strip().lower()
        return [
            FactAssertion(
                id=_new_id(),
                subject="assistant",
                predicate="name",
                value=name,
                source_kind=(
                    "user_statement" if normalized_role == "user" else "conversation_turn"
                ),
                source_id=source_id,
                source_session=session_id,
                channel=channel,
                confidence=1.0 if normalized_role == "user" else 0.8,
            )
        ]

    def record_turn(
        self,
        *,
        user_content: str,
        assistant_content: str = "",
        channel: str = "",
        message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist the completed exchange as durable event history."""
        session_id = self.staging.session_id
        self.store.append_conversation_turn(
            session_id=session_id,
            role="user",
            content=user_content,
            channel=channel,
            message_id=message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
        )
        self.store.append_conversation_turn(
            session_id=session_id,
            role="assistant",
            content=assistant_content,
            channel=channel,
            reply_to_id=message_id,
            metadata=metadata,
        )
        for assertion in self._extract_turn_fact_assertions(
            role="user",
            content=user_content,
            session_id=session_id,
            channel=channel,
            source_id=message_id,
        ):
            self.store.add_fact_assertion(assertion)
        for assertion in self._extract_turn_fact_assertions(
            role="assistant",
            content=assistant_content,
            session_id=session_id,
            channel=channel,
            source_id=reply_to_id or message_id,
        ):
            self.store.add_fact_assertion(assertion)

    def idle_elapsed(self) -> float:
        """Seconds since last activity (0 if never active)."""
        with self._lock:
            if self._last_activity == 0.0:
                return 0.0
            return time.time() - self._last_activity

    # ── Trigger checks ────────────────────────────────────────────────────────

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        """Token-ratio trigger: only fires when dirty and messages are sufficient."""
        with self._lock:
            if not self._needs_consolidation:
                return False
        if len(messages) < self.min_messages:
            return False
        return self.consolidation.should_sleep(messages, max_tokens)

    def should_idle_sleep(self, messages: list[dict]) -> bool:
        """Idle trigger: fires when dirty, sufficient messages, and idle long enough."""
        with self._lock:
            if not self._needs_consolidation:
                return False
        if len(messages) < self.min_messages:
            return False
        return self.idle_elapsed() >= self.idle_seconds

    def should_session_end_sleep(self) -> bool:
        """Session-end trigger: fires when staging has at least one complete turn.

        Requires count >= 2 (one user + one assistant message) to ensure
        there is a complete exchange worth extracting.  count == 1 would mean
        a bare user message with no response was staged, which is not worth
        an LLM extraction call.
        """
        with self._lock:
            return self._needs_consolidation and self.staging.count() >= 2

    def should_enqueue_consolidation(self) -> bool:
        """Queue consolidation based on staged content volume, not working-memory size."""
        with self._lock:
            if not self._needs_consolidation:
                return False
            count = self.staging.count()
        # Fast path: count() is an in-memory counter; no file I/O needed.
        if count >= self.staging_turn_threshold:
            return True
        # Slow path: require at least min_messages staged entries before checking
        # tokens.  Without this guard a single verbose response (common with CJK
        # text, where ~1 char ≈ 1 estimated token) crosses the 2100-token threshold
        # on every turn, causing consolidation to fire every turn even though only
        # two entries have accumulated since the last job ran.
        if count < self.min_messages:
            return False
        # Only read the file once we know there are enough entries to warrant it.
        staged = self.staging.read_all()
        if not staged:
            return False
        return (
            self.consolidation.estimate_tokens(staged) >= self.staging_token_threshold
        )

    def enqueue_consolidation(self, reason: str) -> None:
        """Queue one consolidation job if there is staged work pending."""
        self.enqueue_staging_job(reason, self.staging)

    def enqueue_staging_job(self, reason: str, staging: "StagingBuffer") -> None:
        """Queue consolidation for an explicit staging buffer."""
        with self._lock:
            if staging.count() == 0:
                return
            staging_path = str(staging.path.resolve())
            session_id = staging.session_id
            if any(
                job.get("staging_path", str(self.staging.path.resolve()))
                == staging_path
                and job.get("session_id", self.staging.session_id) == session_id
                for job in self._jobs
            ):
                return
            self._jobs.append(
                {
                    "reason": reason,
                    "session_id": session_id,
                    "staging_path": staging_path,
                    "staging_backend": (
                        "sqlite"
                        if getattr(staging, "_sqlite_backed", False)
                        else "jsonl"
                    ),
                    "context_dir": str(
                        getattr(staging, "context_dir", self.staging.context_dir).resolve()
                    ),
                    "queued_at": _now(),
                }
            )

    def next_job(self, pop: bool = False) -> Optional[dict]:
        with self._lock:
            if not self._jobs:
                if self._needs_consolidation and self.staging.count() > 0:
                    self._jobs.append(
                        {
                            "reason": "idle",
                            "session_id": self.staging.session_id,
                            "queued_at": _now(),
                        }
                    )
                else:
                    return None
            return self._jobs.popleft() if pop else dict(self._jobs[0])

    def pending_jobs(self) -> int:
        with self._lock:
            return len(self._jobs)

    def _job_staging(self, job: dict) -> tuple["StagingBuffer", bool]:
        backend = str(job.get("staging_backend", "jsonl"))
        path_value = job.get("staging_path")
        session_id = str(job.get("session_id", self.staging.session_id))
        if backend == "sqlite":
            context_dir = Path(
                job.get("context_dir", str(self.staging.context_dir))
            ).resolve()
            if (
                context_dir == self.staging.context_dir.resolve()
                and session_id == self.staging.session_id
                and getattr(self.staging, "_sqlite_backed", False)
            ):
                return self.staging, True
            return StagingBuffer(context_dir=context_dir, session_id=session_id), False
        if not path_value:
            return self.staging, True
        path = Path(path_value).resolve()
        if (
            path == self.staging.path.resolve()
            and session_id == self.staging.session_id
        ):
            return self.staging, True
        return StagingBuffer(path=path, session_id=session_id), False

    def should_process_jobs(self) -> bool:
        with self._lock:
            has_pending = bool(self._jobs)
            # Require enough staged entries to be worth an LLM extraction call.
            # count > 0 is not sufficient: the idle path would fire after any pause
            # of idle_seconds even with a single staged turn.  Using
            # staging_turn_threshold makes the implicit idle enqueue consistent
            # with the explicit fast-path in should_enqueue_consolidation().
            has_staged_work = (
                self._needs_consolidation
                and self.staging.count() >= self.staging_turn_threshold
            )
        return (
            has_pending or has_staged_work
        ) and self.idle_elapsed() >= self.idle_seconds

    def should_compact_messages(self, messages: list[dict], max_tokens: int) -> bool:
        """Front-end compaction keeps working memory bounded without network calls."""
        if len(messages) < self.min_messages:
            return False
        return self.consolidation.should_sleep(messages, max_tokens)

    def compact_messages(self, messages: list[dict]) -> list[dict]:
        keep_last = self.consolidation.keep_last_messages
        if len(messages) <= keep_last:
            return messages
        _before = len(messages)
        ideal = len(messages) - keep_last
        for i in range(ideal, -1, -1):
            msg = messages[i]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                ideal = i
                break
        compacted = messages[ideal:]
        _emit_consolidation(
            "compaction",
            messages_before=_before,
            messages_after=len(compacted),
        )
        return compacted

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def _is_episode_recall_query(self, query: str) -> bool:
        q = query.lower()

        explicit_keywords = self.route_keywords.get("episodes", ())
        if any(keyword in q for keyword in explicit_keywords):
            return True

        zh_phrases = (
            "对话历史",
            "聊天内容",
            "聊天记录",
            "刚才的对话",
            "刚刚的对话",
            "本次会话",
            "这次会话",
        )
        if any(phrase in q for phrase in zh_phrases):
            return True

        zh_recent = ("刚才", "刚刚", "上次", "之前")
        zh_conversation = ("聊", "说", "提", "问", "讨论", "对话", "聊天")
        if any(marker in q for marker in zh_recent) and any(
            marker in q for marker in zh_conversation
        ):
            return True

        en_phrases = (
            "conversation history",
            "chat history",
            "recent conversation",
            "earlier conversation",
            "previous conversation",
            "current conversation",
        )
        if any(phrase in q for phrase in en_phrases):
            return True

        en_recent = ("just", "earlier", "recently", "previously", "last time")
        en_conversation = ("talk", "chat", "discuss", "say", "ask", "conversation")
        if any(marker in q for marker in en_recent) and any(
            marker in q for marker in en_conversation
        ):
            return True
        return False

    def _route_categories(self, query: str) -> list[str]:
        q = query.lower()
        routes: list[str] = []
        if self._is_episode_recall_query(query):
            routes.append("episodes")
        for category, keywords in self.route_keywords.items():
            if category == "episodes":
                continue
            if any(keyword in q for keyword in keywords):
                if category == "projects":
                    routes.extend(["projects", "tasks"])
                else:
                    routes.append(category)
        seen = []
        for cat in routes:
            if cat not in seen:
                seen.append(cat)
        return seen

    @staticmethod
    def _format_fact_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _targeted_fact_conflict(self, plan: QueryPlan) -> bool:
        if not plan.target_subjects or not plan.target_predicates:
            return False
        for subject in plan.target_subjects:
            for predicate in plan.target_predicates:
                if self.store.has_conflicted_fact(subject, predicate, plan.scope):
                    return True
        return False

    @staticmethod
    def _has_multi_intent_marker(query: str) -> bool:
        lowered = str(query or "").lower()
        return any(
            marker in lowered
            for marker in (
                " and ",
                " also ",
                " plus ",
                "另外",
                "还有",
                "以及",
                "顺便",
                "并且",
            )
        )

    def _should_include_freeform_context(
        self,
        *,
        query: str,
        plan: QueryPlan,
        has_fact_hits: bool,
    ) -> bool:
        if plan.query_type == "event_recall":
            return False
        if self._targeted_fact_conflict(plan):
            return False
        if plan.query_type == "fact_lookup":
            return not has_fact_hits
        if plan.query_type == "mixed":
            if not has_fact_hits:
                return True
            return self._has_multi_intent_marker(query)
        return True

    def _resolved_fact_candidates(
        self,
        query: str,
        *,
        plan: Optional[QueryPlan] = None,
        top_k: int = shared.RETRIEVAL_TOP_K,
    ) -> list[ResolvedFact]:
        plan = plan or self._plan_query(query)
        if plan.query_type not in {"fact_lookup", "mixed"}:
            return []
        facts = self.store.read_resolved_facts()
        if not facts:
            return []

        candidates: list[ResolvedFact] = []
        for fact in facts:
            if plan.target_subjects and fact.subject not in plan.target_subjects:
                continue
            if plan.target_predicates and fact.predicate not in plan.target_predicates:
                continue
            candidates.append(fact)
        if not candidates:
            return []

        query_terms = set(plan.lexical_terms)

        def score(fact: ResolvedFact) -> float:
            value_terms = set(_lexical_terms(self._format_fact_value(fact.value)))
            total = float(fact.confidence or 0.0)
            if fact.subject in plan.target_subjects:
                total += 3.0
            if fact.predicate in plan.target_predicates:
                total += 3.0
            total += 0.25 * len(query_terms & value_terms)
            return total

        ranked = sorted(candidates, key=score, reverse=True)
        return [fact for fact in ranked[:top_k] if score(fact) > 0]

    def retrieve_resolved_fact_context(
        self,
        query: str,
        *,
        top_k: int = shared.RETRIEVAL_TOP_K,
        plan: Optional[QueryPlan] = None,
        title: str = "## Resolved Facts",
    ) -> str:
        facts = self._resolved_fact_candidates(query, plan=plan, top_k=top_k)
        if not facts:
            return ""
        lines = [title]
        for fact in facts:
            lines.append(
                f"- {fact.subject}.{fact.predicate} = {self._format_fact_value(fact.value)}"
            )
        return "\n".join(lines)

    def retrieve_ltm_context(self, query: str, top_k: int = shared.RETRIEVAL_TOP_K) -> str:
        """Return top-K relevant LTM entries as an injectable string.

        Two-stage retrieval:
          1. SQLite FTS5 fetches a broad candidate set via BM25.
          2. LocalRetriever re-ranks candidates with importance-boosted BM25.
          3. Routed categories receive a small score bonus rather than hard filtering.

        This keeps keyword routing useful without hiding relevant memories that
        live outside the routed categories.
        """
        categories = self._route_categories(query)
        candidates = self.store.search_entries(query, categories=None, limit=top_k * 6)
        if not candidates and self._is_episode_recall_query(query):
            candidates = self.store.read_entries("episodes")[: top_k * 3]
        if not candidates:
            return ""
        scored = self.retriever.score(query, candidates)
        if categories:
            routed = set(categories)
            scored = [
                (
                    entry,
                    score * (1.15 if entry.category in routed else 1.0),
                )
                for entry, score in scored
            ]
            scored.sort(key=lambda item: item[1], reverse=True)
        top = [entry for entry, score in scored[:top_k] if score > 0]
        if not top:
            return ""
        lines = ["## Retrieved Context (from long-term memory)"]
        for e in top:
            anchor = f"{e.category}/{e.entity}" if e.entity else e.category
            lines.append(f"- [{anchor}] {e.content}")
        return "\n".join(lines)

    def _recent_session_context(self, limit: int = shared.RECENT_SESSION_TURNS) -> str:
        """Return the most recent staged turns for explicit current-session recall."""
        staged = self.staging.read_all()
        if not staged:
            # Staging may be empty after a restart (sleep archived turns to
            # conversation_turns).  Fall back to the durable store so the
            # agent still sees recent conversation history.
            turns = self.store.recent_conversation_turns(
                session_id=self.staging.session_id,
                limit=limit,
            )
            if not turns:
                return ""
            lines = ["## Previous Session (restored from history)"]
            for turn in turns:
                content = turn.content.strip()
                if content:
                    lines.append(f"- {turn.role.upper()}: {content}")
            return "\n".join(lines) if len(lines) > 1 else ""
        lines = ["## Current Session (not yet consolidated)"]
        for msg in staged[-limit:]:
            role = str(msg.get("role", "unknown")).upper()
            content = str(msg.get("content", "")).strip()
            if content:
                lines.append(f"- {role}: {content}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def retrieve_history_context(
        self,
        query: str,
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> str:
        """Return durable event-history evidence when the query asks for it."""
        if not self._is_episode_recall_query(query):
            return ""
        turns = self.store.recent_conversation_turns(
            session_id=self.staging.session_id,
            limit=limit,
        )
        if not turns:
            return ""
        lines = ["## Conversation History"]
        for turn in turns:
            content = turn.content.strip()
            if content:
                lines.append(f"- {turn.role.upper()}: {content}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _recent_unconsolidated_context(
        self,
        current_messages: Optional[list[dict]] = None,
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> str:
        staged = self.staging.read_all()
        if not staged:
            return ""
        if current_messages is None:
            return ""
        visible_contents: set[str] = set()
        for msg in current_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    visible_contents.add(text)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = str(
                    block.get("text", "") or block.get("content", "")
                ).strip()
                if text:
                    visible_contents.add(text)
        staged_contents = [
            str(msg.get("content", "")).strip()
            for msg in staged[-limit:]
            if str(msg.get("content", "")).strip()
        ]
        if not staged_contents or all(text in visible_contents for text in staged_contents):
            return ""
        lines = ["## Current Session (not yet consolidated)"]
        for msg in staged[-limit:]:
            role = str(msg.get("role", "unknown")).upper()
            content = str(msg.get("content", "")).strip()
            if content:
                lines.append(f"- {role}: {content}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _assistant_identity_context(self, limit: int = 3) -> str:
        name_conflict = self.store.has_conflicted_fact("assistant", "name")
        role_conflict = self.store.has_conflicted_fact("assistant", "role")
        facts = [
            fact
            for fact in self.store.read_resolved_facts(subject="assistant")
            if fact.predicate in {"name", "role", "identity_note"}
            and not (name_conflict and fact.predicate in {"name", "identity_note"})
            and not (role_conflict and fact.predicate == "role")
        ]
        if facts:
            lines = ["## Assistant Identity"]
            for fact in facts[: max(1, limit)]:
                lines.append(
                    f"- {fact.subject}.{fact.predicate} = {self._format_fact_value(fact.value)}"
                )
            return "\n".join(lines)
        if name_conflict or role_conflict:
            return ""
        entries = self.store.read_entries_for_entity("identity", "assistant")
        if not entries:
            return ""
        lines = ["## Assistant Identity"]
        for entry in entries[: max(1, limit)]:
            content = entry.content.strip()
            if content:
                lines.append(f"- {content}")
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _format_working_state_text(state: dict[str, Any]) -> str:
        if not isinstance(state, dict) or not state:
            return ""
        fields = [
            ("active_goal", "Active goal"),
            ("status", "Status"),
            ("progress", "Progress"),
            ("next_action", "Next action"),
            ("last_error", "Last error"),
        ]
        lines = ["## Restored Working Context"]
        lines.append(
            "A prior working context exists for this session. Use it when relevant; "
            "if the user's new message is unrelated, do not force the old task."
        )
        for key, label in fields:
            value = str(state.get(key, "") or "").strip()
            if value:
                lines.append(f"- {label}: {value}")
        artifacts = state.get("artifacts")
        if isinstance(artifacts, list):
            clean_artifacts = [str(item).strip() for item in artifacts if str(item).strip()]
            if clean_artifacts:
                lines.append("- Artifacts: " + ", ".join(clean_artifacts[:8]))
        recent_turns = state.get("recent_turns")
        if isinstance(recent_turns, list) and recent_turns:
            lines.append("- Recent turns:")
            for turn in recent_turns[-4:]:
                if not isinstance(turn, dict):
                    continue
                role = str(turn.get("role", "") or "").upper()
                content = str(turn.get("content", "") or "").strip()
                if role and content:
                    lines.append(f"  - {role}: {content}")
        return "\n".join(lines) if len(lines) > 2 else ""

    def working_state_context(self) -> str:
        snapshot = self.store.load_session_working_state(self.staging.session_id)
        if snapshot is None:
            return ""
        text = self._format_working_state_text(snapshot.state)
        events = self.store.recent_agent_events(
            session_id=self.staging.session_id,
            limit=5,
        )
        event_lines = []
        for event in events[-5:]:
            detail = str(
                event.payload.get("error")
                or event.payload.get("content_preview")
                or event.payload.get("user_content")
                or ""
            ).strip()
            if detail:
                detail = self._clip_working_state_text(detail, 160)
                event_lines.append(f"- {event.event_type}: {detail}")
            else:
                event_lines.append(f"- {event.event_type}")
        if text and event_lines:
            text += "\n- Recent runtime events:\n" + "\n".join(
                f"  {line}" for line in event_lines
            )
        return text

    @staticmethod
    def _clip_working_state_text(text: str, limit: int = 800) -> str:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 1].rstrip() + "…"

    @staticmethod
    def _merge_artifacts(*groups: Any, limit: int = 12) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
                if len(merged) >= limit:
                    return merged
        return merged

    def _project_working_state_from_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> SessionWorkingState:
        previous = self.store.load_session_working_state(self.staging.session_id)
        prior_state = previous.state if previous is not None else {}
        payload = payload if isinstance(payload, dict) else {}
        user_content = str(payload.get("user_content", "") or "")
        assistant_content = str(payload.get("assistant_content", "") or "")
        error = str(payload.get("error", "") or "")
        clean_user = self._clip_working_state_text(user_content, 1000)
        clean_assistant = self._clip_working_state_text(assistant_content, 1000)
        clean_error = self._clip_working_state_text(error, 1000)

        recent_turns = list(prior_state.get("recent_turns", [])) if isinstance(prior_state.get("recent_turns"), list) else []
        if clean_user:
            recent_turns.append({"role": "user", "content": clean_user})
        if clean_assistant:
            recent_turns.append({"role": "assistant", "content": clean_assistant})
        recent_turns = recent_turns[-8:]

        status = "failed" if clean_error else "active"
        if clean_assistant and not clean_error:
            status = "updated"
        progress = clean_assistant or str(prior_state.get("progress", "") or "")
        next_action = str(prior_state.get("next_action", "") or "")
        if clean_error:
            next_action = "Recover from the last error and continue the active goal if the user's next message is related."
        elif clean_assistant:
            next_action = "Use this working context only when it is relevant to the user's next message."

        artifacts = self._merge_artifacts(
            payload.get("artifacts"),
            prior_state.get("artifacts"),
        )
        state = {
            "active_goal": clean_user or str(prior_state.get("active_goal", "") or ""),
            "status": status,
            "progress": progress,
            "next_action": next_action,
            "last_error": clean_error,
            "artifacts": artifacts,
            "recent_turns": recent_turns,
            "last_event_type": event_type,
            "updated_at": created_at,
        }
        return self.store.save_session_working_state(
            self.staging.session_id,
            state,
            updated_at=created_at,
        )

    def record_runtime_event(
        self,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        turn_id: str = "",
    ) -> AgentRuntimeEvent:
        """Record a runtime fact and update the model-readable projection.

        This is the single path for recoverable runtime context: append the
        factual event first, then derive ``session_working_state`` from it.
        """
        payload = payload if isinstance(payload, dict) else {}
        created_at = _now()
        event = self.store.append_agent_event(
            session_id=self.staging.session_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            created_at=created_at,
        )
        self._project_working_state_from_event(
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
        )
        return event

    def retrieve_context(self, query: str, top_k: int = shared.RETRIEVAL_TOP_K) -> str:
        """Return explicit context lookup results across active session and LTM."""
        plan = self._plan_query(query)
        sections = []
        history = self.retrieve_history_context(query)
        if history:
            sections.append(history)
        recent = "" if history else self._recent_session_context()
        if recent:
            sections.append(recent)
        facts = self.retrieve_resolved_fact_context(query, top_k=top_k, plan=plan)
        if facts:
            sections.append(facts)
        include_freeform = self._should_include_freeform_context(
            query=query,
            plan=plan,
            has_fact_hits=bool(facts),
        )
        ltm = self.retrieve_ltm_context(query, top_k=top_k) if include_freeform else ""
        if ltm:
            sections.append(ltm)
        return "\n\n".join(sections)

    def retrieve_implicit_context(
        self,
        query: str,
        top_k: int = shared.RETRIEVAL_TOP_K,
        current_messages: Optional[list[dict]] = None,
    ) -> str:
        """Return context for automatic prompt injection.

        Keep routine prompt augmentation focused on LTM, and only include the
        in-session staging buffer when the user is explicitly asking to recall
        recent conversation.
        """
        plan = self._plan_query(query)
        sections = []
        assistant_identity = self._assistant_identity_context()
        if assistant_identity:
            sections.append(assistant_identity)
        working_state = self.working_state_context()
        if working_state:
            sections.append(working_state)
        if "episodes" in self._route_categories(query):
            recent = self._recent_session_context()
            if recent:
                sections.append(recent)
        else:
            recent = self._recent_unconsolidated_context(
                current_messages=current_messages
            )
            if recent:
                sections.append(recent)
        facts = ""
        if plan.query_type in {"fact_lookup", "mixed"}:
            facts = self.retrieve_resolved_fact_context(query, top_k=top_k, plan=plan)
            if facts and facts not in sections:
                sections.append(facts)
        include_freeform = self._should_include_freeform_context(
            query=query,
            plan=plan,
            has_fact_hits=bool(facts),
        )
        ltm = self.retrieve_ltm_context(query, top_k=top_k) if include_freeform else ""
        if ltm:
            sections.append(ltm)
        return "\n\n".join(sections)

    # ── Consolidation ─────────────────────────────────────────────────────────

    async def sleep(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
    ) -> list[dict]:
        """Run one sleep cycle (uses staging as source), then clear dirty flag."""
        try:
            result = await self.consolidation.consolidate(
                messages, client, model, api_format, staging=self.staging
            )
            return self._coerce_consolidation_result(result).compressed_messages
        finally:
            with self._lock:
                self._needs_consolidation = False

    async def process_one_job(
        self,
        client: Any,
        model: str,
        api_format: str = "anthropic",
        extractor: Optional[Callable[..., list[Any]]] = None,
    ) -> bool:
        """Process one queued consolidation job without mutating working memory."""
        with self._lock:
            if self._processing_job:
                return False
            self._processing_job = True
        job = self.next_job(pop=True)
        if job is None:
            with self._lock:
                self._processing_job = False
            return False

        try:
            staging_buffer, is_primary_staging = self._job_staging(job)
            reason = str(job.get("reason", "?"))
            session_id = staging_buffer.session_id
            shared.CONSOLE.print(
                f"[dim]💤 Context consolidation (sleep)... reason={reason} "
                f"session={session_id}[/dim]"
            )
            with self._lock:
                staged = staging_buffer.read_all()
            if not staged:
                if is_primary_staging:
                    with self._lock:
                        self._needs_consolidation = False
                return False

            # Emit consolidation lifecycle event
            _emit_consolidation("started", reason=reason, staged_count=len(staged))

            if extractor is not None:
                entries = [
                    self.consolidation._build_episode_entry(
                        staged, staging_buffer.session_id
                    )
                ]
                extracted = extractor(staged, job)
                for item in extracted or []:
                    if isinstance(item, LTMEntry):
                        entries.append(item)
                    elif isinstance(item, dict):
                        lines = json.dumps(item, ensure_ascii=False)
                        entries.extend(self.consolidation._parse_entries(lines))
                self.store.add_entries(entries)
                self.store.apply_retention()
                staging_buffer.drop_prefix(len(staged))
                if is_primary_staging:
                    with self._lock:
                        self._needs_consolidation = False
                _emit_consolidation("completed", entries_extracted=len(entries))
                return True

            try:
                result = await self.consolidation.consolidate(
                    [],
                    client,
                    model,
                    api_format,
                    staging=staging_buffer,
                )
            except Exception as exc:
                _emit_consolidation("failed", reason="llm_extraction_error", error=str(exc))
                shared.CONSOLE.print(f"[dim]Sleep extraction error: {exc}[/dim]")
                return False
            consolidated = self._coerce_consolidation_result(result)
            if not consolidated.success:
                _emit_consolidation(
                    "failed", reason="extraction_returned_failure", error=consolidated.error
                )
                return False
            if is_primary_staging:
                with self._lock:
                    self._needs_consolidation = False
            _emit_consolidation(
                "completed", entries_extracted=len(getattr(consolidated, "entries", []))
            )
            return True
        except Exception as exc:
            _emit_consolidation("failed", reason="exception", error=str(exc))
            raise
        finally:
            with self._lock:
                self._processing_job = False

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        cats = self.store.list_categories()
        return {
            "dynamic_categories": self.store.dynamic_category_count(),
            "total_categories": len(cats),
            "total_entries": sum(c.entry_count for c in cats),
            "category_names": [c.name for c in cats],
            "max_categories": self.store.max_categories,
            "needs_consolidation": self._needs_consolidation,
            "queued_jobs": self.pending_jobs(),
            "staged_turns": self.staging.count(),
            "idle_elapsed_s": round(self.idle_elapsed()),
            "idle_threshold_s": self.idle_seconds,
        }


class BackgroundMemoryWorker:
    """Background thread that processes queued memory jobs during prompt idle time."""

    def __init__(
        self,
        ctx_mgr: ContextManager,
        client: Any,
        model: str,
        api_format: str,
        poll_seconds: float = 1.0,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.ctx_mgr = ctx_mgr
        self.client = client
        self.model = model
        self.api_format = api_format
        self.poll_seconds = poll_seconds
        self.client_factory = client_factory
        self._stop_event = threading.Event()
        # _wake_event lets callers interrupt the poll sleep and trigger an
        # immediate (idle-gate-bypassing) consolidation run without blocking
        # the main asyncio event loop.
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the worker thread once."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="background-memory-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker thread to stop."""
        self._stop_event.set()
        self._wake_event.set()  # unblock any ongoing wait() immediately

    def wake(self) -> None:
        """Interrupt the poll sleep so the worker runs its next job immediately.

        Safe to call from any thread or coroutine.  Does not block.
        When woken the worker bypasses the idle-seconds gate so consolidation
        happens right away rather than waiting up to idle_seconds for the next
        natural trigger.
        """
        self._wake_event.set()

    async def wait(self) -> None:
        """Wait for the worker thread to exit in async call sites."""
        if self._thread:
            await asyncio.to_thread(self._thread.join)

    def _run(self) -> None:
        client = self.client_factory() if self.client_factory else self.client
        try:
            while not self._stop_event.is_set():
                # Consume the wake signal before deciding whether to run, so a
                # signal arriving while a job is already running is not lost.
                on_demand = self._wake_event.is_set()
                self._wake_event.clear()
                try:
                    while not self._stop_event.is_set():
                        # on_demand bypasses the idle gate: once explicitly
                        # woken, drain the currently available queue rather than
                        # processing only a single job and falling back to the
                        # idle gate for the rest.
                        should_run = (
                            on_demand or self.ctx_mgr.should_process_jobs()
                        )
                        if not should_run:
                            break
                        processed = asyncio.run(
                            self.ctx_mgr.process_one_job(
                                client,
                                self.model,
                                api_format=self.api_format,
                            )
                        )
                        if not processed:
                            break
                        if not on_demand:
                            continue
                except Exception as e:
                    shared.CONSOLE.print(f"[dim]Background consolidation error: {e}[/dim]")
                # Sleep for poll_seconds OR until wake()/stop() interrupts,
                # whichever comes first.  This replaces the old _stop_event.wait()
                # so that wake() can also cut the sleep short.
                self._wake_event.wait(timeout=self.poll_seconds)
        finally:
            aclose = getattr(client, "aclose", None)
            if self.client_factory and callable(aclose):
                try:
                    asyncio.run(aclose())
                except Exception:
                    pass
