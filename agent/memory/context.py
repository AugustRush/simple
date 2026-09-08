"""Orchestrates long-term storage, retrieval, and consolidation for a turn."""

from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Optional

from agent import shared
from agent.lexical import lexical_terms

from ._helpers import (
    _FACT_QUERY_PREDICATE_ALIASES,
    _FACT_QUERY_SUBJECT_ALIASES,
    _emit_consolidation,
    _lexical_terms,
    _now,
)
from .consolidation import ConsolidationEngine
from .models import (
    AgentRuntimeEvent,
    ConsolidationResult,
    ContextLimitError,
    ConversationWriteResult,
    LTMEntry,
    QueryPlan,
    ResolvedFact,
    SessionWorkingState,
)
from .retrieval import LocalRetriever
from .staging import StagingBuffer
from .store import LTMStore

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
        self._suppress_next_turn_persistence = False
        self.last_working_state_recovery_trace: dict[str, Any] = {}

    def on_memory_cleared(self) -> None:
        """Drop queued extraction work and keep the clearing turn forgotten."""
        with self._lock:
            self._jobs.clear()
            self._needs_consolidation = False
            self._last_activity = 0.0
            self._suppress_next_turn_persistence = True
        self.staging.clear_all()

    def consume_memory_clear_suppression(self) -> bool:
        with self._lock:
            suppress = self._suppress_next_turn_persistence
            self._suppress_next_turn_persistence = False
            return suppress

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
        # ``StagingBuffer`` keeps the owning context directory explicitly for
        # both its SQLite and legacy JSONL backends.  Prefer that value over
        # inferring it from ``path``: callers may place a JSONL staging file in
        # an arbitrary subdirectory, and inferring from that path would make a
        # spawned session silently write a new ``palace.db`` in the wrong
        # directory.  The path fallback is retained for old test doubles.
        context_dir = getattr(self.staging, "context_dir", None)
        if context_dir is None:
            path = Path(self.staging.path)
            context_dir = (
                path.parent.parent
                if path.parent.name == "_staging"
                else path.parent
            )
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

    def record_turn(
        self,
        *,
        user_content: str,
        assistant_content: str = "",
        channel: str = "",
        message_id: str = "",
        assistant_message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Persist an exchange; return whether either event was newly created."""
        return self.record_turn_result(
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
        ).changed

    def record_turn_result(
        self,
        *,
        user_content: str,
        assistant_content: str = "",
        channel: str = "",
        message_id: str = "",
        assistant_message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> ConversationWriteResult:
        """Persist an exchange and expose which event rows were created."""
        session_id = self.staging.session_id
        write_result = self.store.write_conversation_exchange(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
        )
        return write_result

    def begin_turn(
        self,
        *,
        user_content: str,
        channel: str = "",
        message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Durably journal user intent before any fallible model work starts."""
        session_id = self.staging.session_id
        write_result = self.store.write_conversation_exchange(
            session_id=session_id,
            user_content=user_content,
            channel=channel,
            message_id=message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
        )
        if not write_result.user_created:
            return False
        return True

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

    def should_compact_messages(
        self, messages: list[dict], input_token_budget: int
    ) -> bool:
        """Return whether provider-bound messages violate the strict budget."""
        return self.consolidation.estimate_tokens(messages) >= int(
            input_token_budget
        )

    @staticmethod
    def _anthropic_tool_result_ids(message: dict) -> Optional[set[str]]:
        if message.get("role") != "user":
            return None
        content = message.get("content")
        if not isinstance(content, list) or not content:
            return None
        if not all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            return None
        return {str(block.get("tool_use_id") or "") for block in content}

    @classmethod
    def _is_real_user_request(cls, message: dict) -> bool:
        return message.get("role") == "user" and cls._anthropic_tool_result_ids(
            message
        ) is None

    @classmethod
    def _complete_message_units(
        cls, messages: list[dict], newest_request_index: int
    ) -> list[list[int]]:
        units: list[list[int]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            role = message.get("role")
            openai_calls = message.get("tool_calls") if role == "assistant" else None
            openai_ids = {
                str(call.get("id") or "")
                for call in openai_calls or []
                if isinstance(call, dict)
            }
            content = message.get("content")
            content_blocks = content if isinstance(content, list) else []
            anthropic_ids = {
                str(block.get("id") or "")
                for block in content_blocks
                if isinstance(block, dict) and block.get("type") == "tool_use"
            }
            expected_ids = openai_ids | anthropic_ids
            if expected_ids:
                if "" in expected_ids:
                    raise ContextLimitError("tool history is structurally incomplete")
                unit = [index]
                answered: set[str] = set()
                cursor = index + 1
                while cursor < len(messages):
                    following = messages[cursor]
                    if following.get("role") == "tool":
                        tool_id = str(following.get("tool_call_id") or "")
                        if tool_id not in expected_ids:
                            raise ContextLimitError(
                                "tool history contains an orphan tool result"
                            )
                        answered.add(tool_id)
                        unit.append(cursor)
                        cursor += 1
                        continue
                    result_ids = cls._anthropic_tool_result_ids(following)
                    if result_ids is not None:
                        if not result_ids or not result_ids.issubset(expected_ids):
                            raise ContextLimitError(
                                "tool history contains an orphan tool result"
                            )
                        answered.update(result_ids)
                        unit.append(cursor)
                        cursor += 1
                        continue
                    break
                if answered != expected_ids:
                    raise ContextLimitError("tool history is structurally incomplete")
                units.append(unit)
                index = cursor
                continue
            if role == "tool" or cls._anthropic_tool_result_ids(message) is not None:
                raise ContextLimitError("tool history contains an orphan tool result")
            if (
                role == "user"
                and index != newest_request_index
                and index + 1 < len(messages)
                and messages[index + 1].get("role") == "assistant"
                and not messages[index + 1].get("tool_calls")
                and not any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in (
                        messages[index + 1].get("content")
                        if isinstance(messages[index + 1].get("content"), list)
                        else []
                    )
                )
            ):
                units.append([index, index + 1])
                index += 2
                continue
            units.append([index])
            index += 1
        return units

    @classmethod
    def _repair_tool_history(cls, messages: list[dict]) -> list[dict]:
        """Drop incomplete provider protocol units left by interrupted turns."""
        repaired: list[dict] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            role = message.get("role")
            openai_calls = message.get("tool_calls") if role == "assistant" else None
            openai_ids = {
                str(call.get("id") or "")
                for call in openai_calls or []
                if isinstance(call, dict)
            }
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            anthropic_ids = {
                str(block.get("id") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_use"
            }
            expected_ids = openai_ids | anthropic_ids
            if expected_ids:
                cursor = index + 1
                answered: set[str] = set()
                result_messages: list[dict] = []
                while cursor < len(messages):
                    following = messages[cursor]
                    if following.get("role") == "tool":
                        tool_id = str(following.get("tool_call_id") or "")
                        answered.add(tool_id)
                        result_messages.append(following)
                        cursor += 1
                        continue
                    result_ids = cls._anthropic_tool_result_ids(following)
                    if result_ids is not None:
                        answered.update(result_ids)
                        result_messages.append(following)
                        cursor += 1
                        continue
                    break
                if "" not in expected_ids and answered == expected_ids:
                    repaired.append(message)
                    repaired.extend(result_messages)
                index = cursor
                continue
            if role == "tool" or cls._anthropic_tool_result_ids(message) is not None:
                index += 1
                continue
            repaired.append(message)
            index += 1
        if len(repaired) != len(messages):
            _emit_consolidation(
                "tool_history_repaired",
                messages_before=len(messages),
                messages_after=len(repaired),
            )
        return repaired

    # Identified by a text sentinel rather than a custom message key: both
    # Anthropic and OpenAI reject unknown fields on a message object.
    _EVICTION_SENTINEL = "[context-eviction]"

    @classmethod
    def _eviction_notice(cls, dropped_count: int) -> dict:
        return {
            "role": "user",
            "content": (
                f"{cls._EVICTION_SENTINEL} {dropped_count} earlier message(s) were "
                "dropped to fit the input budget. They are recoverable: use "
                "context_retrieve to search this session before assuming anything "
                "about them, rather than guessing or asking the user to repeat."
            ),
        }

    @classmethod
    def _strip_eviction_notices(cls, messages: list[dict]) -> tuple[list[dict], int]:
        """Remove prior notices and report how many messages they accounted for."""
        kept: list[dict] = []
        dropped = 0
        for message in messages:
            content = message.get("content")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and content.startswith(cls._EVICTION_SENTINEL)
            ):
                head = content[len(cls._EVICTION_SENTINEL):].strip().split(" ", 1)[0]
                if head.isdigit():
                    dropped += int(head)
                continue
            kept.append(message)
        return kept, dropped

    def compact_messages(
        self, messages: list[dict], *, input_token_budget: int
    ) -> list[dict]:
        budget = int(input_token_budget)
        if budget <= 0:
            raise ContextLimitError("provider input token budget is not positive")
        # Collapse any prior notice into a running count so repeated compactions
        # report cumulative loss instead of stacking notices.
        messages, dropped_before = self._strip_eviction_notices(messages)
        messages = self._repair_tool_history(messages)
        newest_request_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if self._is_real_user_request(messages[index])
            ),
            -1,
        )
        if newest_request_index >= 0 and self.consolidation.estimate_tokens(
            [messages[newest_request_index]]
        ) >= budget:
            raise ContextLimitError("newest user request exceeds provider input budget")

        retained = self._complete_message_units(messages, newest_request_index)

        def materialize() -> list[dict]:
            kept_indexes = {item for unit in retained for item in unit}
            return [msg for index, msg in enumerate(messages) if index in kept_indexes]

        compacted = materialize()
        while self.consolidation.estimate_tokens(compacted) >= budget:
            removable = next(
                (
                    unit
                    for unit in retained
                    if newest_request_index not in unit
                ),
                None,
            )
            if removable is None:
                raise ContextLimitError("complete conversation context exceeds input budget")
            retained.remove(removable)
            compacted = materialize()
        kept_indexes = {item for unit in retained for item in unit}
        dropped_messages = [
            message
            for index, message in enumerate(messages)
            if index not in kept_indexes
        ]
        dropped_now = dropped_before + len(dropped_messages)
        if dropped_now:
            # Evicted turns remain reachable through staging and the durable
            # store, but the model cannot retrieve what it does not know is
            # missing, so leave an explicit notice.  The notice is an aid, not a
            # requirement: if it does not fit alongside the conversation it is
            # omitted rather than allowed to fail the compaction it describes.
            notice = self._eviction_notice(dropped_now)
            if self.consolidation.estimate_tokens([notice] + compacted) < budget:
                compacted = [notice] + compacted
        if len(compacted) != len(messages):
            # Counts alone say how much was lost but not what: a caller
            # cannot tell a dropped tool batch from a dropped user request.
            # The role sequence and token estimate make that judgeable.
            _emit_consolidation(
                "compaction",
                messages_before=len(messages),
                messages_after=len(compacted),
                messages_dropped=dropped_now,
                dropped_roles=self._role_sequence(dropped_messages),
                dropped_tokens=self.consolidation.estimate_tokens(dropped_messages)
                if dropped_messages
                else 0,
            )
        return compacted

    @staticmethod
    def _role_sequence(messages: list[dict], limit: int = 40) -> str:
        """Comma-joined roles of *messages*, truncated to stay log-sized."""
        roles = [str(message.get("role", "?")) for message in messages[:limit]]
        if len(messages) > limit:
            roles.append(f"+{len(messages) - limit}")
        return ",".join(roles)

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

    def ltm_candidates(
        self,
        query: str,
        top_k: int = shared.RETRIEVAL_TOP_K,
    ) -> list[LTMEntry]:
        """Stage 1: the candidate set the re-ranker is allowed to choose from.

        Named and exposed because it is a hard ceiling on retrieval quality —
        an entry that stage 1 misses cannot be recovered by any amount of
        stage-2 ranking. Measuring the two stages separately is what tells you
        which one to work on; without the split, a recall failure and a
        ranking failure look identical from the outside.
        """
        scopes = ["global", f"session:{self.staging.session_id}"]
        candidates = self.store.search_entries(
            query,
            categories=None,
            limit=top_k * 6,
            scopes=scopes,
        )
        if not candidates and self._is_episode_recall_query(query):
            candidates = self.store.read_entries("episodes", scopes=scopes)[: top_k * 3]
        return candidates

    def rank_ltm_entries(
        self,
        query: str | list[str],
        top_k: int = shared.RETRIEVAL_TOP_K,
    ) -> list[LTMEntry]:
        """Return the top-K entries for *query*, highest-scoring first.

        *query* may be several phrasings.  This is the single highest-value
        thing the retrieval layer can offer, because the dominant failure is
        not ranking — it is that one unmodified user message is a bad query
        against a lexical index.  Measured on the eval set, 7 of 7 stage-1
        misses were recoverable by re-asking in the memory's own wording or
        language ("我在哪家公司做什么工作" finds nothing; "August 大疆 iOS"
        finds it).  The information was always reachable; the query was the
        problem.

        Candidates are unioned across phrasings and each entry keeps its best
        score, rather than concatenating the phrasings into one string —
        concatenation dilutes IDF across terms that belong to different
        formulations and ends up ranking worse than any single one of them.

        Two-stage retrieval:
          1. :meth:`ltm_candidates` fetches a broad candidate set via FTS5.
          2. LocalRetriever re-ranks candidates by relevance, using document
             frequencies measured over the **whole store**.
          3. Routed categories receive a small score bonus rather than hard
             filtering.
        """
        queries = [query] if isinstance(query, str) else list(query)
        queries = [str(q).strip() for q in queries if str(q).strip()]
        if not queries:
            return []

        scopes = ["global", f"session:{self.staging.session_id}"]
        best: dict[str, float] = {}
        entries: dict[str, LTMEntry] = {}
        routed: set[str] = set()

        for one in queries:
            candidates = self.ltm_candidates(one, top_k)
            if not candidates:
                continue
            routed.update(self._route_categories(one))
            corpus = self.store.corpus_stats(
                self.retriever.tokenize(one), scopes=scopes
            )
            for entry, score in self.retriever.score(one, candidates, corpus):
                if score > best.get(entry.id, 0.0):
                    best[entry.id] = score
                    entries[entry.id] = entry

        if not best:
            return []
        scored = [
            (entries[entry_id], score * (1.15 if entries[entry_id].category in routed else 1.0))
            for entry_id, score in best.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [entry for entry, score in scored[:top_k] if score > 0]

    def retrieve_ltm_context(
        self,
        query: str | list[str],
        top_k: int = shared.RETRIEVAL_TOP_K,
        token_budget: Optional[int] = None,
    ) -> str:
        """Format the top-K relevant LTM entries as an injectable string.

        Ranking lives in :meth:`rank_ltm_entries`; this method only decides
        how much of the result fits in *token_budget*.
        """
        top = self.rank_ltm_entries(query, top_k)
        if not top:
            return ""
        header = "## Retrieved Context (from long-term memory)"
        lines = [header]
        if token_budget is None or token_budget <= 0:
            for e in top:
                anchor = f"{e.category}/{e.entity}" if e.entity else e.category
                lines.append(f"- [{anchor}] {e.content}")
            return "\n".join(lines)
        # Fit at entry granularity.  Dropping the whole section because one
        # verbose memory overflowed would discard the other four for free, so
        # spend the budget on the highest-scoring entries that fit and say how
        # many were left out.
        estimate = self.consolidation.estimate_tokens
        used = estimate([{"role": "system", "content": header}])
        omitted = 0
        for e in top:
            anchor = f"{e.category}/{e.entity}" if e.entity else e.category
            line = f"- [{anchor}] {e.content}"
            cost = estimate([{"role": "system", "content": line}])
            if used + cost > token_budget:
                omitted += 1
                continue
            lines.append(line)
            used += cost
        if omitted:
            lines.append(f"- [{omitted} lower-ranked memor(ies) omitted: over budget]")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _recent_session_context(
        self,
        limit: int = shared.RECENT_SESSION_TURNS,
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Return the most recent staged turns for explicit current-session recall."""
        staged = self.staging.read_last(limit * 4)
        if not staged:
            # Staging may be empty after a restart (sleep archived turns to
            # conversation_turns).  Fall back to the durable store so the
            # agent still sees recent conversation history.
            if self.staging.session_id == "cli":
                turns = self.store.recent_conversation_turns(
                    channel="cli",
                    exclude_message_id=exclude_message_id,
                    limit=limit,
                )
            else:
                turns = self.store.recent_conversation_turns(
                    session_id=self.staging.session_id,
                    exclude_message_id=exclude_message_id,
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
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Return durable event-history evidence when the query asks for it."""
        if not self._is_episode_recall_query(query):
            return ""
        if self.staging.session_id == "cli":
            turns = self.store.recent_conversation_turns(
                channel="cli",
                exclude_message_id=exclude_message_id,
                limit=limit,
            )
        else:
            turns = self.store.search_conversation_turns(
                query,
                session_id=self.staging.session_id,
                exclude_message_id=exclude_message_id,
                limit=limit,
            )
            if not turns:
                turns = self.store.recent_conversation_turns(
                    session_id=self.staging.session_id,
                    exclude_message_id=exclude_message_id,
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
        staged = self.staging.read_last(limit * 4)
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
        missing = [
            msg
            for msg in staged
            if str(msg.get("content", "")).strip()
            and str(msg.get("content", "")).strip() not in visible_contents
        ]
        if not missing:
            return ""
        lines = ["## Current Session (not yet consolidated)"]
        for msg in missing[-limit:]:
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

    @staticmethod
    def _working_state_terms(state: dict[str, Any]) -> set[str]:
        parts: list[str] = []
        for key in ("active_goal", "progress"):
            value = str(state.get(key, "") or "").strip()
            if value:
                parts.append(value)
        artifacts = state.get("artifacts")
        if isinstance(artifacts, list):
            parts.extend(str(item or "").strip() for item in artifacts)
        recent_turns = state.get("recent_turns")
        if isinstance(recent_turns, list):
            for turn in recent_turns:
                if isinstance(turn, dict):
                    content = str(turn.get("content", "") or "").strip()
                    if content:
                        parts.append(content)
        return set(_lexical_terms(" ".join(parts)))

    @staticmethod
    def _weighted_term_overlap(
        query_terms: set[str],
        value: Any,
        *,
        weight: float,
    ) -> float:
        terms = set(_lexical_terms(str(value or "")))
        if not terms:
            return 0.0
        return len(query_terms & terms) * weight

    def _working_state_relevance_score(self, state: dict[str, Any], query: str) -> float:
        query_terms = set(_lexical_terms(query))
        if not query_terms:
            return 0.0

        score = 0.0
        score += self._weighted_term_overlap(
            query_terms, state.get("active_goal"), weight=2.0
        )
        score += self._weighted_term_overlap(
            query_terms, state.get("progress"), weight=1.0
        )
        score += self._weighted_term_overlap(
            query_terms, state.get("last_error"), weight=0.35
        )
        artifacts = state.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                score += self._weighted_term_overlap(query_terms, artifact, weight=2.5)
        recent_turns = state.get("recent_turns")
        if isinstance(recent_turns, list):
            for turn in recent_turns:
                if not isinstance(turn, dict):
                    continue
                role = str(turn.get("role", "") or "").strip().lower()
                weight = 1.5 if role == "user" else 0.75
                score += self._weighted_term_overlap(
                    query_terms,
                    turn.get("content", ""),
                    weight=weight,
                )
        return score

    @staticmethod
    def _working_state_is_complete(state: dict[str, Any]) -> bool:
        status = str(state.get("status", "") or "").strip().lower()
        return status in {"completed", "updated", "done", "success", "dismissed"}

    def _working_state_candidates(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        tasks = state.get("tasks")
        if isinstance(tasks, list):
            candidates = [task for task in tasks if isinstance(task, dict)]
            if candidates:
                return candidates
        return [state] if isinstance(state, dict) and state else []

    def _select_working_state(
        self,
        state: dict[str, Any],
        query: str,
        *,
        include_completed: bool = False,
    ) -> dict[str, Any] | None:
        decision = self._working_state_recovery_decision(
            state,
            query,
            include_completed=include_completed,
        )
        selected_task_id = str(decision.get("selected_task_id", "") or "")
        if not selected_task_id:
            return None
        for candidate in self._working_state_candidates(state):
            if str(candidate.get("task_id", "") or "") == selected_task_id:
                return candidate
        return None

    def _working_state_recovery_decision(
        self,
        state: dict[str, Any],
        query: str,
        *,
        include_completed: bool = False,
    ) -> dict[str, Any]:
        threshold = 2.0
        continuation_request = bool(
            re.search(
                r"(?:^|\s)(?:continue|resume)(?:\s|$)|继续|接着(?:做|来|完成)?|恢复(?:任务|工作)?",
                str(query or "").strip().lower(),
            )
        )
        candidates: list[dict[str, Any]] = []
        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in self._working_state_candidates(state):
            complete = self._working_state_is_complete(candidate)
            dismissed = (
                str(candidate.get("status", "") or "").strip().lower()
                == "dismissed"
            )
            score = self._working_state_relevance_score(candidate, query)
            # An explicit dismissal is durable user intent.  Even a generic
            # "continue" request must not resurrect that task automatically.
            eligible = False if dismissed else (
                include_completed or continuation_request or not complete
            )
            candidate_trace = {
                "task_id": str(candidate.get("task_id", "") or ""),
                "status": str(candidate.get("status", "") or ""),
                "score": round(score, 3),
                "eligible": bool(eligible),
                "active_goal": self._clip_working_state_text(
                    candidate.get("active_goal", ""), 120
                ),
            }
            candidates.append(candidate_trace)
            if eligible and (score >= threshold or continuation_request):
                scored.append((score, candidate))
        selected: dict[str, Any] | None = None
        if not scored:
            selected = None
        else:
            scored.sort(
                key=lambda item: (
                    item[0],
                    str(item[1].get("updated_at", "")),
                    str(item[1].get("task_id", "")),
                ),
                reverse=True,
            )
            selected = scored[0][1]
        selected_score = (
            self._working_state_relevance_score(selected, query)
            if selected is not None
            else 0.0
        )
        return {
            "query": query,
            "threshold": threshold,
            "selected": selected is not None,
            "selected_task_id": str((selected or {}).get("task_id", "") or ""),
            "selected_goal": str((selected or {}).get("active_goal", "") or ""),
            "selected_status": str((selected or {}).get("status", "") or ""),
            "selected_score": round(selected_score, 3),
            "candidates": candidates,
        }

    def _should_include_working_state(self, state: dict[str, Any], query: str) -> bool:
        if not isinstance(state, dict) or not state:
            return False
        return self._select_working_state(state, query) is not None

    def working_state_context(self, query: str = "") -> str:
        snapshot = self.store.load_session_working_state(self.staging.session_id)
        if snapshot is None:
            self.last_working_state_recovery_trace = {
                "query": query,
                "threshold": 2.0,
                "selected": False,
                "selected_task_id": "",
                "selected_goal": "",
                "selected_status": "",
                "selected_score": 0.0,
                "candidates": [],
            }
            return ""
        self.last_working_state_recovery_trace = self._working_state_recovery_decision(
            snapshot.state,
            query,
        )
        selected_state = self._select_working_state(snapshot.state, query)
        if selected_state is None:
            return ""
        text = self._format_working_state_text(selected_state)
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

        error_lower = clean_error.lower()
        if clean_error and "cancel" in error_lower:
            status = "cancelled"
        elif clean_error:
            status = "failed"
        elif clean_assistant:
            status = "completed"
        else:
            status = "in_progress"

        prior_tasks = [
            dict(task)
            for task in self._working_state_candidates(prior_state)
            if isinstance(task, dict) and task
        ]
        if (
            prior_state
            and not isinstance(prior_state.get("tasks"), list)
            and str(prior_state.get("active_goal", "") or "").strip()
        ):
            prior_tasks = [dict(prior_state)]

        matched_task: dict[str, Any] | None = None
        if clean_user and prior_tasks:
            scored_matches = [
                (self._working_state_relevance_score(task, clean_user), task)
                for task in prior_tasks
                if not self._working_state_is_complete(task)
            ]
            scored_matches = [
                (score, task) for score, task in scored_matches if score >= 2.0
            ]
            if scored_matches:
                scored_matches.sort(
                    key=lambda item: (
                        item[0],
                        str(item[1].get("updated_at", "")),
                        str(item[1].get("task_id", "")),
                    ),
                    reverse=True,
                )
                matched_task = dict(scored_matches[0][1])
        elif prior_tasks:
            open_tasks = [
                task for task in prior_tasks if not self._working_state_is_complete(task)
            ]
            if open_tasks:
                matched_task = dict(open_tasks[-1])

        task_fingerprint = hashlib.sha1(
            f"{self.staging.session_id}\0{clean_user}\0{created_at}".encode("utf-8")
        ).hexdigest()[:12]
        task_id = str(
            (matched_task or {}).get("task_id")
            or f"task-{task_fingerprint}"
        )
        task_recent_turns = (
            list((matched_task or {}).get("recent_turns", []))
            if isinstance((matched_task or {}).get("recent_turns"), list)
            else []
        )
        if clean_user:
            task_recent_turns.append({"role": "user", "content": clean_user})
        if clean_assistant:
            task_recent_turns.append({"role": "assistant", "content": clean_assistant})
        task_recent_turns = task_recent_turns[-8:]

        progress = clean_assistant or str((matched_task or {}).get("progress", "") or "")
        next_action = str((matched_task or {}).get("next_action", "") or "")
        if clean_error:
            next_action = "Recover from the last error and continue the active goal if the user's next message is related."
        elif clean_assistant:
            next_action = "Use this working context only when it is relevant to the user's next message."

        artifacts = self._merge_artifacts(
            payload.get("artifacts"),
            (matched_task or {}).get("artifacts"),
        )
        current_task = {
            "task_id": task_id,
            "active_goal": clean_user
            or str((matched_task or {}).get("active_goal", "") or ""),
            "status": status,
            "progress": progress,
            "next_action": next_action,
            "last_error": clean_error,
            "artifacts": artifacts,
            "recent_turns": task_recent_turns,
            "last_event_type": event_type,
            "updated_at": created_at,
        }
        tasks_by_id: dict[str, dict[str, Any]] = {}
        for task in prior_tasks:
            existing_id = str(task.get("task_id") or "")
            if not existing_id:
                existing_id = f"legacy-{len(tasks_by_id)}"
                task["task_id"] = existing_id
            tasks_by_id[existing_id] = task
        tasks_by_id[task_id] = current_task
        tasks = sorted(
            tasks_by_id.values(),
            key=lambda task: str(task.get("updated_at", "")),
        )[-12:]
        state = dict(current_task)
        state["tasks"] = tasks
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

    def retrieve_context(
        self,
        query: str | list[str],
        top_k: int = shared.RETRIEVAL_TOP_K,
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Return explicit context lookup results across active session and LTM.

        *query* may be several phrasings; see :meth:`rank_ltm_entries` for why
        that is the highest-value knob in this layer.  The session/fact
        lookups use the first phrasing (they match structured subjects rather
        than free text, so extra wordings add nothing), while the free-text
        LTM search unions all of them.
        """
        queries = [query] if isinstance(query, str) else list(query)
        queries = [str(q).strip() for q in queries if str(q).strip()]
        if not queries:
            return ""
        query = queries[0]
        plan = self._plan_query(query)
        sections = []
        history = self.retrieve_history_context(
            query,
            exclude_message_id=exclude_message_id,
        )
        if history:
            sections.append(history)
        recent = (
            ""
            if history
            else self._recent_session_context(exclude_message_id=exclude_message_id)
        )
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
        ltm = (
            self.retrieve_ltm_context(queries, top_k=top_k)
            if include_freeform
            else ""
        )
        if ltm:
            sections.append(ltm)
        return "\n\n".join(sections)

    def retrieve_implicit_context(
        self,
        query: str,
        top_k: int = shared.RETRIEVAL_TOP_K,
        current_messages: Optional[list[dict]] = None,
        current_turn_id: str = "",
        token_budget: Optional[int] = None,
    ) -> str:
        """Return context for automatic prompt injection.

        Keep routine prompt augmentation focused on LTM, and only include the
        in-session staging buffer when the user is explicitly asking to recall
        recent conversation.

        ``token_budget`` bounds the result in the same currency as every other
        payload decision.  ``top_k`` bounds the *count* of entries, which says
        nothing about their size: five verbose memories measured 112k tokens,
        and because this text is injected into the system prompt — which
        compaction never touches — that either starved the conversation of room
        or drove the input budget negative and hard-failed the turn.
        """
        plan = self._plan_query(query)
        sections: list[str] = []
        assistant_identity = self._assistant_identity_context()
        if assistant_identity:
            sections.append(assistant_identity)
        working_state = self.working_state_context(query)
        if working_state:
            sections.append(working_state)
        if "episodes" in self._route_categories(query):
            recent = self._recent_session_context(
                exclude_message_id=current_turn_id,
            )
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
        # Give LTM whatever the higher-priority sections left unspent, so a
        # verbose memory is trimmed to fit rather than dropped wholesale.
        ltm = ""
        if include_freeform:
            spent = 0
            if token_budget:
                spent = sum(
                    self.consolidation.estimate_tokens(
                        [{"role": "system", "content": section}]
                    )
                    for section in sections
                )
            ltm = self.retrieve_ltm_context(
                query,
                top_k=top_k,
                token_budget=(max(0, token_budget - spent) if token_budget else None),
            )
        if ltm:
            sections.append(ltm)
        return self._fit_sections_to_budget(sections, token_budget)

    def _fit_sections_to_budget(
        self, sections: list[str], token_budget: Optional[int]
    ) -> str:
        """Join sections, dropping whole ones that exceed the budget.

        Sections arrive in priority order (identity, working state, recent
        turns, facts, then free-form LTM), so shedding from the tail drops the
        least important material first.  Dropping a whole section beats
        truncating mid-sentence: half a remembered fact still reads as a
        complete one, which is worse than its absence.  A notice records what
        was shed so the omission is visible rather than silent.
        """
        if token_budget is None or token_budget <= 0 or not sections:
            return "\n\n".join(sections)
        estimate = self.consolidation.estimate_tokens
        kept: list[str] = []
        dropped = 0
        used = 0
        for section in sections:
            cost = estimate([{"role": "system", "content": section}])
            if used + cost > token_budget:
                dropped += 1
                continue
            kept.append(section)
            used += cost
        if dropped:
            kept.append(
                f"[{dropped} lower-priority context section(s) omitted to fit the "
                f"{token_budget}-token retrieval budget]"
            )
        return "\n\n".join(kept)

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

        staging_buffer: Optional[StagingBuffer] = None
        is_primary_staging = True
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
            if staging_buffer is not None and not is_primary_staging:
                staging_buffer.close()
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
