"""SQLite-backed long-term memory store."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Optional, Sequence

from agent import shared
from agent.lexical import LATIN_TOKEN_RE

from . import journal
from . import fts
from .retrieval import CorpusStats

from . import facts
from . import queries
from . import schema
from . import usage
from . import migrations
from . import rows as _row_mappers
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
    _USAGE_EVENT_RETENTION_DAYS,
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

#: Documents scanned to estimate CJK n-gram document frequency.  Sampling
#: makes the cost independent of store size; 2000 rows bound the relative
#: error on a df/N ratio well below what a logarithm can distinguish.


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
                retracted = facts._retract_inferred_identity_facts(conn)
                derived = self._derive_missing_identity_facts(conn)
            for subject, predicate in retracted | derived:
                self.resolve_fact(subject, predicate)
        except sqlite3.Error:
            return

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
            entry = _row_mappers._row_to_entry(row)
            for fact in self._fact_assertions_from_entry(entry):
                if (str(fact.source_id), fact.predicate) in known:
                    continue
                facts._insert_fact_assertion(conn, fact)
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
            schema._ensure_schema(
                conn,
            )

    def _ensure_fts_index(self) -> None:
        with self._connect() as conn:
            schema._ensure_fts_index(
                conn,
            )

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
        fts._delete_fts_rows(
            conn,
            entry_ids=entry_ids,
        )

    def _sync_fts_row(self, conn: sqlite3.Connection, entry_id: str) -> None:
        fts._sync_fts_row(
            conn,
            entry_id=entry_id,
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
        return fts._dynamic_category_names(
            conn,
        )


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
        return [_row_mappers._row_to_entry(row) for row in rows]

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
        return [_row_mappers._row_to_entry(row) for row in rows]

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
        return _row_mappers._row_to_entry(row) if row else None

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
        with self._connect() as conn:
            return queries.append_conversation_turn(
                conn,
                session_id=session_id,
                role=role,
                content=content,
                channel=channel,
                message_id=message_id,
                reply_to_id=reply_to_id,
                metadata=metadata,
                created_at=created_at,
            )


    @staticmethod
    def _json_object(raw: Any) -> dict[str, Any]:
        """The object a stored ``metadata_json`` holds, or an empty one."""
        try:
            value = json.loads(raw or "{}")
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

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
        """Idempotently journal a user event and optional assistant completion.

        A non-empty message ID is an idempotency key for the user event.  The
        user row is committed before an LLM request can begin; a later call can
        attach the assistant row without duplicating the user event.

        ``interjections`` are the messages the user sent *during* the turn and
        the model read mid-flight.  They are written here, between the request
        that opened the turn and the answer it produced, because that is where
        they happened: the model answered them within this same turn, so a
        record without them claims the answer came from nowhere -- and the
        reader loses the words that asked for it.  Each row carries an
        ``interjection`` marker, which is what lets a client render it as
        in-turn speech rather than as the start of another turn.
        """
        payload = metadata or {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        clean_user_content = str(user_content or "").strip()
        has_attachments = bool(payload.get("attachments"))
        if not clean_user_content and not has_attachments:
            return ConversationWriteResult()
        clean_assistant_content = str(assistant_content or "").strip()
        clean_session_id = str(session_id or "").strip() or "default"
        clean_message_id = str(message_id or "").strip()
        clean_assistant_message_id = str(assistant_message_id or "").strip()
        metadata_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        timestamp = created_at or _now()

        conn = self._connect()
        user_created = False
        assistant_created = False
        first_assistant_for_user = False
        interjections_created: list[str] = []
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
                        metadata_json, created_at, seq
                    ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_session_id,
                        clean_user_content,
                        str(channel or ""),
                        clean_message_id,
                        str(reply_to_id or ""),
                        metadata_json,
                        timestamp,
                        migrations._next_transcript_seq(conn, clean_session_id),
                    ),
                )
                user_created = True
            # After the request that opened the turn and before its answer:
            # row ids are the display order, and this is where the message
            # happened.  Also after, not before, so the turn's own request
            # stays the first row a reader sees for it.
            for entry in interjections or ():
                if not isinstance(entry, dict):
                    continue
                text = str(entry.get("text") or "").strip()
                if not text:
                    continue
                entry_id = str(entry.get("message_id") or "").strip()
                if entry_id:
                    already = conn.execute(
                        """
                        SELECT id FROM conversation_turns
                        WHERE session_id = ? AND role = 'user' AND message_id = ?
                        LIMIT 1
                        """,
                        (clean_session_id, entry_id),
                    ).fetchone()
                    if already is not None:
                        continue
                entry_metadata: dict[str, Any] = {
                    "interjection": True,
                    "urgency": str(entry.get("urgency") or "normal"),
                }
                if entry.get("arrived_at"):
                    entry_metadata["arrived_at"] = entry["arrived_at"]
                conn.execute(
                    """
                    INSERT INTO conversation_turns (
                        session_id, role, content, channel, message_id, reply_to_id,
                        metadata_json, created_at, seq
                    ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_session_id,
                        text,
                        str(channel or ""),
                        entry_id,
                        clean_message_id,
                        json.dumps(entry_metadata, ensure_ascii=False, sort_keys=True),
                        timestamp,
                        migrations._next_transcript_seq(conn, clean_session_id),
                    ),
                )
                interjections_created.append(text)
            if clean_assistant_content:
                existing_assistant = None
                if clean_assistant_message_id:
                    existing_assistant = conn.execute(
                        """
                        SELECT id, metadata_json FROM conversation_turns
                        WHERE session_id = ? AND role = 'assistant'
                          AND message_id = ?
                        LIMIT 1
                        """,
                        (clean_session_id, clean_assistant_message_id),
                    ).fetchone()
                elif clean_message_id:
                    existing_assistant = conn.execute(
                        """
                        SELECT id, metadata_json FROM conversation_turns
                        WHERE session_id = ? AND role = 'assistant'
                          AND reply_to_id = ?
                        LIMIT 1
                        """,
                        (clean_session_id, clean_message_id),
                    ).fetchone()
                if existing_assistant is not None:
                    # Idempotency is per *execution*: a write this run already
                    # made collapses into its own row, while another run's answer
                    # is a row of its own.  Keying on the completion id alone made
                    # a second execution's answer look like a duplicate of the
                    # first and dropped it -- measurably, two delivered answers
                    # were missing from the record while the rows held *older*
                    # text.  Rows written before runs had ids keep the old
                    # behaviour, so this only ever adds an answer.
                    incoming_run = str(payload.get("run_id") or "")
                    existing_run = str(
                        self._json_object(existing_assistant["metadata_json"]).get("run_id") or ""
                    )
                    if incoming_run and existing_run and incoming_run != existing_run:
                        existing_assistant = None
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
                            metadata_json, created_at, seq
                        ) VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_session_id,
                            clean_assistant_content,
                            str(channel or ""),
                            clean_assistant_message_id,
                            clean_message_id,
                            metadata_json,
                            timestamp,
                            migrations._next_transcript_seq(conn, clean_session_id),
                        ),
                    )
                    assistant_created = True
                    first_assistant_for_user = prior_assistant is None
        return ConversationWriteResult(
            user_created=user_created,
            assistant_created=assistant_created,
            first_assistant_for_user=first_assistant_for_user,
            interjections_created=tuple(interjections_created),
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
        with self._connect() as conn:
            return queries.recent_conversation_turns(
                conn,
                session_id=session_id,
                channel=channel,
                exclude_message_id=exclude_message_id,
                limit=limit,
            )

    def message_has_answer(self, *, session_id: str, message_id: str) -> bool:
        """Whether this message already has an assistant answer on record."""
        with self._connect() as conn:
            return journal.message_has_answer(
                conn,
                session_id=session_id,
                message_id=message_id,
            )


    def transcript_window(
        self,
        *,
        session_id: str,
        limit: int = 200,
        before_seq: int = 0,
    ) -> tuple[list[ConversationTurn], list[AgentRuntimeEvent], bool]:
        """One window of the transcript: both tables, one order, whether more exists."""
        with self._connect() as conn:
            return queries.transcript_window(
                conn,
                session_id=session_id,
                limit=limit,
                before_seq=before_seq,
            )

    def _rows_at_seqs(
        self, session_id: str, table: str, seqs: list[int]
    ) -> list[sqlite3.Row]:
        """Rows of one session's ``table`` at these positions, in order."""
        with self._connect() as conn:
            return queries._rows_at_seqs(
                conn,
                session_id=session_id,
                table=table,
                seqs=seqs,
            )

    def conversation_turn_count(self, *, session_id: Optional[str] = None) -> int:
        """How many journaled turns exist, so a capped read can say it was capped."""
        with self._connect() as conn:
            return journal.conversation_turn_count(
                conn,
                session_id=session_id,
            )


    def list_session_ids(
        self,
        prefix: str = "",
        limit: int = 50,
    ) -> list[tuple[str, str, int]]:
        """Return distinct journaled sessions, most recently active first."""
        with self._connect() as conn:
            return queries.list_session_ids(
                conn,
                prefix=prefix,
                limit=limit,
            )

    def delete_conversation_session(self, session_id: str) -> None:
        """Delete a conversation session's journaled turns and working state."""
        with self._connect() as conn:
            journal.delete_conversation_session(
                conn,
                session_id=session_id,
            )


    def set_session_title(self, session_id: str, title: str) -> None:
        with self._connect() as conn:
            journal.set_session_title(
                conn,
                session_id=session_id,
                title=title,
            )


    def list_session_titles(self) -> dict[str, str]:
        with self._connect() as conn:
            return journal.list_session_titles(
                conn,
            )


    def search_conversation_turns(
        self,
        query: str,
        *,
        session_id: str,
        exclude_message_id: str = "",
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> list[ConversationTurn]:
        with self._connect() as conn:
            return queries.search_conversation_turns(
                conn,
                query=query,
                session_id=session_id,
                exclude_message_id=exclude_message_id,
                limit=limit,
            )

    def load_session_working_state(
        self,
        session_id: str,
    ) -> Optional[SessionWorkingState]:
        with self._connect() as conn:
            return journal.load_session_working_state(
                conn,
                session_id=session_id,
            )


    def save_session_working_state(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        updated_at: Optional[str] = None,
    ) -> SessionWorkingState:
        with self._connect() as conn:
            return journal.save_session_working_state(
                conn,
                session_id=session_id,
                state=state,
                updated_at=updated_at,
            )


    def save_provider_checkpoint(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        summary: str = "",
        updated_at: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            journal.save_provider_checkpoint(
                conn,
                session_id=session_id,
                messages=messages,
                summary=summary,
                updated_at=updated_at,
            )


    def load_provider_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return journal.load_provider_checkpoint(
                conn,
                session_id=session_id,
            )


    def append_usage_event(
        self,
        *,
        session_id: str,
        phase: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        turn_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Persist one provider call so foreground and background cost is visible."""
        with self._connect() as conn:
            usage.append_usage_event(
                conn,
                session_id=session_id,
                phase=phase,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                turn_id=turn_id,
                metadata=metadata,
                created_at=created_at,
            )

    def usage_summary(self, session_id: str) -> dict[str, Any]:
        """Return bounded aggregate usage for one session, grouped by phase."""
        with self._connect() as conn:
            return usage.usage_summary(conn, session_id)

    def append_agent_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        turn_id: str = "",
        created_at: Optional[str] = None,
    ) -> AgentRuntimeEvent:
        with self._connect() as conn:
            return journal.append_agent_event(
                conn,
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                turn_id=turn_id,
                created_at=created_at,
            )


    def recent_agent_events(
        self,
        *,
        session_id: str,
        limit: int = 20,
    ) -> list[AgentRuntimeEvent]:
        with self._connect() as conn:
            return journal.recent_agent_events(
                conn,
                session_id=session_id,
                limit=limit,
            )


    def recent_agent_events_for_turns(
        self,
        *,
        turn_ids: list[str] | tuple[str, ...] | set[str],
        limit: int = 2000,
    ) -> list[AgentRuntimeEvent]:
        """Read events for known turns, including legacy mis-scoped rows."""
        with self._connect() as conn:
            return journal.recent_agent_events_for_turns(
                conn,
                turn_ids=turn_ids,
                limit=limit,
            )



    @classmethod
    def _fact_assertion_total_score(
        cls, assertion: FactAssertion
    ) -> tuple[str, int, float, str, str]:
        return (*facts._fact_assertion_core_score(assertion), assertion.id)

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
            facts._insert_fact_assertion(conn, normalized)
            self.resolve_fact(
                normalized.subject,
                normalized.predicate,
                normalized.scope,
                _conn=conn,
            )
        return normalized

    def _read_facts(
        self,
        table: str,
        order_by: str,
        to_row: Callable[[sqlite3.Row], Any],
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[Any]:
        """Filter one fact table by the three optional keys and read it back.

        Both fact tables carry the same ``subject``/``predicate``/``scope``
        triple and normalize it the same way, so the query lives here once and
        each caller supplies only what actually differs.  ``table`` and
        ``order_by`` are literals from this module, never caller input.
        """
        sql = f"SELECT * FROM {table} WHERE 1 = 1"
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
        sql += f" ORDER BY {order_by}"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [to_row(row) for row in rows]

    def read_fact_assertions(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[FactAssertion]:
        return self._read_facts(
            "fact_assertions",
            "created_at ASC, id ASC",
            _row_mappers._row_to_fact_assertion,
            subject=subject,
            predicate=predicate,
            scope=scope,
        )

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
            assertions = [_row_mappers._row_to_fact_assertion(row) for row in rows]
            if not assertions:
                conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
                return None

            active = [assertion for assertion in assertions if not assertion.valid_to]
            if not active:
                conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
                return None

            best_core_score = max(
                facts._fact_assertion_core_score(assertion) for assertion in active
            )
            top_assertions = [
                assertion
                for assertion in active
                if facts._fact_assertion_core_score(assertion) == best_core_score
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
        return self._read_facts(
            "resolved_facts",
            "subject ASC, predicate ASC, scope ASC",
            _row_mappers._row_to_resolved_fact,
            subject=subject,
            predicate=predicate,
            scope=scope,
        )

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
        clean_content = str(content or "").strip()
        with self._connect() as conn:
            # Read-modify-write under one transaction: a concurrent append
            # must not read the same base note and overwrite the other
            # writer's update (lost update).
            conn.execute("BEGIN IMMEDIATE")
            try:
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
                if row:
                    entry = _row_mappers._row_to_entry(row)
                    entry.content = (
                        f"{entry.content.rstrip()}\n{clean_content}"
                        if append
                        else clean_content
                    ).strip()
                    entry.updated_at = _now()
                else:
                    entry = LTMEntry(
                        id=_new_id(),
                        content=clean_content,
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
                affected_categories = self._write_entry_row(conn, entry)
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
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
        return [_row_mappers._row_to_entry(row) for row in rows]

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
            return [_row_mappers._row_to_entry(row) for row in rows]

        latin_terms = [term for term in query_terms if LATIN_TOKEN_RE.fullmatch(term)]
        cjk_terms = [term for term in query_terms if term not in latin_terms]
        results_by_id: dict[str, LTMEntry] = {}

        def _merge(rows: list[sqlite3.Row]) -> None:
            for row in rows:
                entry = _row_mappers._row_to_entry(row)
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

    def corpus_stats(
        self,
        terms: list[str],
        scopes: Optional[list[str]] = None,
    ) -> 'CorpusStats':
        """Corpus-wide document frequencies for *terms*, plus the corpus size."""
        with self._connect() as conn:
            return queries.corpus_stats(
                conn,
                terms=terms,
                scopes=scopes,
            )


    # ── Maintenance ───────────────────────────────────────────────────────────

    def apply_decay(self, factor: float = shared.DECAY_FACTOR) -> None:
        """Decay importance of all entries; prune those below shared.MIN_IMPORTANCE.

        Pushed into SQL so it no longer fetches every entry into Python and
        writes them back one row at a time (the old per-row _write_entry_row
        also re-synced each entry's FTS row individually).  Mirrors
        apply_retention, which already does this for episodes.
        """
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_items
                SET importance = importance * ?,
                    updated_at = ?
                WHERE status NOT IN ('archived', 'superseded')
                """,
                (factor, now),
            )
            pruned = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM memory_items
                    WHERE status NOT IN ('archived', 'superseded')
                      AND importance < ?
                    """,
                    (shared.MIN_IMPORTANCE,),
                ).fetchall()
            ]
            if pruned:
                self._delete_fts_rows(conn, pruned)
                conn.execute(
                    f"DELETE FROM memory_items WHERE id IN ({','.join('?' for _ in pruned)})",
                    pruned,
                )
        self._category_stats_cache = None

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
        usage_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_USAGE_EVENT_RETENTION_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
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
            # The usage ledger is append-only by design (one row per provider
            # call); bound it here so a long-running gateway cannot grow the
            # table without limit. created_at is fixed-width UTC, so the
            # lexicographic comparison tracks time order.
            conn.execute(
                "DELETE FROM usage_events WHERE created_at < ?",
                (usage_cutoff,),
            )
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
