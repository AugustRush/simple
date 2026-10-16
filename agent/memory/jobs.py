"""The session's background job queue.

What used to be six loose attributes on ``ContextManager`` -- the pending jobs, the lock that
guards them, the "a job is being processed" flag, the "a consolidation is owed" flag, the
idle clock and the memory-clear suppression -- together with the logic that reads them.  It
is one object because it is one thing: the queue and the clocks the background worker polls.

``ContextManager`` keeps methods of the same names as thin delegates, so a caller that
monkeypatches the instance (`worker.py` calls ``ctx_mgr.process_one_job``) keeps intercepting,
and it keeps ``_needs_consolidation`` and ``_jobs`` as properties, because tests read and
write them.

The services the queue logic needs (`staging`, `consolidation`, `store`, `project_scope`) and
the thresholds it compares against are handed over once, at construction.  None of them is
reassigned after ``ContextManager.__init__``, which is what makes holding the reference safe
rather than merely convenient.
"""

from __future__ import annotations
import time
import json

from pathlib import Path
from agent import shared
from . import text as _render
from ._helpers import _emit_consolidation, _now
from .staging import StagingBuffer
from .models import LTMEntry

import threading
from collections import deque
from typing import Any, Callable, Optional


class JobQueue:
    """The queue, its lock, and the clocks the worker reads."""

    def __init__(
        self,
        *,
        staging: Any,
        consolidation: Any,
        store: Any,
        project_scope: str,
        idle_seconds: float,
        min_messages: int,
        staging_turn_threshold: int,
        staging_token_threshold: int,
    ) -> None:
        self.staging = staging
        self.consolidation = consolidation
        self.store = store
        self.project_scope = project_scope
        self.idle_seconds = idle_seconds
        self.min_messages = min_messages
        self.staging_turn_threshold = staging_turn_threshold
        self.staging_token_threshold = staging_token_threshold

        self.jobs: deque[dict[str, Any]] = deque()
        self.lock = threading.RLock()
        self.processing_job = False
        self.needs_consolidation = False
        self.last_activity = 0.0
        self.suppress_next_turn_persistence = False


def on_memory_cleared(
    *,
    queue,
) -> None:
    """Drop queued extraction work and keep the clearing turn forgotten."""
    with queue.lock:
        queue.jobs.clear()
        queue.needs_consolidation = False
        queue.last_activity = 0.0
        queue.suppress_next_turn_persistence = True
    queue.staging.clear_all()


def consume_memory_clear_suppression(
    *,
    queue,
) -> bool:
    with queue.lock:
        suppress = queue.suppress_next_turn_persistence
        queue.suppress_next_turn_persistence = False
        return suppress


def mark_activity(
    *,
    queue,
) -> None:
    """Call after each user message to arm consolidation and reset idle timer."""
    with queue.lock:
        queue.last_activity = time.time()
        queue.needs_consolidation = True


def idle_elapsed(
    *,
    queue,
) -> float:
    """Seconds since last activity (0 if never active)."""
    with queue.lock:
        if queue.last_activity == 0.0:
            return 0.0
        return time.time() - queue.last_activity


def should_sleep(
    messages: list[dict],
    max_tokens: int,
    *,
    queue,
) -> bool:
    """Token-ratio trigger: only fires when dirty and messages are sufficient."""
    with queue.lock:
        if not queue.needs_consolidation:
            return False
    if len(messages) < queue.min_messages:
        return False
    return queue.consolidation.should_sleep(messages, max_tokens)


def should_idle_sleep(
    messages: list[dict],
    *,
    queue,
) -> bool:
    """Idle trigger: fires when dirty, sufficient messages, and idle long enough."""
    with queue.lock:
        if not queue.needs_consolidation:
            return False
    if len(messages) < queue.min_messages:
        return False
    return idle_elapsed(queue=queue) >= queue.idle_seconds


def should_session_end_sleep(
    *,
    queue,
) -> bool:
    """Session-end trigger: fires when staging has at least one complete turn.

        Requires count >= 2 (one user + one assistant message) to ensure
        there is a complete exchange worth extracting.  count == 1 would mean
        a bare user message with no response was staged, which is not worth
        an LLM extraction call.
        """
    with queue.lock:
        return queue.needs_consolidation and queue.staging.count() >= 2


def should_enqueue_consolidation(
    *,
    queue,
) -> bool:
    """Queue consolidation based on staged content volume, not working-memory size."""
    with queue.lock:
        if not queue.needs_consolidation:
            return False
        count = queue.staging.count()
    # Fast path: count() is an in-memory counter; no file I/O needed.
    if count >= queue.staging_turn_threshold:
        return True
    # Slow path: require at least min_messages staged entries before checking
    # tokens.  Without this guard a single verbose response (common with CJK
    # text, where ~1 char ≈ 1 estimated token) crosses the 2100-token threshold
    # on every turn, causing consolidation to fire every turn even though only
    # two entries have accumulated since the last job ran.
    if count < queue.min_messages:
        return False
    # Only read the file once we know there are enough entries to warrant it.
    staged = queue.staging.read_all()
    if not staged:
        return False
    return (
        queue.consolidation.estimate_tokens(staged) >= queue.staging_token_threshold
    )


def enqueue_staging_job(
    reason: str,
    staging: "StagingBuffer",
    *,
    queue,
) -> None:
    """Queue consolidation for an explicit staging buffer."""
    with queue.lock:
        if staging.count() == 0:
            return
        staging_path = str(staging.path.resolve())
        session_id = staging.session_id
        if any(
            job.get("staging_path", str(queue.staging.path.resolve()))
            == staging_path
            and job.get("session_id", queue.staging.session_id) == session_id
            for job in queue.jobs
        ):
            return
        queue.jobs.append(
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
                    getattr(staging, "context_dir", queue.staging.context_dir).resolve()
                ),
                "queued_at": _now(),
            }
        )


def next_job(
    pop: bool = False,
    *,
    queue,
) -> Optional[dict]:
    with queue.lock:
        if not queue.jobs:
            if queue.needs_consolidation and queue.staging.count() > 0:
                queue.jobs.append(
                    {
                        "reason": "idle",
                        "session_id": queue.staging.session_id,
                        "queued_at": _now(),
                    }
                )
            else:
                return None
        return queue.jobs.popleft() if pop else dict(queue.jobs[0])


def pending_jobs(
    *,
    queue,
) -> int:
    with queue.lock:
        return len(queue.jobs)


def _job_staging(
    job: dict,
    *,
    queue,
) -> tuple["StagingBuffer", bool]:
    backend = str(job.get("staging_backend", "jsonl"))
    path_value = job.get("staging_path")
    session_id = str(job.get("session_id", queue.staging.session_id))
    if backend == "sqlite":
        context_dir = Path(
            job.get("context_dir", str(queue.staging.context_dir))
        ).resolve()
        if (
            context_dir == queue.staging.context_dir.resolve()
            and session_id == queue.staging.session_id
            and getattr(queue.staging, "_sqlite_backed", False)
        ):
            return queue.staging, True
        return StagingBuffer(context_dir=context_dir, session_id=session_id), False
    if not path_value:
        return queue.staging, True
    path = Path(path_value).resolve()
    if (
        path == queue.staging.path.resolve()
        and session_id == queue.staging.session_id
    ):
        return queue.staging, True
    return StagingBuffer(path=path, session_id=session_id), False


def should_process_jobs(
    *,
    queue,
) -> bool:
    with queue.lock:
        has_pending = bool(queue.jobs)
        # Require enough staged entries to be worth an LLM extraction call.
        # count > 0 is not sufficient: the idle path would fire after any pause
        # of idle_seconds even with a single staged turn.  Using
        # staging_turn_threshold makes the implicit idle enqueue consistent
        # with the explicit fast-path in should_enqueue_consolidation(queue=queue).
        has_staged_work = (
            queue.needs_consolidation
            and queue.staging.count() >= queue.staging_turn_threshold
        )
    return (
        has_pending or has_staged_work
    ) and idle_elapsed(queue=queue) >= queue.idle_seconds


async def sleep(
    messages: list[dict],
    endpoint: "ModelEndpoint",
    model: str,
    *,
    queue,
) -> list[dict]:
    """Run one sleep cycle (uses staging as source), then clear dirty flag."""
    try:
        result = await queue.consolidation.consolidate(
            messages,
            endpoint,
            model,
            staging=queue.staging,
            project_scope=queue.project_scope,
        )
        return _render._coerce_consolidation_result(result).compressed_messages
    finally:
        with queue.lock:
            queue.needs_consolidation = False


async def process_one_job(
    endpoint: "ModelEndpoint",
    model: str,
    extractor: Optional[Callable[..., list[Any]]] = None,
    *,
    queue,
) -> bool:
    """Process one queued consolidation job without mutating working memory."""
    with queue.lock:
        if queue.processing_job:
            return False
        queue.processing_job = True
    job = next_job(pop=True, queue=queue)
    if job is None:
        with queue.lock:
            queue.processing_job = False
        return False

    staging_buffer: Optional[StagingBuffer] = None
    is_primary_staging = True
    try:
        staging_buffer, is_primary_staging = _job_staging(job, queue=queue)
        reason = str(job.get("reason", "?"))
        session_id = staging_buffer.session_id
        shared.CONSOLE.print(
            f"[dim]💤 Context consolidation (sleep)... reason={reason} "
                f"session={session_id}[/dim]"
        )
        with queue.lock:
            staged = staging_buffer.read_all()
        if not staged:
            if is_primary_staging:
                with queue.lock:
                    queue.needs_consolidation = False
            return False

        # Emit consolidation lifecycle event
        _emit_consolidation("started", reason=reason, staged_count=len(staged))

        if extractor is not None:
            entries = [
                queue.consolidation._build_episode_entry(
                    staged, staging_buffer.session_id
                )
            ]
            extracted = extractor(staged, job)
            for item in extracted or []:
                if isinstance(item, LTMEntry):
                    if not item.scope or item.scope == "global":
                        item.scope = queue.consolidation._scope_for_entry(
                            item.category,
                            session_id=staging_buffer.session_id,
                            project_scope=queue.project_scope,
                        )
                        item.source_session = staging_buffer.session_id
                    entries.append(item)
                elif isinstance(item, dict):
                    lines = json.dumps(item, ensure_ascii=False)
                    parsed = queue.consolidation._parse_entries(lines)
                    for entry in parsed:
                        entry.scope = queue.consolidation._scope_for_entry(
                            entry.category,
                            session_id=staging_buffer.session_id,
                            project_scope=queue.project_scope,
                        )
                        entry.source_session = staging_buffer.session_id
                    entries.extend(parsed)
            queue.store.add_entries(entries)
            queue.store.apply_retention()
            staging_buffer.drop_prefix(len(staged))
            if is_primary_staging:
                with queue.lock:
                    queue.needs_consolidation = False
            _emit_consolidation("completed", entries_extracted=len(entries))
            return True

        try:
            result = await queue.consolidation.consolidate(
                [],
                endpoint,
                model,
                staging=staging_buffer,
                project_scope=queue.project_scope,
            )
        except Exception as exc:
            _emit_consolidation("failed", reason="llm_extraction_error", error=str(exc))
            shared.CONSOLE.print(f"[dim]Sleep extraction error: {exc}[/dim]")
            return False
        consolidated = _render._coerce_consolidation_result(result)
        if not consolidated.success:
            _emit_consolidation(
                "failed", reason="extraction_returned_failure", error=consolidated.error
            )
            return False
        if is_primary_staging:
            with queue.lock:
                queue.needs_consolidation = False
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
        with queue.lock:
            queue.processing_job = False


def stats(
    *,
    queue,
) -> dict:
    cats = queue.store.list_categories()
    return {
        "dynamic_categories": queue.store.dynamic_category_count(),
        "total_categories": len(cats),
        "total_entries": sum(c.entry_count for c in cats),
        "category_names": [c.name for c in cats],
        "max_categories": queue.store.max_categories,
        "needs_consolidation": queue.needs_consolidation,
        "queued_jobs": pending_jobs(queue=queue),
        "staged_turns": queue.staging.count(),
        "idle_elapsed_s": round(idle_elapsed(queue=queue)),
        "idle_threshold_s": queue.idle_seconds,
    }
