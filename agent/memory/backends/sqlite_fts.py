"""A second memory store: SQLite + FTS5, standard library only.

Why this exists
---------------
"Replaceable" is a claim until something replaces it.  This is the second
implementation behind ``agent/memory/contracts.py``: the same 32 members, the
same observable semantics, a different storage engine (one SQLite file, FTS5 for
search, no embeddings and no dependency beyond the standard library), and it
passes ``run_store_contract`` unchanged.  That is the only evidence that the
protocol is a protocol and not a description of ``LTMStore``.

What it deliberately is not
---------------------------
Not a port of ``agent/memory/store.py``.  That file carries usage accounting,
category merging and renaming, event-log compaction, provider-checkpoint
summaries, schema migrations and a hand-written BM25 re-rank -- none of which the
contract asks for.  What is here is what the contract asks for, which doubles as
a measurement of how much of the built-in surface the rest of the application
actually depends on.

What it does *not* replace, and why that is worth knowing
--------------------------------------------------------
Replacing ``ltm_store`` swaps the storage the contract covers.  It does not swap
``StagingBuffer``, which keeps staged turns in its own SQLite file and defaults
to the *same* ``palace.db`` the built-in store uses -- so a deployment on this
backend still has one built-in file on disk, holding staging rather than memory.
That is a real boundary of the seam, not a bug in it: staging is not part of
``MemoryStore``, and the way to replace it too is to provide ``context_manager``
(a different ``ContextService``), which owns the buffer it reads.  Measured while
writing this: with the profile selected, ``context/palace.db`` still appears, and
``context/palace-fts.db`` is the memory.

Four places where it is honestly thinner than the built-in one, all of them
outside the contract's observable behaviour:

* ``usage_summary`` reports row counts, not provider token totals: this store
  never sees a provider response, so it has nothing to total.  The contract's
  caller renders whatever keys are present.
* ``apply_retention`` is a no-op: retention policy is the built-in store's
  opinion, and a replacement is allowed to keep everything.  The contract only
  promises the call is safe.
* Search is FTS5 with a ``LIKE`` fallback, rather than the built-in's BM25-lite
  re-rank; both return entries, ordered by relevance, which is what callers see.
* Conflicts are detected by "more than one distinct active value for a fact key"
  rather than by the built-in's resolution reasons.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

from agent import shared

from .._helpers import _now, normalize_memory_chapter
from ..models import (
    AgentRuntimeEvent,
    ConversationTurn,
    ConversationWriteResult,
    FactAssertion,
    LTMEntry,
    LTMCategory,
    ResolvedFact,
    SessionWorkingState,
)
from ..retrieval import CorpusStats

__all__ = ["SqliteFtsMemoryPlugin", "SqliteFtsStore"]

CATEGORY_NORMALIZE = re.compile(r"[^a-z0-9_-]+")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw not in (None, "") else default
    except Exception:
        return default


def _fact_key(subject: str, predicate: str, scope: str) -> str:
    return _json_dumps([subject.strip().lower(), predicate.strip().lower(), scope or "global"])


def _fts_terms(query: str) -> str:
    """A safe FTS5 MATCH expression: quoted terms, OR-joined.

    Built by quoting rather than by escaping: FTS5's own syntax is a query
    language, and a user's message is not one.  A query with no usable term
    yields "" and the caller falls back to ``LIKE``.
    """
    terms = re.findall(r"[\w\u4e00-\u9fff]+", str(query or ""))
    if not terms:
        return ""
    return " OR ".join('"' + term.replace('"', "") + '"' for term in terms[:12])


class SqliteFtsStore:
    """A :class:`agent.memory.contracts.MemoryStore` over SQLite with FTS5."""

    def __init__(
        self,
        *,
        context_dir: Path,
        memory_dir: Optional[Path] = None,
        max_categories: int = shared.MAX_CATEGORIES,
    ) -> None:
        self.context_dir = Path(context_dir)
        self.memory_dir = Path(memory_dir or self.context_dir)
        self.max_categories = int(max_categories)
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.context_dir / "palace-fts.db"
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._closed = False
        self._fts = False

    # ── connection and schema ───────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema(conn)
            self._conn = conn
            return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.0,
                category TEXT NOT NULL,
                entity TEXT NOT NULL DEFAULT '',
                memory_type TEXT NOT NULL DEFAULT 'fact',
                scope TEXT NOT NULL DEFAULT 'global',
                status TEXT NOT NULL DEFAULT 'active',
                source_session TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS entries_category ON entries(category, status);
            CREATE TABLE IF NOT EXISTS fact_assertions (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_type TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'global',
                status TEXT NOT NULL DEFAULT 'active',
                source_kind TEXT NOT NULL DEFAULT 'manual_write',
                source_id TEXT NOT NULL DEFAULT '',
                source_session TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1.0,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS facts_key ON fact_assertions(fact_key, status);
            CREATE TABLE IF NOT EXISTS resolved_facts (
                fact_key TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_type TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'global',
                winning_assertion_id TEXT NOT NULL DEFAULT '',
                resolution_reason TEXT NOT NULL DEFAULT 'resolved',
                confidence REAL NOT NULL DEFAULT 1.0,
                resolved_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
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
                created_at TEXT NOT NULL DEFAULT '',
                seq INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS turns_session ON conversation_turns(session_id, seq);
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                turn_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                seq INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS events_session ON agent_events(session_id, seq);
            CREATE TABLE IF NOT EXISTS session_working_state (
                session_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS provider_checkpoints (
                session_id TEXT PRIMARY KEY,
                messages_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS transcript_seq_counters (
                session_id TEXT PRIMARY KEY,
                next_seq INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        # One row per (session, message id): that is the idempotency key the
        # contract's callers rely on, for the user row and -- crucially -- for
        # the *assistant* row, whose id names one execution rather than one
        # message.  A partial index keeps empty ids out of it, so a row without
        # an id is always insertable.
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS turns_message
                ON conversation_turns(session_id, role, message_id)
                WHERE message_id != '';
            """
        )
        try:
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                    content, id UNINDEXED, category UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    content, id UNINDEXED, session_id UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            self._fts = True
        except sqlite3.OperationalError:
            # A SQLite without FTS5 still satisfies the contract: search falls
            # back to LIKE, which is slower and ranks nothing.
            self._fts = False
        conn.commit()

    def _insert_fts(self, table: str, row_id: Any, *columns: str) -> None:
        if not self._fts:
            return
        placeholders = ", ".join("?" for _ in range(len(columns) + 1))
        self._connect().execute(
            f"INSERT INTO {table}(rowid, {', '.join(['content', 'id', 'category'][:len(columns)+1])}) "
            f"VALUES ({placeholders})",
            (row_id, *columns),
        )

    # ── entries ─────────────────────────────────────────────────────────────

    @staticmethod
    def _entry(row: sqlite3.Row) -> LTMEntry:
        return LTMEntry(
            id=row["id"],
            content=row["content"],
            importance=float(row["importance"]),
            category=row["category"],
            entity=row["entity"],
            memory_type=row["memory_type"],
            scope=row["scope"],
            status=row["status"],
            source_session=row["source_session"],
            confidence=float(row["confidence"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add_entry(self, entry: LTMEntry) -> None:
        self.add_entries([entry])

    def add_entries(self, entries: list[LTMEntry]) -> None:
        with self._lock:
            conn = self._connect()
            with conn:
                for entry in entries:
                    conn.execute(
                        """
                        INSERT INTO entries (
                            id, content, importance, category, entity, memory_type,
                            scope, status, source_session, confidence, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            content=excluded.content,
                            importance=excluded.importance,
                            category=excluded.category,
                            entity=excluded.entity,
                            memory_type=excluded.memory_type,
                            scope=excluded.scope,
                            status=excluded.status,
                            confidence=excluded.confidence,
                            updated_at=excluded.updated_at
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
                            entry.created_at or _now(),
                            entry.updated_at or _now(),
                        ),
                    )
                    if self._fts:
                        conn.execute("DELETE FROM entries_fts WHERE id = ?", (entry.id,))
                        conn.execute(
                            "INSERT INTO entries_fts(content, id, category) VALUES (?, ?, ?)",
                            (entry.content, entry.id, entry.category),
                        )

    def read_entries(
        self, category: str, *, scopes: Optional[list[str]] = None
    ) -> list[LTMEntry]:
        clause = "category = ? AND status = 'active'"
        params: list[Any] = [category]
        if scopes:
            clause += f" AND scope IN ({', '.join('?' for _ in scopes)})"
            params.extend(scopes)
        rows = self._connect().execute(
            f"SELECT * FROM entries WHERE {clause} ORDER BY importance DESC, updated_at DESC",
            params,
        ).fetchall()
        return [self._entry(row) for row in rows]

    def read_entries_for_entity(self, category: str, entity: str) -> list[LTMEntry]:
        rows = self._connect().execute(
            "SELECT * FROM entries WHERE category = ? AND entity = ? AND status = 'active'"
            " ORDER BY importance DESC",
            (category, entity),
        ).fetchall()
        return [self._entry(row) for row in rows]

    def all_entries(self) -> list[LTMEntry]:
        rows = self._connect().execute(
            "SELECT * FROM entries ORDER BY importance DESC, updated_at DESC"
        ).fetchall()
        return [self._entry(row) for row in rows]

    def search_entries(
        self,
        query: str,
        categories: Optional[list[str]] = None,
        limit: int = shared.RETRIEVAL_TOP_K,
        scopes: Optional[list[str]] = None,
    ) -> list[LTMEntry]:
        match = _fts_terms(query)
        rows: list[sqlite3.Row] = []
        if match and self._fts:
            rows = self._connect().execute(
                """
                SELECT entries.* FROM entries_fts
                JOIN entries ON entries.id = entries_fts.id
                WHERE entries_fts MATCH ? AND entries.status = 'active'
                ORDER BY bm25(entries_fts) LIMIT ?
                """,
                (match, max(1, int(limit))),
            ).fetchall()
        if not rows:
            like = f"%{str(query or '').strip()}%"
            rows = self._connect().execute(
                "SELECT * FROM entries WHERE status = 'active' AND content LIKE ?"
                " ORDER BY importance DESC LIMIT ?",
                (like, max(1, int(limit))),
            ).fetchall()
        entries = [self._entry(row) for row in rows]
        if categories:
            wanted = set(categories)
            entries = [entry for entry in entries if entry.category in wanted]
        if scopes:
            wanted_scopes = set(scopes)
            entries = [entry for entry in entries if entry.scope in wanted_scopes]
        return entries

    def list_categories(self) -> list[LTMCategory]:
        rows = self._connect().execute(
            """
            SELECT category AS name, COUNT(*) AS entry_count,
                   AVG(importance) AS avg_importance, MAX(updated_at) AS last_updated
            FROM entries WHERE status = 'active' GROUP BY category ORDER BY category
            """
        ).fetchall()
        return [
            LTMCategory(
                name=row["name"],
                entry_count=int(row["entry_count"] or 0),
                avg_importance=float(row["avg_importance"] or 0.0),
                last_updated=str(row["last_updated"] or ""),
            )
            for row in rows
        ]

    def dynamic_category_count(self) -> int:
        return len(
            [
                category
                for category in self.list_categories()
                if category.name not in shared.PALACE_LOCI
            ]
        )

    @staticmethod
    def normalize_category_name(name: str) -> str:
        cleaned = CATEGORY_NORMALIZE.sub("-", str(name or "").strip().lower()).strip("-")
        return cleaned or "general"

    def corpus_stats(
        self, terms: list[str], scopes: Optional[list[str]] = None
    ) -> CorpusStats:
        total = int(
            self._connect()
            .execute("SELECT COUNT(*) AS n FROM entries WHERE status = 'active'")
            .fetchone()["n"]
        )
        frequencies: dict[str, int] = {}
        for term in terms or []:
            like = f"%{term}%"
            clause = "status = 'active' AND content LIKE ?"
            params: list[Any] = [like]
            if scopes:
                clause += f" AND scope IN ({', '.join('?' for _ in scopes)})"
                params.extend(scopes)
            row = self._connect().execute(
                f"SELECT COUNT(*) AS n FROM entries WHERE {clause}", params
            ).fetchone()
            frequencies[str(term)] = int(row["n"] or 0)
        return CorpusStats(total_documents=total, document_frequencies=frequencies)

    def apply_retention(self) -> None:
        """Nothing to do: this store keeps everything.  Safe to call at any time."""

    def clear_persistent_memory(self) -> dict[str, int]:
        with self._lock:
            conn = self._connect()
            tables = (
                "entries",
                "fact_assertions",
                "resolved_facts",
                "conversation_turns",
                "agent_events",
                "session_working_state",
                "provider_checkpoints",
            )
            counts: dict[str, int] = {}
            with conn:
                for table in tables:
                    counts[table] = int(
                        conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                    )
                    conn.execute(f"DELETE FROM {table}")
                if self._fts:
                    conn.execute("DELETE FROM entries_fts")
                    conn.execute("DELETE FROM turns_fts")
            return counts

    # ── manual notes and identity ───────────────────────────────────────────

    def _note_id(self, category: str, entity: str) -> str:
        return f"note-{category}-{entity}"

    def read_manual_note(self, category: str, entity: str) -> Optional[LTMEntry]:
        row = self._connect().execute(
            "SELECT * FROM entries WHERE id = ?", (self._note_id(category, entity),)
        ).fetchone()
        return self._entry(row) if row else None

    def upsert_manual_note(
        self, category: str, entity: str, content: str, append: bool = False
    ) -> LTMEntry:
        note_id = self._note_id(category, entity)
        existing = self.read_manual_note(category, entity)
        text = str(content or "")
        if append and existing is not None and existing.content:
            text = f"{existing.content}\n\n{text}" if text else existing.content
        now = _now()
        entry = LTMEntry(
            id=note_id,
            content=text,
            importance=1.0 if not append else max(1.0, existing.importance if existing else 1.0),
            category=normalize_memory_chapter(category, shared.LEGACY_MEMORY_ALIASES),
            entity=entity,
            memory_type="manual_note",
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.add_entry(entry)
        return entry

    def set_identity(
        self,
        *,
        subject: str = "assistant",
        name: Optional[str] = None,
        role: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> dict[str, str]:
        applied: dict[str, str] = {}
        for predicate, raw in (("name", name), ("role", role), ("identity_note", persona)):
            if raw is None:
                continue
            value = str(raw).strip()
            applied[predicate] = value
            if not value:
                continue
            self.add_fact_assertion(
                FactAssertion(
                    id=str(uuid.uuid4()),
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    source_kind="identity_directive",
                    source_session="set_identity",
                )
            )
        return applied

    # ── facts ───────────────────────────────────────────────────────────────

    def add_fact_assertion(self, assertion: FactAssertion) -> FactAssertion:
        subject = str(assertion.subject or "").strip().lower()
        predicate = str(assertion.predicate or "").strip().lower()
        scope = str(assertion.scope or "global")
        key = _fact_key(subject, predicate, scope)
        now = _now()
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO fact_assertions (
                        id, subject, predicate, fact_key, value_json, value_type, scope,
                        status, source_kind, source_id, source_session, channel, confidence,
                        valid_from, valid_to, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        value_json=excluded.value_json, status=excluded.status,
                        confidence=excluded.confidence, updated_at=excluded.updated_at
                    """,
                    (
                        assertion.id,
                        subject,
                        predicate,
                        key,
                        _json_dumps(assertion.value),
                        assertion.value_type,
                        scope,
                        assertion.status,
                        assertion.source_kind,
                        assertion.source_id,
                        assertion.source_session,
                        assertion.channel,
                        float(assertion.confidence),
                        assertion.valid_from,
                        assertion.valid_to,
                        assertion.created_at or now,
                        now,
                    ),
                )
                winner = conn.execute(
                    "SELECT * FROM fact_assertions WHERE fact_key = ? AND status = 'active'"
                    " ORDER BY confidence DESC, created_at DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if winner is not None:
                    conn.execute(
                        """
                        INSERT INTO resolved_facts (
                            fact_key, subject, predicate, value_json, value_type, scope,
                            winning_assertion_id, resolution_reason, confidence,
                            resolved_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'resolved', ?, ?, ?)
                        ON CONFLICT(fact_key) DO UPDATE SET
                            value_json=excluded.value_json, value_type=excluded.value_type,
                            winning_assertion_id=excluded.winning_assertion_id,
                            confidence=excluded.confidence, resolved_at=excluded.resolved_at,
                            updated_at=excluded.updated_at
                        """,
                        (
                            key,
                            subject,
                            predicate,
                            winner["value_json"],
                            winner["value_type"],
                            scope,
                            winner["id"],
                            float(winner["confidence"]),
                            now,
                            now,
                        ),
                    )
        return assertion

    def read_resolved_facts(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[ResolvedFact]:
        clause = "1 = 1"
        params: list[Any] = []
        if subject:
            clause += " AND subject = ?"
            params.append(str(subject).strip().lower())
        if predicate:
            clause += " AND predicate = ?"
            params.append(str(predicate).strip().lower())
        if scope:
            clause += " AND scope = ?"
            params.append(scope)
        rows = self._connect().execute(
            f"SELECT * FROM resolved_facts WHERE {clause} ORDER BY updated_at DESC", params
        ).fetchall()
        return [
            ResolvedFact(
                fact_key=row["fact_key"],
                subject=row["subject"],
                predicate=row["predicate"],
                value=_json_loads(row["value_json"], None),
                value_type=row["value_type"],
                scope=row["scope"],
                winning_assertion_id=row["winning_assertion_id"],
                resolution_reason=row["resolution_reason"],
                confidence=float(row["confidence"]),
                resolved_at=row["resolved_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def has_conflicted_fact(self, subject: str, predicate: str, scope: str = "global") -> bool:
        key = _fact_key(str(subject).strip().lower(), str(predicate).strip().lower(), scope)
        values = {
            row["value_json"]
            for row in self._connect().execute(
                "SELECT value_json FROM fact_assertions WHERE fact_key = ? AND status = 'active'",
                (key,),
            ).fetchall()
        }
        return len(values) > 1

    # ── conversation history ────────────────────────────────────────────────

    def _next_seq(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute(
            """
            INSERT INTO transcript_seq_counters(session_id, next_seq) VALUES (?, 2)
            ON CONFLICT(session_id) DO UPDATE SET next_seq = next_seq + 1
            RETURNING next_seq - 1 AS seq
            """,
            (session_id,),
        ).fetchone()
        return int(row["seq"])

    def _insert_turn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        role: str,
        content: str,
        channel: str,
        message_id: str,
        reply_to_id: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> bool:
        """Insert one turn; False when its id was already there."""
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO conversation_turns (
                session_id, role, content, channel, message_id, reply_to_id,
                metadata_json, created_at, seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                channel,
                message_id,
                reply_to_id,
                _json_dumps(metadata),
                created_at,
                self._next_seq(conn, session_id),
            ),
        )
        if cursor.rowcount != 1:
            return False
        if self._fts and content:
            conn.execute(
                "INSERT INTO turns_fts(content, id, session_id) VALUES (?, ?, ?)",
                (content, str(cursor.lastrowid), session_id),
            )
        return True

    def write_conversation_exchange(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str = "",
        channel: str = "",
        message_id: str = "",
        assistant_message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
        interjections: Optional[Sequence[dict[str, Any]]] = None,
        created_at: Optional[str] = None,
    ) -> ConversationWriteResult:
        payload = metadata if isinstance(metadata, dict) else {}
        clean_user = str(user_content or "").strip()
        if not clean_user and not payload.get("attachments"):
            return ConversationWriteResult()
        clean_session = str(session_id or "").strip() or "default"
        clean_message_id = str(message_id or "").strip()
        clean_assistant_id = str(assistant_message_id or "").strip()
        timestamp = created_at or _now()

        user_created = False
        assistant_created = False
        first_assistant_for_user = False
        interjections_created: list[str] = []
        with self._lock:
            conn = self._connect()
            with conn:
                user_created = self._insert_turn(
                    conn,
                    session_id=clean_session,
                    role="user",
                    content=clean_user,
                    channel=str(channel or ""),
                    message_id=clean_message_id,
                    reply_to_id=str(reply_to_id or ""),
                    metadata=payload,
                    created_at=timestamp,
                )
                for entry in interjections or ():
                    if not isinstance(entry, dict):
                        continue
                    text = str(entry.get("text") or "").strip()
                    if not text:
                        continue
                    entry_id = str(entry.get("message_id") or "").strip()
                    entry_metadata = {
                        "interjection": True,
                        "urgency": str(entry.get("urgency") or "normal"),
                    }
                    if self._insert_turn(
                        conn,
                        session_id=clean_session,
                        role="user",
                        content=text,
                        channel=str(channel or ""),
                        message_id=entry_id,
                        reply_to_id="",
                        metadata=entry_metadata,
                        created_at=timestamp,
                    ):
                        interjections_created.append(text)
                clean_assistant = str(assistant_content or "").strip()
                if clean_assistant:
                    # The assistant row's identity is the *execution* id when the
                    # caller has one: a second run of the same message is a second
                    # answer, and folding it into the first row is how a delivered
                    # answer disappears from the record.
                    assistant_key = clean_assistant_id or clean_message_id
                    if clean_message_id:
                        first_assistant_for_user = (
                            conn.execute(
                                "SELECT COUNT(*) AS n FROM conversation_turns"
                                " WHERE session_id = ? AND role = 'assistant' AND reply_to_id = ?",
                                (clean_session, clean_message_id),
                            ).fetchone()["n"]
                            == 0
                        )
                    assistant_created = self._insert_turn(
                        conn,
                        session_id=clean_session,
                        role="assistant",
                        content=clean_assistant,
                        channel=str(channel or ""),
                        message_id=assistant_key,
                        reply_to_id=clean_message_id or str(reply_to_id or ""),
                        metadata=payload,
                        created_at=timestamp,
                    )
        return ConversationWriteResult(
            user_created=user_created,
            assistant_created=assistant_created,
            first_assistant_for_user=first_assistant_for_user,
            interjections_created=tuple(interjections_created),
        )

    def _turn(self, row: sqlite3.Row) -> ConversationTurn:
        return ConversationTurn(
            id=int(row["id"]),
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            channel=row["channel"],
            message_id=row["message_id"],
            reply_to_id=row["reply_to_id"],
            metadata=_json_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
            seq=int(row["seq"] or 0),
        )

    def _event(self, row: sqlite3.Row) -> AgentRuntimeEvent:
        return AgentRuntimeEvent(
            id=int(row["id"]),
            session_id=row["session_id"],
            event_type=row["event_type"],
            payload=_json_loads(row["payload_json"], {}),
            turn_id=row["turn_id"],
            created_at=row["created_at"],
            seq=int(row["seq"] or 0),
        )

    def recent_conversation_turns(
        self,
        *,
        session_id: Optional[str] = None,
        channel: Optional[str] = None,
        exclude_message_id: str = "",
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> list[ConversationTurn]:
        clause = "1 = 1"
        params: list[Any] = []
        if session_id:
            clause += " AND session_id = ?"
            params.append(session_id)
        if channel:
            clause += " AND channel = ?"
            params.append(channel)
        if exclude_message_id:
            clause += " AND message_id != ?"
            params.append(exclude_message_id)
        rows = self._connect().execute(
            f"SELECT * FROM conversation_turns WHERE {clause} ORDER BY seq DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [self._turn(row) for row in reversed(rows)]

    def search_conversation_turns(
        self,
        query: str,
        *,
        session_id: str,
        exclude_message_id: str = "",
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> list[ConversationTurn]:
        match = _fts_terms(query)
        rows: list[sqlite3.Row] = []
        if match and self._fts:
            rows = self._connect().execute(
                """
                SELECT conversation_turns.* FROM turns_fts
                JOIN conversation_turns ON conversation_turns.id = turns_fts.id
                WHERE turns_fts MATCH ? AND conversation_turns.session_id = ?
                ORDER BY bm25(turns_fts) LIMIT ?
                """,
                (match, session_id, max(1, int(limit))),
            ).fetchall()
        if not rows:
            rows = self._connect().execute(
                "SELECT * FROM conversation_turns WHERE session_id = ? AND content LIKE ?"
                " ORDER BY seq DESC LIMIT ?",
                (session_id, f"%{str(query or '').strip()}%", max(1, int(limit))),
            ).fetchall()
        turns = [
            self._turn(row)
            for row in rows
            if not exclude_message_id or row["message_id"] != exclude_message_id
        ]
        return sorted(turns, key=lambda turn: turn.seq)

    def message_has_answer(self, *, session_id: str, message_id: str) -> bool:
        row = self._connect().execute(
            """
            SELECT 1 FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant'
              AND (reply_to_id = ? OR message_id = ?)
            LIMIT 1
            """,
            (session_id, message_id, message_id),
        ).fetchone()
        return row is not None

    def transcript_window(
        self, *, session_id: str, limit: int = 200, before_seq: int = 0
    ) -> tuple[list[ConversationTurn], list[AgentRuntimeEvent], bool]:
        """A page of the session's timeline, counted in **turns**.

        Rows are walked newest-first in chunks, and a chunk boundary never splits
        a turn: the window is whatever set of whole turns fits the limit, which is
        what keeps a page from handing a client a hundred tool events and no
        reply.
        """
        conn = self._connect()
        limit = max(1, int(limit))
        cursor = int(before_seq or 0)
        if not cursor:
            row = conn.execute(
                "SELECT MAX(seq) AS top FROM ("
                " SELECT seq FROM conversation_turns WHERE session_id = ?"
                " UNION ALL SELECT seq FROM agent_events WHERE session_id = ?)",
                (session_id, session_id),
            ).fetchone()
            cursor = int(row["top"] or 0) + 1
        chunk = max(200, limit * 2)
        turns: list[ConversationTurn] = []
        events: list[AgentRuntimeEvent] = []
        seen: set[str] = set()
        oldest = cursor
        while True:
            rows = conn.execute(
                """
                SELECT 'turn' AS kind, id, seq, role AS extra, message_id, reply_to_id, '' AS turn_id
                FROM conversation_turns WHERE session_id = ? AND seq < ?
                UNION ALL
                SELECT 'event' AS kind, id, seq, '' AS extra, '' AS message_id,
                       '' AS reply_to_id, turn_id
                FROM agent_events WHERE session_id = ? AND seq < ?
                ORDER BY seq DESC LIMIT ?
                """,
                (session_id, cursor, session_id, cursor, chunk),
            ).fetchall()
            if not rows:
                break
            full = False
            for row in rows:
                key = (
                    f"{row['reply_to_id'] or row['message_id']}"
                    if row["kind"] == "turn"
                    else f"{row['turn_id']}"
                )
                if key not in seen:
                    if len(seen) >= limit:
                        full = True
                        break
                    seen.add(key)
                oldest = int(row["seq"])
                if row["kind"] == "turn":
                    turns.append(
                        self._turn(
                            conn.execute(
                                "SELECT * FROM conversation_turns WHERE id = ?", (row["id"],)
                            ).fetchone()
                        )
                    )
                else:
                    events.append(
                        self._event(
                            conn.execute(
                                "SELECT * FROM agent_events WHERE id = ?", (row["id"],)
                            ).fetchone()
                        )
                    )
            if full or len(rows) < chunk:
                break
            cursor = oldest
        remaining = conn.execute(
            """
            SELECT 1 FROM (
                SELECT seq FROM conversation_turns WHERE session_id = ? AND seq < ?
                UNION ALL SELECT seq FROM agent_events WHERE session_id = ? AND seq < ?
            ) LIMIT 1
            """,
            (session_id, oldest, session_id, oldest),
        ).fetchone()
        turns.sort(key=lambda turn: turn.seq)
        events.sort(key=lambda event: event.seq)
        return turns, events, remaining is not None

    # ── working state, checkpoints, usage ───────────────────────────────────

    def save_session_working_state(
        self, session_id: str, state: dict[str, Any], *, updated_at: Optional[str] = None
    ) -> SessionWorkingState:
        stamp = updated_at or _now()
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO session_working_state(session_id, state_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        state_json=excluded.state_json, updated_at=excluded.updated_at
                    """,
                    (session_id, _json_dumps(state or {}), stamp),
                )
        return SessionWorkingState(session_id=session_id, state=dict(state or {}), updated_at=stamp)

    def load_session_working_state(self, session_id: str) -> Optional[SessionWorkingState]:
        row = self._connect().execute(
            "SELECT * FROM session_working_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionWorkingState(
            session_id=row["session_id"],
            state=_json_loads(row["state_json"], {}),
            updated_at=row["updated_at"],
        )

    def save_provider_checkpoint(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        summary: str = "",
        updated_at: Optional[str] = None,
    ) -> None:
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO provider_checkpoints(session_id, messages_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        messages_json=excluded.messages_json, updated_at=excluded.updated_at
                    """,
                    (session_id, _json_dumps(list(messages or [])), updated_at or _now()),
                )

    def load_provider_checkpoint(self, session_id: str) -> Optional[dict[str, Any]]:
        row = self._connect().execute(
            "SELECT * FROM provider_checkpoints WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        messages = _json_loads(row["messages_json"], [])
        return {"session_id": session_id, "messages": messages, "updated_at": row["updated_at"]}

    def usage_summary(self, session_id: str) -> dict[str, Any]:
        turns = int(
            self._connect()
            .execute(
                "SELECT COUNT(*) AS n FROM conversation_turns WHERE session_id = ?", (session_id,)
            )
            .fetchone()["n"]
        )
        events = int(
            self._connect()
            .execute(
                "SELECT COUNT(*) AS n FROM agent_events WHERE session_id = ?", (session_id,)
            )
            .fetchone()["n"]
        )
        return {"session_id": session_id, "turns": turns, "agent_events": events,
                "backend": "sqlite-fts"}

    # ── runtime events ──────────────────────────────────────────────────────

    def append_agent_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        turn_id: str = "",
        created_at: Optional[str] = None,
    ) -> AgentRuntimeEvent:
        stamp = created_at or _now()
        with self._lock:
            conn = self._connect()
            with conn:
                seq = self._next_seq(conn, session_id)
                cursor = conn.execute(
                    """
                    INSERT INTO agent_events (
                        session_id, event_type, payload_json, turn_id, created_at, seq
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        event_type,
                        _json_dumps(payload or {}),
                        turn_id,
                        stamp,
                        seq,
                    ),
                )
                event_id = int(cursor.lastrowid)
        return AgentRuntimeEvent(
            id=event_id,
            session_id=session_id,
            event_type=event_type,
            payload=dict(payload or {}),
            turn_id=turn_id,
            created_at=stamp,
            seq=seq,
        )

    def recent_agent_events(
        self, *, session_id: str, limit: int = 20
    ) -> list[AgentRuntimeEvent]:
        rows = self._connect().execute(
            "SELECT * FROM agent_events WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, max(1, int(limit))),
        ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    # ── maintenance ─────────────────────────────────────────────────────────

    def maintenance_snapshot(self, limit: int = 20) -> str:
        conn = self._connect()

        def count(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

        lines = [
            f"sqlite-fts backend: {self._path}",
            f"entries={count('entries')} facts={count('resolved_facts')}",
            f"turns={count('conversation_turns')} events={count('agent_events')}",
            "top entries by importance:",
        ]
        for entry in self.all_entries()[: max(1, int(limit))]:
            lines.append(f"  [{entry.importance:.2f}] {entry.category}: {entry.content[:60]}")
        return "\n".join(lines)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
            self._closed = True


class SqliteFtsMemoryPlugin:
    """Provides ``ltm_store`` and ``memory`` from :class:`SqliteFtsStore`.

    Same two services as the built-in memory plugin, which is what makes swapping
    one swap the storage without swapping the retrieval, consolidation and
    compaction the rest of the memory system provides.
    """

    name = "sqlite-fts-memory"
    inject = ("cfg",)

    def setup(self, ctx: Any) -> None:
        from agent import shared

        from ..palace import MemoryPalace
        from ..plugin import MemoryPlugin

        cfg = ctx.get("cfg", None) or {}
        ctx_cfg = cfg.get("context", {}) or {}
        storage_cfg = ctx_cfg.get("storage", ctx_cfg)
        mem_cfg = cfg.get("memory", {}) or {}

        store = SqliteFtsStore(
            context_dir=shared.CONTEXT_DIR,
            memory_dir=shared.MEMORY_DIR,
            max_categories=storage_cfg.get("max_categories", shared.MAX_CATEGORIES),
        )
        # Opened here, closed here -- the same rule the built-in plugin follows.
        ctx.on_unwind(store.close)
        MemoryPlugin._seed_identity(store, cfg)

        palace = MemoryPalace(
            tidy_interval=mem_cfg.get("tidy_interval_seconds", shared.MEMORY_TIDY_INTERVAL),
            tidy_threshold=mem_cfg.get("tidy_file_threshold", shared.MEMORY_TIDY_FILE_THRESHOLD),
            base_dir=shared.MEMORY_DIR,
            context_dir=shared.CONTEXT_DIR,
            store=store,
        )

        ctx.provide("ltm_store", store)
        ctx.provide("memory", palace)
