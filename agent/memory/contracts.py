"""What a memory implementation has to provide, written down.

Why this exists
---------------
``memory`` and ``ltm_store`` are services, a profile can declare that they come
from a different plugin, and the composition checks that the declared plugin
really provided them.  None of that makes a replacement *possible*: the interface
was whatever the call sites happened to touch, and there was no way to find out
what that was short of reading the tree.

``scripts/service_seam_audit.py`` answers "what is used" mechanically; this
module answers "what is required".  The two are kept equal by
``tests/test_memory_contract.py``, which compares every member here with the real
class and fails on a signature that drifted.

Three services, three contracts
-------------------------------
``ContextService``
    What ``agent/memory/session_plugin.py`` provides as ``context_manager``, and
    what the rest of the application may call: recording a turn, retrieving
    context, compaction, consolidation jobs, the end-of-session sleep.  This is
    the **narrow** interface -- 15 members out of the class's 36 -- and it is the
    one that matters when the *whole* memory system is replaced: a plugin that
    provides ``context_manager`` satisfies this and nothing else.
``MemoryService``
    The palace, provided as ``memory``: the chapter reader/writer the CLI, the
    channels, the memory tools and the evolution plugin use.
``MemoryStore``
    The store, provided as ``ltm_store``: the **internal** contract between a
    store and this project's own ``ContextManager``/``ConsolidationEngine``/
    ``LocalRetriever``/``MemoryPalace``.  It is much wider than the other two
    (33 members) because those four modules are what read and write it, and it is
    the contract to satisfy when only the *storage* is replaced.

A replacement may satisfy one of them or all three: keep the built-in
``ContextManager`` and swap the store (implement ``MemoryStore``), or provide a
whole memory system of your own (implement ``ContextService`` and put whatever
you like behind it).  ``service_replacements`` names which service you are
replacing, which is exactly the granularity this file makes explicit.

Two members are softer than the rest
------------------------------------
``MemoryStore.message_has_answer`` and ``MemoryStore.usage_summary`` are looked up
with ``getattr(..., None)`` by their two callers, so an implementation that omits
them still boots and only loses that feature (a duplicate-turn guard, and the
usage panel).  They are declared here because a replacement should provide them,
not because the application requires them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - types only
    from pathlib import Path

    from agent.core.transport import ModelEndpoint
    from agent.memory.models import (
        AgentRuntimeEvent,
        ConversationTurn,
        ConversationWriteResult,
        FactAssertion,
        LTMCategory,
        LTMEntry,
        ResolvedFact,
        SessionWorkingState,
    )
    from agent.memory.retrieval import CorpusStats

__all__ = ["ContextService", "MemoryService", "MemoryStore"]


@runtime_checkable
class MemoryStore(Protocol):
    """Durable storage: entries, facts, history, working state, events.

    Grouped by what the caller is doing rather than by table, because that is how
    a re-implementation gets written: the first group is "remember this", the
    third is "what did we say", and so on.
    """

    # ── data the callers read ───────────────────────────────────────────────
    @property
    def memory_dir(self) -> "Path":
        """Where the palace keeps what it exports (``MemoryPalace`` reads it)."""

    @property
    def max_categories(self) -> int:
        """The category ceiling consolidation reports against (read by
        ``ContextManager``)."""

    # ── long-term entries ───────────────────────────────────────────────────
    def add_entry(self, entry: "LTMEntry") -> None:
        """Insert or replace one entry by id."""

    def add_entries(self, entries: "list[LTMEntry]") -> None:
        """Insert or replace a batch."""

    def read_entries(
        self, category: str, *, scopes: "Optional[list[str]]" = None
    ) -> "list[LTMEntry]":
        """Every active entry in one category, newest score first."""

    def read_entries_for_entity(self, category: str, entity: str) -> "list[LTMEntry]":
        """Every entry in one category belonging to one entity."""

    def all_entries(self) -> "list[LTMEntry]":
        """Every entry, for export and maintenance."""

    def search_entries(
        self,
        query: str,
        categories: "Optional[list[str]]" = None,
        limit: int = 10,
        scopes: "Optional[list[str]]" = None,
    ) -> "list[LTMEntry]":
        """Ranked search over entries."""

    def list_categories(self) -> "list[LTMCategory]":
        """The known categories, with counts."""

    def dynamic_category_count(self) -> int:
        """How many categories were created at runtime rather than declared."""

    def normalize_category_name(name: str) -> str:
        """Map a spelled category to its canonical name (a static method)."""

    def corpus_stats(
        self, terms: "list[str]", scopes: "Optional[list[str]]" = None
    ) -> "CorpusStats":
        """Corpus-level statistics the retriever scores against."""

    def apply_retention(self) -> None:
        """Enforce the retention policy.  Must be safe to call at any time."""

    def clear_persistent_memory(self) -> "dict[str, int]":
        """Remove every durable memory.  Returns how much went."""

    # ── manual notes and identity ───────────────────────────────────────────
    def read_manual_note(self, category: str, entity: str) -> "Optional[LTMEntry]":
        """The one hand-written note for an entity, if any."""

    def upsert_manual_note(
        self, category: str, entity: str, content: str, append: bool = False
    ) -> "LTMEntry":
        """Write the hand-written note for an entity, replacing or appending."""

    def set_identity(
        self,
        *,
        subject: str = "assistant",
        name: "Optional[str]" = None,
        role: "Optional[str]" = None,
        persona: "Optional[str]" = None,
    ) -> "dict[str, str]":
        """Record who the assistant (or another subject) is."""

    # ── conversation history ────────────────────────────────────────────────
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
        metadata: "Optional[dict[str, Any]]" = None,
        interjections: "Optional[Sequence[dict[str, Any]]]" = None,
        created_at: "Optional[str]" = None,
    ) -> "ConversationWriteResult":
        """Journal one exchange.  Idempotent per *execution*, not per message."""

    def recent_conversation_turns(
        self,
        *,
        session_id: "Optional[str]" = None,
        channel: "Optional[str]" = None,
        exclude_message_id: str = "",
        limit: int = 20,
    ) -> "list[ConversationTurn]":
        """The newest turns, oldest first."""

    def search_conversation_turns(
        self,
        query: str,
        *,
        session_id: str,
        exclude_message_id: str = "",
        limit: int = 20,
    ) -> "list[ConversationTurn]":
        """Full-text search over one session's turns."""

    def transcript_window(
        self, *, session_id: str, limit: int = 200, before_seq: int = 0
    ) -> "tuple[list[ConversationTurn], list[AgentRuntimeEvent], bool]":
        """A page of the session's timeline, plus whether more exists.

        ``limit`` counts **turns**, not rows and not log positions: a window that
        pages by position hands a client a hundred tool events and no reply.
        """

    def message_has_answer(self, *, session_id: str, message_id: str) -> bool:
        """Whether this user message already has an assistant answer on disk."""

    # ── working state, checkpoints, usage ───────────────────────────────────
    def load_session_working_state(
        self, session_id: str
    ) -> "Optional[SessionWorkingState]":
        """The durable working state for one session."""

    def save_session_working_state(
        self, session_id: str, state: "dict[str, Any]", *, updated_at: "Optional[str]" = None
    ) -> "SessionWorkingState":
        """Replace it, and return what was stored."""

    def save_provider_checkpoint(
        self, session_id: str, messages: "list[dict[str, Any]]"
    ) -> None:
        """Remember the provider conversation so a restart can resume it."""

    def load_provider_checkpoint(self, session_id: str) -> "Optional[dict[str, Any]]":
        """What ``save_provider_checkpoint`` stored, if anything."""

    def usage_summary(self, session_id: str) -> "dict[str, Any]":
        """Token and cost totals for one session."""

    # ── runtime events ──────────────────────────────────────────────────────
    def append_agent_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: "Optional[dict[str, Any]]" = None,
        turn_id: str = "",
        created_at: "Optional[str]" = None,
    ) -> "AgentRuntimeEvent":
        """Append one durable runtime fact."""

    def recent_agent_events(
        self, *, session_id: str, limit: int = 20
    ) -> "list[AgentRuntimeEvent]":
        """The newest runtime events, oldest first."""

    # ── facts ───────────────────────────────────────────────────────────────
    def add_fact_assertion(self, assertion: "FactAssertion") -> "FactAssertion":
        """Append a normalized claim and re-resolve its fact key."""

    def read_resolved_facts(
        self,
        *,
        subject: "Optional[str]" = None,
        predicate: "Optional[str]" = None,
        scope: "Optional[str]" = None,
    ) -> "list[ResolvedFact]":
        """The current best belief per fact key, filtered."""

    def has_conflicted_fact(self, subject: str, predicate: str, scope: str = "global") -> bool:
        """Whether a fact key has competing active assertions."""

    # ── maintenance ─────────────────────────────────────────────────────────
    def maintenance_snapshot(self, limit: int = 20) -> str:
        """A human-readable summary for diagnostics."""

    def close(self) -> None:
        """Release the store.  Must be safe to call twice."""


@runtime_checkable
class ContextService(Protocol):
    """Per-session memory behaviour: recording, retrieval, compaction, jobs.

    This is the interface the application uses, and the one a replacement memory
    *system* satisfies when it does not want to keep this project's
    ``ContextManager``.  ``store`` is deliberately absent from the protocol even
    though the class has it: it is an implementation detail, and the two modules
    that reach through it (``core/spawn_planning``, ``core/subagent_finalize``)
    are the exception the contract test records.
    """

    @property
    def session_id(self) -> str:
        """The session this service journals (read by both channels and the two
        sub-agent writers, which used to reach into ``staging`` for it)."""

    def stage_turn(self, role: str, content: str) -> None:
        """Stage one turn for later consolidation (the turn loop and Ralph)."""

    def has_enough_staged(self) -> bool:
        """Whether enough is staged to be worth waking a background worker."""

    def remember(self, entry: "LTMEntry") -> None:
        """Store one durable entry, for writers that know what they learned."""

    def has_answer(self, *, session_id: str, message_id: str) -> bool:
        """Whether a message already has an assistant answer on record.

        Soft by design, the same way ``MemoryStore.message_has_answer`` is: an
        implementation without the underlying query answers ``False``.
        """

    def recover_orphan_staging(self) -> int:
        """Queue consolidation for turns stranded by sessions that never finished.

        Returns how many interrupted sessions were queued.
        """

    def close(self) -> None:
        """Release what this service owns.  Safe to call twice."""

    def spawn_session(
        self, session_id: "Optional[str]" = None, *, project_scope: str = ""
    ) -> "ContextService":
        """A session-scoped service sharing durable memory, not staging or timers.

        Channel transports multiplex many chats through one process, so this is how
        one chat gets its own history and idle policy.  Three callers depend on it
        (both channels, the CLI's ``cli`` session, a scheduled run's
        ``scheduler:<id>``) and all three bind it with ``getattr`` first, which is
        why the method scan did not see it: the name they call is a local one.
        """

    def mark_activity(self) -> None:
        """Note that the session did something, for idle/sleep accounting."""

    def on_memory_cleared(self) -> None:
        """Fold a memory clear into the current turn's bookkeeping."""

    def record_turn(
        self,
        *,
        user_content: str,
        assistant_content: str = "",
        channel: str = "",
        message_id: str = "",
        assistant_message_id: str = "",
        reply_to_id: str = "",
        metadata: "Optional[dict[str, Any]]" = None,
    ) -> bool:
        """Record a finished turn.  False when it was already recorded."""

    def begin_turn(
        self,
        *,
        user_content: str,
        channel: str = "",
        message_id: str = "",
        reply_to_id: str = "",
        metadata: "Optional[dict[str, Any]]" = None,
    ) -> bool:
        """Record a turn as it starts.  False when the user row already existed."""

    def retrieve_context(
        self,
        query: "str | list[str]",
        top_k: int = 10,
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Long-term memory relevant to *query*, rendered for the prompt."""

    def retrieve_implicit_context(
        self,
        query: str,
        top_k: int = 10,
        current_messages: "Optional[list[dict]]" = None,
        current_turn_id: str = "",
        token_budget: "Optional[int]" = None,
        include_recent_session: bool = True,
        include_working_state: bool = True,
    ) -> str:
        """Everything implicitly relevant, rendered for the prompt."""

    def should_session_end_sleep(self) -> bool:
        """Whether the session should consolidate before it ends."""

    def should_enqueue_consolidation(self) -> bool:
        """Whether enough has been staged to consolidate."""

    def enqueue_consolidation(self, reason: str) -> None:
        """Queue a consolidation job."""

    def pending_jobs(self) -> int:
        """How many consolidation jobs are queued."""

    def should_compact_messages(
        self, messages: "list[dict]", input_token_budget: int
    ) -> bool:
        """Whether the request is over budget and must be compacted."""

    def compact_messages(
        self,
        messages: "list[dict]",
        *,
        input_token_budget: int,
        protected: "Optional[Sequence[dict]]" = None,
    ) -> "list[dict]":
        """Compact the conversation to fit the budget."""

    def fit_to_budget(
        messages: "list[dict]",
        *,
        input_token_budget: int,
        estimate_tokens: "Callable[[list[dict]], int]",
        protected: "Optional[Sequence[dict]]" = None,
    ) -> "list[dict]":
        """The same, without compacting: drop or clip until it fits (a classmethod)."""

    async def sleep(
        self, messages: "list[dict]", endpoint: "ModelEndpoint", model: str
    ) -> "list[dict]":
        """End-of-session consolidation.  Returns the messages to keep."""

    async def process_one_job(
        self,
        endpoint: "ModelEndpoint",
        model: str,
        extractor: "Optional[Callable[..., list[Any]]]" = None,
    ) -> bool:
        """Run one queued consolidation job.  False when the queue was empty."""


@runtime_checkable
class MemoryService(Protocol):
    """The memory palace: named chapters, each with named entries."""

    def write(self, chapter: str, name: str, content: str, append: bool = False) -> None:
        """Write one chapter entry."""

    def read(self, chapter: str, name: str) -> str:
        """Read one chapter entry, or ""."""

    def search(self, query: str) -> "list[dict]":
        """Search every chapter."""

    def read_index(self) -> str:
        """The chapter index, rendered."""

    def set_identity(
        self,
        *,
        subject: str = "assistant",
        name: "Optional[str]" = None,
        role: "Optional[str]" = None,
        persona: "Optional[str]" = None,
    ) -> "dict[str, str]":
        """Record who someone is, in the identity chapter."""

    def clear(self) -> "dict[str, int]":
        """Remove every chapter.  Returns how much went."""

    def force_tidy(self) -> None:
        """Tidy the chapters now."""

    async def tidy(self) -> None:
        """Tidy while nobody is waiting."""
