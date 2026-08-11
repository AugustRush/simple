"""SQLite-backed long-term memory store."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Optional

from agent import shared
from agent.lexical import LATIN_TOKEN_RE

from ._helpers import (
    SQLITE_BUSY_TIMEOUT_MS,
    _ASSISTANT_IDENTITY_ENTITIES,
    _FACT_SOURCE_PRECEDENCE,
    _IDENTITY_SUBJECTS,
    _RECENCY_GOVERNED_FACTS,
    _RETIRED_INFERENCE_SOURCE_KINDS,
    _RUN_SCRATCH_MAX_ACTIVE,
    _RUN_SCRATCH_RETENTION_DAYS,
    _dump_fact_value,
    _fact_key,
    _fact_value_type,
    _identity_note_value,
    _lexical_terms,
    _load_fact_value,
    _new_id,
    _normalize_fact_part,
    _now,
)
from .models import (
    AgentRuntimeEvent,
    ConversationTurn,
    ConversationWriteResult,
    FactAssertion,
    LTMCategory,
    LTMEntry,
    ResolvedFact,
    SessionWorkingState,
)

class LTMStore:
    """SQLite-backed long-term memory with JSON and markdown projections."""

    def __init__(
        self,
        context_dir: Optional[Path] = None,
        max_categories: int = shared.MAX_CATEGORIES,
        memory_dir: Optional[Path] = None,
    ):
        # Resolve at call time for the same reason as MemoryPalace/StagingBuffer.
        context_dir = context_dir or shared.CONTEXT_DIR
        memory_dir = memory_dir or shared.MEMORY_DIR
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
        self._repair_assistant_identity_facts()
        # Populated on first read of ``_meta``; None means "stale".
        self._category_stats_cache: Optional[dict] = None

    # ── Persistence ───────────────────────────────────────────────────────────

    def _repair_assistant_identity_facts(self) -> None:
        """Reconcile stored identity facts with the writers that exist today.

        Two kinds of damage accumulate in an existing store and neither heals
        on its own, because a fact is only ever written — never revisited.

        The pattern-matching extractor that used to read names out of chat is
        gone.  Everything it asserted was a guess made by code that no longer
        exists, and some of those guesses ("你叫什么" → name "什么") were
        recorded at the highest authority the store has, so nothing later could
        dislodge them.  Retract the lot by provenance rather than by inspecting
        values: which rows are unsound is a question about where they came
        from, and answering it by re-judging the text would just reinstate the
        guessing this removed.

        An identity note written by hand produced no fact at all, so the last
        machine-extracted note stayed current long after the user replaced it.
        Derive the missing facts now, keyed on the entry they came from so a
        second run adds nothing.

        Best-effort by construction: a store that cannot be repaired is still a
        store that works, and refusing to start over stale identity would be a
        far worse failure than carrying it.
        """
        try:
            with self._connect() as conn:
                retracted = self._retract_inferred_identity_facts(conn)
                derived = self._derive_missing_identity_facts(conn)
            for subject, predicate in retracted | derived:
                self.resolve_fact(subject, predicate)
        except sqlite3.Error:
            return

    def _retract_inferred_identity_facts(
        self, conn: sqlite3.Connection
    ) -> set[tuple[str, str]]:
        subject_slots = ",".join("?" * len(_IDENTITY_SUBJECTS))
        source_slots = ",".join("?" * len(_RETIRED_INFERENCE_SOURCE_KINDS))
        params = tuple(sorted(_IDENTITY_SUBJECTS)) + _RETIRED_INFERENCE_SOURCE_KINDS
        rows = conn.execute(
            f"""
            SELECT DISTINCT subject, predicate FROM fact_assertions
            WHERE subject IN ({subject_slots})
              AND source_kind IN ({source_slots})
              AND status != 'archived'
            """,
            params,
        ).fetchall()
        if not rows:
            return set()
        conn.execute(
            f"""
            UPDATE fact_assertions SET status = 'archived', updated_at = ?
            WHERE subject IN ({subject_slots})
              AND source_kind IN ({source_slots})
              AND status != 'archived'
            """,
            (_now(),) + params,
        )
        return {(str(row["subject"]), str(row["predicate"])) for row in rows}

    def _derive_missing_identity_facts(
        self, conn: sqlite3.Connection
    ) -> set[tuple[str, str]]:
        entity_slots = ",".join("?" * len(_ASSISTANT_IDENTITY_ENTITIES))
        rows = conn.execute(
            f"""
            SELECT * FROM memory_items
            WHERE category = 'identity' AND entity IN ({entity_slots})
              AND status NOT IN ('archived', 'superseded')
            """,
            tuple(sorted(_ASSISTANT_IDENTITY_ENTITIES)),
        ).fetchall()
        if not rows:
            return set()
        known = {
            (str(row["source_id"]), str(row["predicate"]))
            for row in conn.execute(
                "SELECT source_id, predicate FROM fact_assertions WHERE subject = 'assistant'"
            ).fetchall()
        }
        touched: set[tuple[str, str]] = set()
        for row in rows:
            entry = self._row_to_entry(row)
            for fact in self._fact_assertions_from_entry(entry):
                if (str(fact.source_id), fact.predicate) in known:
                    continue
                self._insert_fact_assertion(conn, fact)
                touched.add((fact.subject, fact.predicate))
        return touched

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
            # See StagingBuffer._connect: WAL allows a single writer, so
            # concurrent writers must wait rather than fail outright.
            conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
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

    def clear_persistent_memory(self) -> dict[str, int]:
        """Atomically delete all model-retrievable memory state."""
        tables = (
            "memory_items_fts",
            "memory_items",
            "fact_assertions",
            "resolved_facts",
            "conversation_turns",
            "session_working_state",
            "agent_events",
            "staging_turns",
        )
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            existing = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in tables:
                if table not in existing:
                    deleted[table] = 0
                    continue
                row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                deleted[table] = int(row["count"] if row else 0)
                conn.execute(f"DELETE FROM {table}")
        self._category_stats_cache = None
        return deleted

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
        """Mark derived category stats stale; they are recomputed on demand."""
        self._category_stats_cache = None

    @property
    def _meta(self) -> dict:
        """Category stats, computed lazily and cached until the next write.

        This used to be recomputed eagerly inside every mutation — a full
        ``GROUP BY`` scan of ``memory_items`` per stored fact, per delete, per
        consolidation round — even though nothing reads it between writes.
        Callers that want fresh numbers go through ``list_categories()``,
        which queries directly.
        """
        if self._category_stats_cache is None:
            self._category_stats_cache = self._category_stats()
        return self._category_stats_cache

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
        # Only invalidate: the stats are derived, and recomputing them here
        # made every write pay for a table scan no caller had asked for.
        self._category_stats_cache = None

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

    def read_entries(
        self,
        category: str,
        *,
        scopes: Optional[list[str]] = None,
    ) -> list[LTMEntry]:
        if scopes is not None and not scopes:
            return []
        category = self.normalize_category_name(category)
        with self._connect() as conn:
            sql = """
                SELECT * FROM memory_items
                WHERE category = ? AND status NOT IN ('archived', 'superseded')
            """
            params: list[Any] = [category]
            if scopes:
                sql += f" AND scope IN ({','.join('?' for _ in scopes)})"
                params.extend(str(scope) for scope in scopes)
            sql += " ORDER BY importance DESC, updated_at DESC, id ASC"
            rows = conn.execute(sql, params).fetchall()
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
        """Derive the identity *prose* an identity entry states, and only that.

        An entry is free text — a paragraph the user or consolidation wrote.
        Reading a name out of it means deciding what a sentence means, which is
        the model's job, not this module's; the extractor that used to try
        renamed the agent "什么" the first time somebody asked it its name.  So
        an entry contributes ``identity_note`` verbatim and nothing else.
        ``name`` and ``role`` come only from deliberate settings: config
        bootstrap and :meth:`set_identity`.
        """
        facts: list[FactAssertion] = []
        category = self.normalize_category_name(entry.category)
        entity = self._normalize_entity(entry.entity, category)
        # The assistant's identity is filed under several entities depending on
        # who wrote it: consolidation uses "assistant", a deliberate memory
        # write uses whatever name the caller passed.  Missing the latter meant
        # every hand-written identity update produced no fact at all, so the
        # first machine-extracted one stayed authoritative forever.
        if category != "identity" or entity not in _ASSISTANT_IDENTITY_ENTITIES:
            return facts
        note = _identity_note_value(entry.content)
        if not note:
            return facts
        source_kind = (
            "manual_write"
            if str(entry.source_session or "").strip() == "manual_memory_write"
            else "consolidation_extract"
        )
        facts.append(
            FactAssertion(
                id=_new_id(),
                subject="assistant",
                predicate="identity_note",
                value=note,
                source_kind=source_kind,
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
        created_at: Optional[str] = None,
    ) -> ConversationWriteResult:
        """Idempotently journal a user event and optional assistant completion.

        A non-empty message ID is an idempotency key for the user event.  The
        user row is committed before an LLM request can begin; a later call can
        attach the assistant row without duplicating the user event.
        """
        clean_user_content = str(user_content or "").strip()
        if not clean_user_content:
            return ConversationWriteResult()
        clean_assistant_content = str(assistant_content or "").strip()
        clean_session_id = str(session_id or "").strip() or "default"
        clean_message_id = str(message_id or "").strip()
        clean_assistant_message_id = str(assistant_message_id or "").strip()
        payload = metadata or {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        metadata_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        timestamp = created_at or _now()

        conn = self._connect()
        user_created = False
        assistant_created = False
        first_assistant_for_user = False
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_user = None
            if clean_message_id:
                existing_user = conn.execute(
                    """
                    SELECT id FROM conversation_turns
                    WHERE session_id = ? AND role = 'user' AND message_id = ?
                    LIMIT 1
                    """,
                    (clean_session_id, clean_message_id),
                ).fetchone()
            if existing_user is None:
                conn.execute(
                    """
                    INSERT INTO conversation_turns (
                        session_id, role, content, channel, message_id, reply_to_id,
                        metadata_json, created_at
                    ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_session_id,
                        clean_user_content,
                        str(channel or ""),
                        clean_message_id,
                        str(reply_to_id or ""),
                        metadata_json,
                        timestamp,
                    ),
                )
                user_created = True
            if clean_assistant_content:
                existing_assistant = None
                if clean_assistant_message_id:
                    existing_assistant = conn.execute(
                        """
                        SELECT id FROM conversation_turns
                        WHERE session_id = ? AND role = 'assistant'
                          AND message_id = ?
                        LIMIT 1
                        """,
                        (clean_session_id, clean_assistant_message_id),
                    ).fetchone()
                elif clean_message_id:
                    existing_assistant = conn.execute(
                        """
                        SELECT id FROM conversation_turns
                        WHERE session_id = ? AND role = 'assistant'
                          AND reply_to_id = ?
                        LIMIT 1
                        """,
                        (clean_session_id, clean_message_id),
                    ).fetchone()
                if existing_assistant is None:
                    prior_assistant = None
                    if clean_message_id:
                        prior_assistant = conn.execute(
                            """
                            SELECT id FROM conversation_turns
                            WHERE session_id = ? AND role = 'assistant'
                              AND reply_to_id = ?
                            LIMIT 1
                            """,
                            (clean_session_id, clean_message_id),
                        ).fetchone()
                    conn.execute(
                        """
                        INSERT INTO conversation_turns (
                            session_id, role, content, channel, message_id, reply_to_id,
                            metadata_json, created_at
                        ) VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_session_id,
                            clean_assistant_content,
                            str(channel or ""),
                            clean_assistant_message_id,
                            clean_message_id,
                            metadata_json,
                            timestamp,
                        ),
                    )
                    assistant_created = True
                    first_assistant_for_user = prior_assistant is None
        return ConversationWriteResult(
            user_created=user_created,
            assistant_created=assistant_created,
            first_assistant_for_user=first_assistant_for_user,
        )

    def append_conversation_exchange(
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
        created_at: Optional[str] = None,
    ) -> bool:
        """Compatibility API: true only when this call created the user row."""
        return self.write_conversation_exchange(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
            created_at=created_at,
        ).user_created

    def recent_conversation_turns(
        self,
        *,
        session_id: Optional[str] = None,
        channel: Optional[str] = None,
        exclude_message_id: str = "",
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> list[ConversationTurn]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            sql = "SELECT * FROM conversation_turns"
            params: list[Any] = []
            clauses: list[str] = []
            if session_id:
                clauses.append("session_id = ?")
                params.append(session_id)
            if channel:
                clauses.append("channel = ?")
                params.append(channel)
            clean_exclude_id = str(exclude_message_id or "").strip()
            if clean_exclude_id:
                clauses.append(
                    "NOT ((role = 'user' AND message_id = ?) "
                    "OR (role = 'assistant' AND reply_to_id = ?))"
                )
                params.extend((clean_exclude_id, clean_exclude_id))
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_conversation_turn(row) for row in reversed(rows)]

    def search_conversation_turns(
        self,
        query: str,
        *,
        session_id: str,
        exclude_message_id: str = "",
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> list[ConversationTurn]:
        terms = [term for term in _lexical_terms(query) if len(term) >= 2][:12]
        if not terms:
            return []
        clauses = " OR ".join("content LIKE ?" for _ in terms)
        clean_exclude_id = str(exclude_message_id or "").strip()
        exclusion_sql = ""
        exclusion_params: list[Any] = []
        if clean_exclude_id:
            exclusion_sql = (
                " AND NOT ((role = 'user' AND message_id = ?) "
                "OR (role = 'assistant' AND reply_to_id = ?))"
            )
            exclusion_params = [clean_exclude_id, clean_exclude_id]
        params: list[Any] = [
            session_id,
            *(f"%{term}%" for term in terms),
            *exclusion_params,
            limit,
        ]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND ({clauses}){exclusion_sql}
                ORDER BY id DESC LIMIT ?
                """,
                params,
            ).fetchall()
            rows_by_id = {int(row["id"]): row for row in rows}
            for row in list(rows):
                neighbor_id = int(row["id"]) + (1 if row["role"] == "user" else -1)
                neighbor = conn.execute(
                    """
                    SELECT * FROM conversation_turns
                    WHERE id = ? AND session_id = ?
                      AND (? = '' OR NOT (
                          (role = 'user' AND message_id = ?)
                          OR (role = 'assistant' AND reply_to_id = ?)
                      ))
                    LIMIT 1
                    """,
                    (
                        neighbor_id,
                        session_id,
                        clean_exclude_id,
                        clean_exclude_id,
                        clean_exclude_id,
                    ),
                ).fetchone()
                if neighbor is None:
                    continue
                if row["role"] == "user" and neighbor["role"] != "assistant":
                    continue
                if row["role"] == "assistant" and neighbor["role"] != "user":
                    continue
                rows_by_id[int(neighbor["id"])] = neighbor
        ordered = sorted(rows_by_id.values(), key=lambda row: int(row["id"]))
        return [self._row_to_conversation_turn(row) for row in ordered[-limit * 2 :]]

    def has_conversation_message(
        self,
        *,
        session_id: str,
        message_id: str,
        role: str = "user",
    ) -> bool:
        clean_message_id = str(message_id or "").strip()
        if not clean_message_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM conversation_turns
                WHERE session_id = ? AND role = ? AND message_id = ?
                LIMIT 1
                """,
                (str(session_id or "").strip() or "default", role, clean_message_id),
            ).fetchone()
        return row is not None

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
    def _fact_assertion_core_score(
        assertion: FactAssertion,
    ) -> tuple[str, int, float, str]:
        """Rank competing assertions of the same fact; highest wins.

        For most facts a trusted source outranks a fresher one, so precedence
        leads.  Identity is different: it is a setting the user changes, and a
        setting is last-write-wins.  Letting precedence lead there meant an
        early statement pinned the identity permanently — a later correction
        arriving through any weaker channel could never displace it.

        ``created_at`` still trails the tuple for the non-identity case, so two
        assertions written in the same instant tie on every component and the
        caller declares a conflict rather than picking arbitrarily.
        """
        precedence = _FACT_SOURCE_PRECEDENCE.get(str(assertion.source_kind or "").strip().lower(), 9)
        created_at = assertion.created_at or ""
        key = (
            _normalize_fact_part(assertion.subject),
            _normalize_fact_part(assertion.predicate),
        )
        recency = created_at if key in _RECENCY_GOVERNED_FACTS else ""
        return (recency, -precedence, float(assertion.confidence or 0.0), created_at)

    @classmethod
    def _fact_assertion_total_score(
        cls, assertion: FactAssertion
    ) -> tuple[str, int, float, str, str]:
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
            self._insert_fact_assertion(conn, normalized)
            self.resolve_fact(
                normalized.subject,
                normalized.predicate,
                normalized.scope,
                _conn=conn,
            )
        return normalized

    @staticmethod
    def _insert_fact_assertion(
        conn: sqlite3.Connection, assertion: FactAssertion
    ) -> None:
        """Write one assertion row. Does not re-resolve — callers decide when."""
        conn.execute(
            """
            INSERT OR IGNORE INTO fact_assertions (
                id, subject, predicate, value_json, value_type, scope,
                source_kind, source_id, source_session, channel, confidence,
                status, valid_from, valid_to, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assertion.id,
                _normalize_fact_part(assertion.subject),
                _normalize_fact_part(assertion.predicate),
                _dump_fact_value(assertion.value),
                assertion.value_type or _fact_value_type(assertion.value),
                _normalize_fact_part(assertion.scope, "global"),
                _normalize_fact_part(assertion.source_kind, "manual_write"),
                assertion.source_id,
                assertion.source_session,
                assertion.channel,
                assertion.confidence,
                _normalize_fact_part(assertion.status, "active"),
                assertion.valid_from,
                assertion.valid_to,
                assertion.created_at or _now(),
                assertion.updated_at or _now(),
            ),
        )

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
        *,
        _conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[ResolvedFact]:
        normalized_subject = _normalize_fact_part(subject)
        normalized_predicate = _normalize_fact_part(predicate)
        normalized_scope = _normalize_fact_part(scope, "global")
        key = _fact_key(normalized_subject, normalized_predicate, normalized_scope)

        connection_context = (
            contextlib.nullcontext(_conn) if _conn is not None else self._connect()
        )
        with connection_context as conn:
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

    def set_identity(
        self,
        *,
        subject: str = "assistant",
        name: Optional[str] = None,
        role: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> dict[str, str]:
        """Record identity as a deliberate setting, last write wins.

        This is the only way ``name`` and ``role`` enter the store apart from
        config bootstrap.  Identity is something the user *sets*, not something
        the system infers from prose, so the caller states the value outright
        and it supersedes whatever was there — no pattern matching, no scoring
        an old value against a new one.

        Fields left as ``None`` are untouched; passing an empty string clears
        that field by superseding it with nothing.
        """
        normalized_subject = _normalize_fact_part(subject, "assistant")
        if normalized_subject not in _IDENTITY_SUBJECTS:
            raise ValueError(
                f"identity subject must be one of {sorted(_IDENTITY_SUBJECTS)}"
            )
        updates = {"name": name, "role": role, "identity_note": persona}
        applied: dict[str, str] = {}
        for predicate, raw in updates.items():
            if raw is None:
                continue
            value = str(raw).strip()
            if predicate == "identity_note":
                value = _identity_note_value(value)
            if not value:
                self.retract_facts(normalized_subject, predicate)
                applied[predicate] = ""
                continue
            self.add_fact_assertion(
                FactAssertion(
                    id=_new_id(),
                    subject=normalized_subject,
                    predicate=predicate,
                    value=value,
                    source_kind="identity_directive",
                    source_session="set_identity",
                    confidence=1.0,
                )
            )
            applied[predicate] = value
        return applied

    def retract_facts(self, subject: str, predicate: str, scope: str = "global") -> int:
        """Archive every assertion behind one fact and drop the resolved row."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE fact_assertions SET status = 'archived', updated_at = ?
                WHERE subject = ? AND predicate = ? AND scope = ?
                  AND status != 'archived'
                """,
                (
                    _now(),
                    _normalize_fact_part(subject),
                    _normalize_fact_part(predicate),
                    _normalize_fact_part(scope, "global"),
                ),
            )
            archived = cursor.rowcount or 0
            conn.execute(
                "DELETE FROM resolved_facts WHERE fact_key = ?",
                (_fact_key(subject, predicate, scope),),
            )
        return archived

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
        # A hand-written identity note is the most deliberate statement of
        # identity there is.  Skipping fact derivation here left it invisible
        # to the startup prompt, which reads facts — the note only surfaced
        # later, if a query happened to route to free-form memory.
        for fact in self._fact_assertions_from_entry(entry):
            self.add_fact_assertion(fact)
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
        scopes: Optional[list[str]] = None,
    ) -> list[LTMEntry]:
        if scopes is not None and not scopes:
            return []
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
                if scopes:
                    sql += f" AND scope IN ({','.join('?' for _ in scopes)})"
                    params.extend(str(scope) for scope in scopes)
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
                if scopes:
                    sql += f" AND m.scope IN ({','.join('?' for _ in scopes)})"
                    params.extend(str(scope) for scope in scopes)
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
                if scopes:
                    sql += f" AND scope IN ({','.join('?' for _ in scopes)})"
                    params.extend(str(scope) for scope in scopes)
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
        run_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_RUN_SCRATCH_RETENTION_DAYS)
        ).strftime("%Y-%m-%d %H:%M UTC")
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
            archived_ids = {
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
            }
            archived_ids.update(
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE category = 'episodes'
                      AND scope LIKE 'run:%'
                      AND created_at < ?
                      AND status NOT IN ('archived', 'superseded')
                    """,
                    (run_cutoff,),
                ).fetchall()
            )
            archived_ids.update(
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE category = 'episodes'
                      AND scope LIKE 'run:%'
                      AND status NOT IN ('archived', 'superseded')
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (_RUN_SCRATCH_MAX_ACTIVE,),
                ).fetchall()
            )
            if archived_ids:
                archived_id_list = sorted(archived_ids)
                conn.execute(
                    f"""
                    UPDATE memory_items
                    SET status     = 'archived',
                        updated_at = ?
                    WHERE id IN ({",".join("?" for _ in archived_id_list)})
                    """,
                    (now, *archived_id_list),
                )
                self._delete_fts_rows(conn, archived_id_list)
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
