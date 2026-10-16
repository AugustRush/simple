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

from . import entry_writes
from . import maintenance
from . import search
from . import entries
from . import conversations
from . import normalize
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
        """Reconcile stored identity facts with the writers that exist today."""
        with self._connect() as conn:
            facts._repair_assistant_identity_facts(
                conn,
            )


    def _derive_missing_identity_facts(
        self, conn: sqlite3.Connection
    ) -> set[tuple[str, str]]:
        return facts._derive_missing_identity_facts(
            conn,
        )


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
        with self._connect() as conn:
            return maintenance.clear_persistent_memory(
                conn,
                invalidate=self._sync_after_mutation,
            )


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
        return normalize.normalize_category_name(
            name=name,
        )


    def _is_palace_locus(self, category: str) -> bool:
        return normalize._is_palace_locus(
            category=category,
        )


    def _normalize_entity(self, entity: str, category: str) -> str:
        return normalize._normalize_entity(
            entity=entity,
            category=category,
        )








    @staticmethod
    def _normalize_content_key(content: str) -> str:
        return normalize._normalize_content_key(
            content=content,
        )


    def _stable_merge_key(self, entry: LTMEntry) -> Optional[str]:
        return normalize._stable_merge_key(
            entry=entry,
        )


    def _match_existing_entry_id(
        self, conn: sqlite3.Connection, entry: LTMEntry
    ) -> Optional[str]:
        return entries._match_existing_entry_id(
            conn,
            entry=entry,
        )


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
        return entry_writes._write_entry_row(
            conn,
            entry=entry,
            invalidate=self._sync_after_mutation,
            max_categories=self.max_categories,
        )


    def _find_existing_entry_row(
        self, conn: sqlite3.Connection, entry: LTMEntry
    ) -> Optional[sqlite3.Row]:
        return entries._find_existing_entry_row(
            conn,
            entry=entry,
        )


    def _coerce_category_for_storage(
        self, conn: sqlite3.Connection, category: str
    ) -> str:
        return entry_writes._coerce_category_for_storage(
            conn,
            category=category,
            max_categories=self.max_categories,
        )


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
        with self._connect() as conn:
            entry_writes._remove_category(
                conn,
                category=category,
                invalidate=self._sync_after_mutation,
                max_categories=self.max_categories,
            )


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
        with self._connect() as conn:
            return entries.read_entries(
                conn,
                category=category,
                scopes=scopes,
            )


    def read_entries_for_entity(self, category: str, entity: str) -> list[LTMEntry]:
        with self._connect() as conn:
            return entries.read_entries_for_entity(
                conn,
                category=category,
                entity=entity,
            )


    def read_manual_note(self, category: str, entity: str) -> Optional[LTMEntry]:
        with self._connect() as conn:
            return entries.read_manual_note(
                conn,
                category=category,
                entity=entity,
            )


    def _fact_assertions_from_entry(self, entry: LTMEntry) -> list[FactAssertion]:
        """Derive the identity *prose* an identity entry states, and only that."""
        return facts._fact_assertions_from_entry(
            self._connect(),
            entry=entry,
        )


    def write_entries(self, category: str, entries: list[LTMEntry]) -> None:
        with self._connect() as conn:
            entry_writes.write_entries(
                conn,
                category=category,
                entries=entries,
                invalidate=self._sync_after_mutation,
                max_categories=self.max_categories,
            )


    def add_entry(self, entry: LTMEntry) -> None:
        with self._connect() as conn:
            entry_writes.add_entry(
                conn,
                entry=entry,
                invalidate=self._sync_after_mutation,
                max_categories=self.max_categories,
            )


    def add_entries(self, entries: list[LTMEntry]) -> None:
        """Batch-insert multiple entries in a single transaction and one sync pass."""
        with self._connect() as conn:
            entry_writes.add_entries(
                conn,
                entries=entries,
                invalidate=self._sync_after_mutation,
                max_categories=self.max_categories,
            )


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
        return conversations._json_object(
            raw=raw,
        )


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
        """Idempotently journal a user event and optional assistant completion."""
        return conversations.write_conversation_exchange(
            self._connect(),
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
            interjections=interjections,
            created_at=created_at,
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
        return facts._fact_assertion_total_score(
            assertion=assertion,
        )


    def add_fact_assertion(self, assertion: FactAssertion) -> FactAssertion:
        with self._connect() as conn:
            return facts.add_fact_assertion(
                conn,
                assertion=assertion,
            )


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
        """Filter one fact table by the three optional keys and read it back."""
        with self._connect() as conn:
            return facts._read_facts(
                conn,
                table=table,
                order_by=order_by,
                to_row=to_row,
                subject=subject,
                predicate=predicate,
                scope=scope,
            )


    def read_fact_assertions(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[FactAssertion]:
        with self._connect() as conn:
            return facts.read_fact_assertions(
                conn,
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
        """Resolve a fact; the transaction boundary depends on who supplied the connection.

        The implementation is :func:`agent.memory.facts.resolve_fact`, which writes -- so the
        distinction matters.  With ``_conn`` the caller owns the transaction and nothing is
        committed here (it used to be ``contextlib.nullcontext``); without it this opens the
        thread-local connection, and leaving the ``with`` block commits.
        """
        if _conn is not None:
            return facts.resolve_fact(_conn, subject, predicate, scope=scope)
        with self._connect() as conn:
            return facts.resolve_fact(conn, subject, predicate, scope=scope)



    def read_resolved_facts(
        self,
        *,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> list[ResolvedFact]:
        with self._connect() as conn:
            return facts.read_resolved_facts(
                conn,
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
            return facts.has_conflicted_fact(
                conn,
                subject=subject,
                predicate=predicate,
                scope=scope,
            )


    def set_identity(
        self,
        *,
        subject: str = "assistant",
        name: Optional[str] = None,
        role: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> dict[str, str]:
        """Record identity as a deliberate setting, last write wins."""
        with self._connect() as conn:
            return facts.set_identity(
                conn,
                subject=subject,
                name=name,
                role=role,
                persona=persona,
            )


    def retract_facts(self, subject: str, predicate: str, scope: str = "global") -> int:
        """Archive every assertion behind one fact and drop the resolved row."""
        with self._connect() as conn:
            return facts.retract_facts(
                conn,
                subject=subject,
                predicate=predicate,
                scope=scope,
            )


    def upsert_manual_note(
        self, category: str, entity: str, content: str, append: bool = False
    ) -> LTMEntry:
        with self._connect() as conn:
            return entry_writes.upsert_manual_note(
                conn,
                category=category,
                entity=entity,
                content=content,
                append=append,
                invalidate=self._sync_after_mutation,
                max_categories=self.max_categories,
            )


    def all_entries(self) -> list[LTMEntry]:
        with self._connect() as conn:
            return entries.all_entries(
                conn,
            )


    def search_entries(
        self,
        query: str,
        categories: Optional[list[str]] = None,
        limit: int = shared.RETRIEVAL_TOP_K,
        scopes: Optional[list[str]] = None,
    ) -> list[LTMEntry]:
        with self._connect() as conn:
            return search.search_entries(
                conn,
                query=query,
                categories=categories,
                limit=limit,
                scopes=scopes,
            )


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
        """Decay importance of all entries; prune those below shared.MIN_IMPORTANCE."""
        with self._connect() as conn:
            maintenance.apply_decay(
                conn,
                factor=factor,
                invalidate=self._sync_after_mutation,
            )


    def apply_retention(self) -> None:
        """Apply locus-aware retention: decay episodes in-database, leave others untouched."""
        with self._connect() as conn:
            maintenance.apply_retention(
                conn,
                invalidate=self._sync_after_mutation,
            )


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
        with self._connect() as conn:
            entry_writes.merge_categories(
                conn,
                cat_a=cat_a,
                cat_b=cat_b,
                merged_name=merged_name,
                invalidate=self._sync_after_mutation,
                max_categories=self.max_categories,
            )

