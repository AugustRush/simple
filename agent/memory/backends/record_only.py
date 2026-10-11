"""A second ``ContextService``: records and retrieves, never consolidates.

Why this exists
---------------
``agent/memory/contracts.py`` describes two layers, and only the lower one had a
second implementation.  The store was *proven* replaceable -- SQLite FTS5 passes
the same ``run_store_contract`` -- while ``ContextService`` was only *declared*
replaceable: a protocol derived from call sites, checked for signatures, and never
implemented twice.  This is that second implementation, and the point of it is not
that it is better: it is that a different **policy** can satisfy the same contract.

What its policy is
------------------
* conversation history: journaled through the store exactly as the built-in
  manager does, because that is what the contract's idempotency rules are about;
* retrieval: entries are ranked by the store's own search and rendered for the
  prompt;
* consolidation: **none**.  Turns are staged nowhere, no job is ever queued by the
  service's own triggers, and ``process_one_job`` consumes a job the caller
  enqueued without calling a model.  A deployment that wants a memory of what was
  said, without paying a model to summarize it, is what this is for;
* compaction: none.  ``fit_to_budget`` still trims to fit -- that is a promise the
  contract makes, not a policy -- and ``compact_messages`` returns the messages.

Everything it does not do is a *policy* the contract leaves open, which is exactly
what makes it a useful second implementation: if the contract demanded the
built-in's behaviour anywhere, this file would fail ``run_context_contract``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..models import AgentRuntimeEvent, ConversationTurn, LTMEntry
from ..store import LTMStore

__all__ = ["RecordOnlyContextPlugin", "RecordOnlyContextService"]


class RecordOnlyContextService:
    """A :class:`agent.memory.contracts.ContextService` that only records."""

    def __init__(self, store: LTMStore, session_id: str) -> None:
        self.store = store
        self._session_id = str(session_id or "default")
        self._jobs: list[str] = []

    # ── identity and lifecycle ──────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    def mark_activity(self) -> None:
        """Nothing to track: this implementation has no idle policy."""

    def on_memory_cleared(self) -> None:
        """Drop queued work, for the same reason the built-in does."""

        self._jobs.clear()

    def close(self) -> None:
        close_store = getattr(self.store, "close", None)
        if callable(close_store):
            close_store()

    def spawn_session(
        self, session_id: Optional[str] = None, *, project_scope: str = ""
    ) -> "RecordOnlyContextService":
        """A sibling for one chat, sharing the store and nothing else.

        ``project_scope`` is accepted and ignored: this implementation stores no
        project-scoped entries of its own, so it has nothing to scope.
        """

        import uuid

        return RecordOnlyContextService(
            self.store, session_id or uuid.uuid4().hex
        )

    # ── staging: deliberately absent ────────────────────────────────────────

    def stage_turn(self, role: str, content: str) -> None:
        """Accepted and discarded: this implementation does not consolidate.

        The contract asks for the *act*, so a caller does not have to know whether
        anything is staged -- and here nothing is.
        """

    def has_enough_staged(self) -> bool:
        return False

    def has_answer(self, *, session_id: str, message_id: str) -> bool:
        """The store's optional query, feature-detected the same way."""

        check = getattr(self.store, "message_has_answer", None)
        if not callable(check) or not message_id:
            return False
        return bool(check(session_id=str(session_id or ""), message_id=str(message_id)))

    def recover_orphan_staging(self) -> int:
        """Nothing is ever staged, so nothing can be stranded."""

        return 0

    # ── entries ─────────────────────────────────────────────────────────────

    def remember(self, entry: LTMEntry) -> None:
        self.store.add_entry(entry)

    # ── recording ───────────────────────────────────────────────────────────

    def begin_turn(
        self,
        *,
        user_content: str,
        channel: str = "",
        message_id: str = "",
        reply_to_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        result = self.store.write_conversation_exchange(
            session_id=self._session_id,
            user_content=user_content,
            channel=channel,
            message_id=message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
        )
        return bool(result.user_created)

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
        result = self.store.write_conversation_exchange(
            session_id=self._session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
        )
        return bool(result.changed)

    # ── retrieval ───────────────────────────────────────────────────────────

    def _render(self, query: str, top_k: int) -> str:
        entries = self.store.search_entries(query, limit=max(1, int(top_k)))
        if not entries:
            return ""
        lines = ["## Relevant memory"]
        for entry in entries:
            lines.append(f"- ({entry.category}/{entry.memory_type}) {entry.content}")
        return "\n".join(lines)

    def retrieve_context(
        self,
        query: "str | list[str]",
        top_k: int = 10,
        *,
        exclude_message_id: str = "",
    ) -> str:
        terms = [query] if isinstance(query, str) else list(query)
        rendered = [self._render(term, top_k) for term in terms if str(term).strip()]
        return "\n\n".join(part for part in rendered if part)

    def retrieve_implicit_context(
        self,
        query: str,
        top_k: int = 10,
        current_messages: Optional[list[dict]] = None,
        current_turn_id: str = "",
        token_budget: Optional[int] = None,
        include_recent_session: bool = True,
        include_working_state: bool = True,
    ) -> str:
        """What the built-in calls "implicit": here, the same search.

        No recent-session block and no working state: both are the built-in's
        *assembly* of the same facts, and the contract asks for a prompt fragment,
        which this is.
        """

        return self.retrieve_context(query, top_k, exclude_message_id=current_turn_id)

    # ── consolidation: the policy this implementation differs on ────────────

    def should_enqueue_consolidation(self) -> bool:
        return False

    def should_session_end_sleep(self) -> bool:
        return False

    def enqueue_consolidation(self, reason: str) -> None:
        """Accepted, so a caller that wants to ask later can.

        ``process_one_job`` answers it without a model: this implementation has
        nothing to summarize.
        """

        self._jobs.append(str(reason or "manual"))

    def pending_jobs(self) -> int:
        return len(self._jobs)

    async def process_one_job(
        self,
        endpoint: Any,
        model: str,
        extractor: Optional[Callable[..., list[Any]]] = None,
    ) -> bool:
        if not self._jobs:
            return False
        self._jobs.pop(0)
        return True

    async def sleep(
        self, messages: list[dict], endpoint: Any, model: str
    ) -> list[dict]:
        """Return the messages unchanged: nothing to summarize, nothing to drop."""

        return messages

    # ── compaction: trimming is a promise, summarizing is not ───────────────

    def should_compact_messages(
        self, messages: list[dict], input_token_budget: int
    ) -> bool:
        return False

    def compact_messages(
        self,
        messages: list[dict],
        *,
        input_token_budget: int,
        protected: Optional[Sequence[dict]] = None,
    ) -> list[dict]:
        return list(messages)

    @classmethod
    def fit_to_budget(
        cls,
        messages: list[dict],
        *,
        input_token_budget: int,
        estimate_tokens: Callable[[list[dict]], int],
        protected: Optional[Sequence[dict]] = None,
    ) -> list[dict]:
        """Drop the oldest messages until the estimate fits, never the last one."""

        kept = list(messages)
        budget = max(0, int(input_token_budget))
        while len(kept) > 1 and estimate_tokens(kept) > budget:
            kept.pop(0)
        return kept


class RecordOnlyContextPlugin:
    """Provides ``context_manager`` -- record and retrieve, no consolidation."""

    name = "record-only-context"

    inject = ("cfg", "ltm_store")

    def setup(self, ctx: Any) -> None:
        store = ctx.get("ltm_store")
        session_id = str(ctx.get("session_id", "") or "")
        manager = RecordOnlyContextService(store, session_id or "default")
        ctx.on_unwind(manager.on_memory_cleared)
        ctx.provide("context_manager", manager)
