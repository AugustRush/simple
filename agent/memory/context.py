"""Orchestrates long-term storage, retrieval, and consolidation for a turn."""

from __future__ import annotations

from .jobs import JobQueue
import hashlib
import json
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

from agent import shared
from agent.lexical import lexical_terms

if TYPE_CHECKING:
    from agent.core.transport import ModelEndpoint

from . import jobs
from . import context_telemetry
from . import context_working_state
from . import context_retrieval
from .session_log import SessionLog
from . import fitting
from . import query_terms
from . import working_state
from .working_state import (
    _merge_artifacts,
    _working_state_candidates,
    _working_state_is_complete,
)
from .query_terms import (
    _has_multi_intent_marker,
    _matched_fact_alias_terms,
    _weighted_term_overlap,
)
from . import text as _render
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

#: The events that end a turn, and so are the only ones whose ``error``
#: describes how the *task* stands.  ``turn_finished`` carries the successful
#: ending (and the assistant's content) as well as a cancellation, so leaving it
#: out would lose 「已取消」; everything else that happens to carry an ``error``
#: key is talking about itself, not about the task.


def enqueue_orphan_staging_recovery(ctx_mgr: Any) -> int:
    """Queue consolidation for turns stranded by sessions that never finished.

    A process that dies between staging a turn and consolidating it leaves the
    turn behind.  Nothing else ever collects it: staged rows are only deleted
    by a successful consolidation or by an explicit ``/forget``, so without a
    sweep at startup the turn is both invisible to memory and never reclaimed.

    Both backends are scanned, because the interesting one is not on disk.  The
    default backend is SQLite, where a session's turns are rows in the shared
    ``palace.db`` partitioned by ``session_id`` — a filesystem scan sees
    nothing at all.  The JSONL sweep stays for homes written by older versions.

    Returns the number of interrupted sessions queued, for the caller to report.
    """
    enqueue = getattr(ctx_mgr, "enqueue_staging_job", None)
    staging = getattr(ctx_mgr, "staging", None)
    if not callable(enqueue) or staging is None:
        return 0
    current_sid = str(getattr(staging, "session_id", "") or "")
    orphans: list[StagingBuffer] = []

    # Only a SQLite-backed buffer names a database to scan.  A JSONL buffer
    # (legacy homes, focused tests) has no shared partition to recover from,
    # and falling back to the global context dir here would make this sweep
    # reach into a home this manager was never pointed at.
    context_dir = getattr(staging, "context_dir", None)
    if context_dir is not None and getattr(staging, "_sqlite_backed", False):
        for session_id, _count in StagingBuffer.discover_sqlite_sessions(context_dir):
            if session_id != current_sid:
                orphans.append(
                    StagingBuffer(context_dir=context_dir, session_id=session_id)
                )

    try:
        legacy_paths = sorted(shared.STAGING_DIR.glob("*.jsonl"))
    except OSError:
        legacy_paths = []
    for path in legacy_paths:
        try:
            has_content = path.stat().st_size > 0
        except OSError:
            continue
        if path.stem != current_sid and has_content:
            orphans.append(StagingBuffer(path=path, session_id=path.stem))

    for orphan in orphans:
        enqueue("orphan_recovery", orphan)
        # The queued job records the backend, context dir and session id, and
        # ``_job_staging`` rebuilds a buffer from those when the worker picks
        # the job up.  Holding these open until then would keep one SQLite
        # connection per interrupted session alive for the whole run.
        with shared._suppress_with_log("orphan staging close failed"):
            orphan.close()
    return len(orphans)


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
        project_scope: str = "",
    ):
        self.store = store
        self.retriever = retriever
        self.consolidation = consolidation
        self.staging: StagingBuffer = staging or StagingBuffer()
        self.project_scope = str(project_scope or "").strip()
        source_keywords = route_keywords or shared.DEFAULT_ROUTE_KEYWORDS
        self.route_keywords = {
            category: tuple(str(keyword).lower() for keyword in keywords)
            for category, keywords in source_keywords.items()
        }
        # The queue's state and the logic that reads it live in one object; the services it
        # needs are handed over here because none of them is reassigned after this point.
        self._queue = JobQueue(
            staging=self.staging,
            consolidation=self.consolidation,
            store=self.store,
            project_scope=self.project_scope,
            # the constructor's parameters, not the attributes: those are properties that
            # would read the queue being built here
            idle_seconds=idle_seconds,
            min_messages=min_messages,
            staging_turn_threshold=staging_turn_threshold,
            staging_token_threshold=staging_token_threshold,
        )
        self.last_working_state_recovery_trace: dict[str, Any] = {}

    # Two names stay addressable on the manager itself: tests set and read
    # `_needs_consolidation` (eight places), and one comment reasons about `_jobs`.  They
    # forward to the queue rather than shadowing it, so there is one copy of each.
    # The four numbers the queue logic compares against are forwarded too, for the same
    # reason: they are assigned after construction in the tests (`ctx_mgr.idle_seconds = 0`,
    # `ctx_mgr.staging_token_threshold = 999999`), so the queue has to see the new value
    # rather than the one it was built with.
    @property
    def idle_seconds(self) -> float:
        return self._queue.idle_seconds

    @idle_seconds.setter
    def idle_seconds(self, value: float) -> None:
        self._queue.idle_seconds = value

    @property
    def min_messages(self) -> int:
        return self._queue.min_messages

    @min_messages.setter
    def min_messages(self, value: int) -> None:
        self._queue.min_messages = value

    @property
    def staging_turn_threshold(self) -> int:
        return self._queue.staging_turn_threshold

    @staging_turn_threshold.setter
    def staging_turn_threshold(self, value: int) -> None:
        self._queue.staging_turn_threshold = value

    @property
    def staging_token_threshold(self) -> int:
        return self._queue.staging_token_threshold

    @staging_token_threshold.setter
    def staging_token_threshold(self, value: int) -> None:
        self._queue.staging_token_threshold = value

    @property
    def _needs_consolidation(self) -> bool:
        return self._queue.needs_consolidation

    @_needs_consolidation.setter
    def _needs_consolidation(self, value: bool) -> None:
        self._queue.needs_consolidation = value

    @property
    def _jobs(self):
        return self._queue.jobs

    @_jobs.setter
    def _jobs(self, value) -> None:
        self._queue.jobs = value

    @property
    def session_id(self) -> str:
        """The session this manager journals.

        A contract member because four callers needed it and used to reach for
        ``staging.session_id``: the two sub-agent writers (which stamp the session
        on what they store) and both channels (which register and log by session).
        """
        return str(getattr(self.staging, "session_id", "") or "")

    def stage_turn(self, role: str, content: str) -> None:
        """Stage one turn for later consolidation.

        The *act* the turn loop and Ralph need.  ``StagingBuffer`` is how this
        implementation keeps staged turns; naming the act rather than the buffer is
        what lets a replacement keep them wherever it likes.
        """
        self.staging.append(role, content)

    def has_enough_staged(self) -> bool:
        """Whether enough is staged that waking a background worker is worth it.

        The threshold (``min_messages``) is this implementation's policy; the caller
        used to compare against the attribute, which made the policy part of the
        interface.
        """
        return self.staging.count() >= self.min_messages

    def remember(self, entry: LTMEntry) -> None:
        """Store one durable entry.

        For the writers that know *what* they learned and not how it is kept: the
        spawn planner's promoted summaries and the sub-agent finale.  Retrieval and
        consolidation stay behind ``retrieve_context`` and ``process_one_job``.
        """
        self.store.add_entry(entry)

    def has_answer(self, *, session_id: str, message_id: str) -> bool:
        """Whether this session already has an assistant answer for a message.

        The store's ``message_has_answer`` is deliberately optional (see
        ``contracts.py``), so the feature-detection lives here rather than in the
        runtime layer, which used to reach ``context_manager.store`` by name to
        ask.  A store without it answers "no", which is the same behaviour as
        before -- but now the dependency is a declared member.
        """
        check = getattr(self.store, "message_has_answer", None)
        if not callable(check) or not message_id:
            return False
        return bool(check(session_id=str(session_id or ""), message_id=str(message_id)))

    def recover_orphan_staging(self) -> int:
        """Queue consolidation for turns stranded by sessions that never finished."""
        return enqueue_orphan_staging_recovery(self)

    def close(self) -> None:
        """Release what this manager owns.  Safe to call twice.

        Replaces two call sites that closed its *parts* (``staging.close()`` then
        ``store.close()``), which is knowledge of this implementation's internals.
        """
        close_staging = getattr(self.staging, "close", None)
        if callable(close_staging):
            close_staging()
        close_store = getattr(self.store, "close", None)
        if callable(close_store):
            close_store()

    def on_memory_cleared(self) -> None:
        """Drop queued extraction work and keep the clearing turn forgotten."""
        jobs.on_memory_cleared(queue=self._queue)


    def consume_memory_clear_suppression(self) -> bool:
        return jobs.consume_memory_clear_suppression(queue=self._queue)



    def spawn_session(
        self,
        session_id: Optional[str] = None,
        *,
        project_scope: str = "",
    ) -> "ContextManager":
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
            project_scope=project_scope or self.project_scope,
        )

    # ── Activity tracking ─────────────────────────────────────────────────────

    @classmethod
    def fit_to_budget(
        cls,
        messages: list[dict],
        *,
        input_token_budget: int,
        estimate_tokens: Callable[[list[dict]], int],
        protected: Optional[Sequence[dict]] = None,
    ) -> list[dict]:
        """Fit a provider payload into ``input_token_budget``, dropping oldest first.

        The implementation is :func:`agent.memory.fitting.fit_to_budget`: fitting a request
        into the provider's window is a property of the request, and the class kept the name
        for the protocol.  See that module for why the rest of the cluster went with it.
        """
        return fitting.fit_to_budget(
            messages,
            input_token_budget=input_token_budget,
            estimate_tokens=estimate_tokens,
            protected=protected,
        )

    def mark_activity(self) -> None:
        """Call after each user message to arm consolidation and reset idle timer."""
        jobs.mark_activity(queue=self._queue)



    def _plan_query(self, query: str) -> QueryPlan:
        return context_retrieval._plan_query(
            query=query, route_keywords=self.route_keywords
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
        interjections: Optional[Sequence[dict[str, Any]]] = None,
    ) -> ConversationWriteResult:
        """Persist an exchange and expose which event rows were created."""
        return context_telemetry.record_turn_result(
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
            interjections=interjections, store=self.store, staging=self.staging
        )


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
        return jobs.idle_elapsed(queue=self._queue)


    # ── Trigger checks ────────────────────────────────────────────────────────

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        """Token-ratio trigger: only fires when dirty and messages are sufficient."""
        return jobs.should_sleep(
            messages=messages,
            max_tokens=max_tokens, queue=self._queue
        )


    def should_idle_sleep(self, messages: list[dict]) -> bool:
        """Idle trigger: fires when dirty, sufficient messages, and idle long enough."""
        return jobs.should_idle_sleep(
            messages=messages, queue=self._queue
        )


    def should_session_end_sleep(self) -> bool:
        """Session-end trigger: fires when staging has at least one complete turn."""
        return jobs.should_session_end_sleep(queue=self._queue)


    def should_enqueue_consolidation(self) -> bool:
        """Queue consolidation based on staged content volume, not working-memory size."""
        return jobs.should_enqueue_consolidation(queue=self._queue)


    def enqueue_consolidation(self, reason: str) -> None:
        """Queue one consolidation job if there is staged work pending."""
        self.enqueue_staging_job(reason, self.staging)

    def enqueue_staging_job(self, reason: str, staging: "StagingBuffer") -> None:
        """Queue consolidation for an explicit staging buffer."""
        jobs.enqueue_staging_job(
            reason=reason,
            staging=staging, queue=self._queue
        )


    def next_job(self, pop: bool = False) -> Optional[dict]:
        return jobs.next_job(
            pop=pop, queue=self._queue
        )


    def pending_jobs(self) -> int:
        return jobs.pending_jobs(queue=self._queue)


    def _job_staging(self, job: dict) -> tuple['StagingBuffer', bool]:
        return jobs._job_staging(
            job=job, queue=self._queue
        )


    def should_process_jobs(self) -> bool:
        return jobs.should_process_jobs(queue=self._queue)


    def should_compact_messages(
        self, messages: list[dict], input_token_budget: int
    ) -> bool:
        """Return whether provider-bound messages violate the strict budget."""
        return self.consolidation.estimate_tokens(messages) >= int(
            input_token_budget
        )







    # Identified by a text sentinel rather than a custom message key: both
    # Anthropic and OpenAI reject unknown fields on a message object.

    #: How far below the input budget a compaction cuts once it has been
    #: triggered.  Cutting to the brim would put the very next step over the
    #: line again, and every cut rewrites the front of the body — which is
    #: exactly what a provider's prefix cache reuses — so the remaining
    #: conversation is re-billed at full rate each time.  A deep, rare cut costs
    #: one re-bill and buys many append-only turns; a shallow, frequent one pays
    #: that price on nearly every request.  See `fit_to_budget`.



    def compact_messages(
        self,
        messages: list[dict],
        *,
        input_token_budget: int,
        protected: Optional[Sequence[dict]] = None,
    ) -> list[dict]:
        """Drop the oldest turns until the payload fits ``input_token_budget``.

        ``protected`` names the message objects this cut must keep.  A caller
        inside a turn passes the request it appended; see
        :meth:`_protected_indexes` for what happens when it does not.
        """
        return self.fit_to_budget(
            messages,
            input_token_budget=input_token_budget,
            estimate_tokens=self.consolidation.estimate_tokens,
            protected=protected,
        )




    # ── Retrieval ─────────────────────────────────────────────────────────────

    def _is_episode_recall_query(self, query: str) -> bool:
        return context_retrieval._is_episode_recall_query(
            query=query, route_keywords=self.route_keywords
        )


    def _route_categories(self, query: str) -> list[str]:
        return context_retrieval._route_categories(
            query=query, route_keywords=self.route_keywords
        )



    def _targeted_fact_conflict(self, plan: QueryPlan) -> bool:
        return context_retrieval._targeted_fact_conflict(
            plan=plan, store=self.store
        )



    def _should_include_freeform_context(
        self,
        *,
        query: str,
        plan: QueryPlan,
        has_fact_hits: bool,
    ) -> bool:
        return context_retrieval._should_include_freeform_context(
            query=query,
            plan=plan,
            has_fact_hits=has_fact_hits, store=self.store
        )


    def _resolved_fact_candidates(
        self,
        query: str,
        *,
        plan: Optional[QueryPlan] = None,
        top_k: int = shared.RETRIEVAL_TOP_K,
    ) -> list[ResolvedFact]:
        return context_retrieval._resolved_fact_candidates(
            query=query,
            plan=plan,
            top_k=top_k, store=self.store, route_keywords=self.route_keywords
        )


    def retrieve_resolved_fact_context(
        self,
        query: str,
        *,
        top_k: int = shared.RETRIEVAL_TOP_K,
        plan: Optional[QueryPlan] = None,
        title: str = "## Resolved Facts",
    ) -> str:
        return context_retrieval.retrieve_resolved_fact_context(
            query=query,
            top_k=top_k,
            plan=plan,
            title=title, store=self.store, route_keywords=self.route_keywords
        )


    def ltm_candidates(
        self,
        query: str,
        top_k: int = shared.RETRIEVAL_TOP_K,
    ) -> list[LTMEntry]:
        """Stage 1: the candidate set the re-ranker is allowed to choose from."""
        return context_retrieval.ltm_candidates(
            query=query,
            top_k=top_k, store=self.store, route_keywords=self.route_keywords, staging=self.staging, project_scope=self.project_scope
        )


    def rank_ltm_entries(
        self,
        query: str | list[str],
        top_k: int = shared.RETRIEVAL_TOP_K,
    ) -> list[LTMEntry]:
        """Return the top-K entries for *query*, highest-scoring first."""
        return context_retrieval.rank_ltm_entries(
            query=query,
            top_k=top_k, store=self.store, route_keywords=self.route_keywords, staging=self.staging, retriever=self.retriever, project_scope=self.project_scope
        )


    def retrieve_ltm_context(
        self,
        query: str | list[str],
        top_k: int = shared.RETRIEVAL_TOP_K,
        token_budget: Optional[int] = None,
    ) -> str:
        """Format the top-K relevant LTM entries as an injectable string."""
        return context_retrieval.retrieve_ltm_context(
            query=query,
            top_k=top_k,
            token_budget=token_budget, store=self.store, route_keywords=self.route_keywords, staging=self.staging, retriever=self.retriever, consolidation=self.consolidation, project_scope=self.project_scope
        )


    def _recent_session_context(
        self,
        limit: int = shared.RECENT_SESSION_TURNS,
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Return the most recent staged turns for explicit current-session recall."""
        return context_retrieval._recent_session_context(
            limit=limit,
            exclude_message_id=exclude_message_id, store=self.store, staging=self.staging
        )


    def retrieve_history_context(
        self,
        query: str,
        limit: int = shared.RECENT_SESSION_TURNS,
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Return durable event-history evidence when the query asks for it."""
        return context_retrieval.retrieve_history_context(
            query=query,
            limit=limit,
            exclude_message_id=exclude_message_id, store=self.store, route_keywords=self.route_keywords, staging=self.staging
        )



    def _recent_unconsolidated_context(
        self,
        current_messages: Optional[list[dict]] = None,
        limit: int = shared.RECENT_SESSION_TURNS,
    ) -> str:
        return context_retrieval._recent_unconsolidated_context(
            current_messages=current_messages,
            limit=limit, staging=self.staging
        )


    def _assistant_identity_context(self, limit: int = 3) -> str:
        return context_retrieval._assistant_identity_context(
            limit=limit, store=self.store
        )




    def _working_state_relevance_score(self, state: dict[str, Any], query: str) -> float:
        return context_working_state._working_state_relevance_score(
            state=state,
            query=query,
        )




    def _select_working_state(
        self,
        state: dict[str, Any],
        query: str,
        *,
        include_completed: bool = False,
    ) -> dict[str, Any] | None:
        return context_working_state._select_working_state(
            state=state,
            query=query,
            include_completed=include_completed,
        )


    def _working_state_recovery_decision(
        self,
        state: dict[str, Any],
        query: str,
        *,
        include_completed: bool = False,
    ) -> dict[str, Any]:
        return context_working_state._working_state_recovery_decision(
            state=state,
            query=query,
            include_completed=include_completed,
        )


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
        text = _render._format_working_state_text(selected_state)
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
                detail = _render._clip_working_state_text(detail, 160)
                event_lines.append(f"- {event.event_type}: {detail}")
            else:
                event_lines.append(f"- {event.event_type}")
        if text and event_lines:
            text += "\n- Recent runtime events:\n" + "\n".join(
                f"  {line}" for line in event_lines
            )
        return text

    def has_active_working_state(self) -> bool:
        """Whether durable state still describes an unfinished task."""
        return context_working_state.has_active_working_state(store=self.store, staging=self.staging)



    def save_provider_checkpoint(
        self,
        messages: list[dict],
        *,
        token_budget: int = 24000,
    ) -> dict[str, Any]:
        """Persist a bounded, structurally complete provider replay tail."""

        repaired = fitting._repair_tool_history(list(messages))
        compacted = repaired
        if repaired and self.consolidation.estimate_tokens(repaired) >= token_budget:
            compacted = self.compact_messages(
                repaired,
                input_token_budget=max(512, int(token_budget)),
            )
        # compact_messages() may replace a dropped message with an eviction
        # notice, so comparing list lengths cannot reliably detect loss.  Match
        # canonical message values as a multiset instead; this also handles
        # repeated, identical messages without relying on object identity.
        retained_counts: dict[str, int] = {}
        for item in compacted:
            content = item.get("content")
            if (
                item.get("role") == "user"
                and isinstance(content, str)
                and content.startswith(fitting._EVICTION_SENTINEL)
            ):
                continue
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            retained_counts[key] = retained_counts.get(key, 0) + 1
        dropped: list[dict] = []
        for item in repaired:
            content = item.get("content")
            if (
                item.get("role") == "user"
                and isinstance(content, str)
                and content.startswith(fitting._EVICTION_SENTINEL)
            ):
                continue
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if retained_counts.get(key, 0) > 0:
                retained_counts[key] -= 1
            else:
                dropped.append(item)

        existing = self.load_provider_checkpoint() or {}
        previous_summary = str(existing.get("summary") or "").strip()
        dropped_summary = _render._checkpoint_summary(dropped).strip()
        summary = "\n".join(
            part for part in (previous_summary, dropped_summary) if part
        )
        if len(summary) > 4000:
            summary = summary[:2000].rstrip() + "\n...\n" + summary[-1995:].lstrip()
        save = getattr(self.store, "save_provider_checkpoint", None)
        if callable(save):
            save(self.staging.session_id, compacted, summary=summary)
        return {"messages": compacted, "summary": summary}

    def load_provider_checkpoint(self) -> dict[str, Any] | None:
        return context_telemetry.load_provider_checkpoint(store=self.store, staging=self.staging)




    @property
    def session_log(self) -> Any:
        """This session's durable log.

        Built once and cached, and it does **not** need rebuilding when the session is
        rebound: it reads the id through a callable rather than capturing it, so it always
        files under the session in force.  Capturing the id is what produced events
        attributed to an unrelated session -- the bug the channel used to work around by
        hand.

        (The first version of this property compared the log's id against the staging id to
        decide whether to rebuild.  That comparison is always false, because both sides read
        the same callable -- a condition that can never be true, guarding nothing, which is
        worse than no condition at all.)
        """
        log = getattr(self, "_session_log", None)
        if log is None:
            log = SessionLog(
                self.store,
                lambda: self.staging.session_id,
                project=self._project_working_state_from_event,
            )
            self._session_log = log
        return log

    def _project_working_state_from_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> SessionWorkingState:
        return context_working_state._project_working_state_from_event(
            event_type=event_type,
            payload=payload,
            created_at=created_at, store=self.store, staging=self.staging
        )


    def record_runtime_event(
        self,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        turn_id: str = "",
    ) -> AgentRuntimeEvent:
        """Record a runtime fact and update the model-readable projection.

        Delegates to :attr:`session_log`.  It used to do the work itself, which meant two
        places could write durable history -- and the other one, a channel, had to find the
        right context manager by a lookup chain with a known-wrong fallback.

        Kept as a method because callers hold a context manager, not a log; the *path* is
        what was duplicated, and there is now one.
        """
        return self.session_log.append(event_type, payload, turn_id=turn_id)

    def observe_token_usage(
        self,
        *,
        predicted: int,
        actual: int,
        fingerprint: str = "",
        head_estimate: int = 0,
        body_raw: int = 0,
    ) -> None:
        """Feed one call's usage back to the engine that calibrates the estimator."""
        context_telemetry.observe_token_usage(
            predicted=predicted,
            actual=actual,
            fingerprint=fingerprint,
            head_estimate=head_estimate,
            body_raw=body_raw, consolidation=self.consolidation
        )


    def record_usage_event(
        self,
        *,
        session_id: str,
        phase: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        turn_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist one provider-usage row through the store, when it keeps them."""
        context_telemetry.record_usage_event(
            session_id=session_id,
            phase=phase,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            turn_id=turn_id,
            metadata=metadata, store=self.store
        )


    def retrieve_context(
        self,
        query: str | list[str],
        top_k: int = shared.RETRIEVAL_TOP_K,
        *,
        exclude_message_id: str = "",
    ) -> str:
        """Return explicit context lookup results across active session and LTM."""
        return context_retrieval.retrieve_context(
            query=query,
            top_k=top_k,
            exclude_message_id=exclude_message_id, store=self.store, route_keywords=self.route_keywords, staging=self.staging, retriever=self.retriever, consolidation=self.consolidation, project_scope=self.project_scope
        )


    def retrieve_implicit_context(
        self,
        query: str,
        top_k: int = shared.RETRIEVAL_TOP_K,
        current_messages: Optional[list[dict]] = None,
        current_turn_id: str = "",
        token_budget: Optional[int] = None,
        include_recent_session: bool = True,
        include_working_state: bool = True,
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
        if include_working_state:
            working_state = self.working_state_context(query)
            if working_state:
                sections.append(working_state)
        if include_recent_session and "episodes" in self._route_categories(query):
            recent = self._recent_session_context(
                exclude_message_id=current_turn_id,
            )
            if recent:
                sections.append(recent)
        elif include_recent_session:
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
        """Join sections, dropping whole ones that exceed the budget."""
        return context_retrieval._fit_sections_to_budget(
            sections=sections,
            token_budget=token_budget, consolidation=self.consolidation
        )


    # ── Consolidation ─────────────────────────────────────────────────────────

    async def sleep(
        self,
        messages: list[dict],
        endpoint: "ModelEndpoint",
        model: str,
    ) -> list[dict]:
        """Run one sleep cycle (uses staging as source), then clear dirty flag."""
        return await jobs.sleep(
            messages=messages,
            endpoint=endpoint,
            model=model, queue=self._queue
        )


    async def process_one_job(
        self,
        endpoint: "ModelEndpoint",
        model: str,
        extractor: Optional[Callable[..., list[Any]]] = None,
    ) -> bool:
        """Process one queued consolidation job without mutating working memory."""
        return await jobs.process_one_job(
            endpoint=endpoint,
            model=model,
            extractor=extractor, queue=self._queue
        )


    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return jobs.stats(queue=self._queue)

