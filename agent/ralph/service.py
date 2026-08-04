from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .models import (
    RalphIterationResult,
    RalphTask,
    RalphTaskStatus,
    RalphValidationError,
    VerificationResult,
    VerificationStatus,
)

logger = logging.getLogger(__name__)

RALPH_SUMMARY_LIMIT = 2_000
RALPH_DIAGNOSTIC_LIMIT = 2_000

TurnExecutor = Callable[..., Awaitable[RalphIterationResult]]


@dataclass(frozen=True, slots=True)
class RalphRunResult:
    task: RalphTask
    durability_error: str | None = None


@dataclass(frozen=True, slots=True)
class RalphProgressEvent:
    kind: str
    task_id: str
    status: RalphTaskStatus
    iteration: int
    max_iterations: int
    message: str = ""


class RalphService:
    """Transport-neutral Ralph iteration state machine."""

    def __init__(
        self,
        *,
        turn_executor: TurnExecutor,
        store: Any,
        verifier: Any = None,
        context_factory: Callable[[], Any] | None = None,
        context_manager: Any = None,
        observer: Callable[[RalphProgressEvent], Any] | None = None,
        task_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._turn_executor = turn_executor
        self._store = store
        self._verifier = verifier
        self._context_factory = context_factory or _default_context_factory
        self._context_manager = context_manager
        self._observer = observer
        self._task_id_factory = task_id_factory or (lambda: uuid.uuid4().hex)

    def list_tasks(self) -> list[RalphTask]:
        return self._store.list_tasks()

    async def start(
        self,
        goal: str,
        session_state: Any,
        *,
        max_iterations: int,
        verify_command: str | None = None,
        observer: Callable[[RalphProgressEvent], Any] | None = None,
    ) -> RalphRunResult:
        task = RalphTask(
            id=self._task_id_factory(),
            goal=goal,
            max_iterations=max_iterations,
            verify_command=verify_command,
        )
        try:
            self._store.save(task)
        except Exception as exc:
            return RalphRunResult(
                _clone_task(task),
                durability_error=f"Unable to persist Ralph task: {exc}",
            )
        return await self.run(task, session_state, observer=observer)

    async def resume(
        self,
        task_ref: str,
        session_state: Any,
        *,
        observer: Callable[[RalphProgressEvent], Any] | None = None,
    ) -> RalphRunResult:
        task = self._store.load(task_ref)
        if task.status in (
            RalphTaskStatus.COMPLETE,
            RalphTaskStatus.MAX_ITERATIONS_REACHED,
        ):
            raise RalphValidationError(
                "task_not_resumable",
                f"Ralph task '{task.id}' cannot be resumed from {task.status.value}",
            )
        if task.status in (RalphTaskStatus.INTERRUPTED, RalphTaskStatus.FAILED):
            durable_task = _clone_task(task)
            task.status = RalphTaskStatus.RUNNING
            task.last_error = None
            durable = self._save_or_error(task, durable_task)
            if durable is not None:
                return durable
        return await self.run(task, session_state, observer=observer)

    async def run(
        self,
        task: RalphTask,
        session_state: Any,
        *,
        observer: Callable[[RalphProgressEvent], Any] | None = None,
    ) -> RalphRunResult:
        durable_task = _clone_task(task)
        summaries: list[str] = []
        result: RalphRunResult | None = None
        try:
            while task.current_iteration < task.max_iterations:
                if _is_cancelled(getattr(session_state, "cancel_token", None)):
                    result = self._persist_terminal(
                        task,
                        durable_task,
                        RalphTaskStatus.INTERRUPTED,
                        "Ralph run was cancelled",
                    )
                    break

                iteration_number = task.current_iteration + 1
                pending = getattr(session_state, "pending_interjections", None)
                if pending is None:
                    pending = []
                interjections = list(pending)
                pending.clear()
                try:
                    prompt = _build_prompt(task, iteration_number, interjections)
                    context = self._context_factory()
                    metadata = getattr(context, "metadata", None)
                    if metadata is not None:
                        metadata["pending_messages"] = pending
                        token = getattr(session_state, "cancel_token", None)
                        if token is not None:
                            metadata["cancel_token"] = token
                        model_override = getattr(session_state, "model_override", None)
                        if model_override is not None:
                            metadata["model_override"] = model_override
                    iteration = await self._turn_executor(
                        context,
                        prompt,
                        cancel_token=getattr(session_state, "cancel_token", None),
                        model_override=getattr(session_state, "model_override", None),
                    )
                    if not isinstance(iteration, RalphIterationResult):
                        raise TypeError("turn executor must return RalphIterationResult")
                    iteration = replace(
                        iteration,
                        iteration=iteration_number,
                        summary=_bounded(iteration.summary, RALPH_SUMMARY_LIMIT),
                    )
                except asyncio.CancelledError:
                    result = await _persist_terminal_after_cancellation(
                        self,
                        task,
                        durable_task,
                    )
                    break
                except Exception as exc:
                    iteration = RalphIterationResult(
                        iteration_number,
                        "(turn execution failed)",
                        error=_diagnostic(exc),
                    )

                next_status = RalphTaskStatus.RUNNING
                if iteration.error:
                    next_status = RalphTaskStatus.FAILED
                    task.last_error = _bounded(iteration.error, RALPH_DIAGNOSTIC_LIMIT)
                elif task.verify_command:
                    try:
                        if self._verifier is None:
                            raise RuntimeError("Ralph verifier is not configured")
                        verification = await self._verifier.verify(
                            task.verify_command,
                            cancel_token=getattr(session_state, "cancel_token", None),
                        )
                    except asyncio.CancelledError:
                        result = await _persist_terminal_after_cancellation(
                            self,
                            task,
                            durable_task,
                        )
                        break
                    except Exception as exc:
                        verification = VerificationResult(
                            VerificationStatus.SETUP_ERROR,
                            error=_diagnostic(exc),
                        )
                    iteration = replace(iteration, verification=verification)
                    if verification.status is VerificationStatus.PASSED:
                        next_status = RalphTaskStatus.COMPLETE
                        iteration = replace(iteration, completed_by="verify_command")
                    elif verification.status is VerificationStatus.CANCELLED:
                        next_status = RalphTaskStatus.INTERRUPTED
                        task.last_error = verification.error or "Verification was cancelled"
                    elif verification.infrastructure_error:
                        next_status = RalphTaskStatus.FAILED
                        task.last_error = verification.error or "Verification setup failed"
                    else:
                        iteration = replace(
                            iteration,
                            summary=_append_verification_diagnostic(iteration, verification),
                        )
                elif task.completion_promise in iteration.summary:
                    next_status = RalphTaskStatus.COMPLETE
                    iteration = replace(iteration, completed_by="promise")

                task.current_iteration = iteration_number
                task.iterations.append(iteration)
                task.progress.append(_progress_entry(iteration))
                task.status = next_status
                save_error = self._save_or_error(task, durable_task)
                if save_error is not None:
                    result = save_error
                    break
                durable_task = _clone_task(task)
                summaries.append(f"Iter {iteration_number}: {iteration.summary}")
                await self._observe("iteration", task, iteration.summary, observer)
                if next_status is not RalphTaskStatus.RUNNING:
                    result = RalphRunResult(_clone_task(task))
                    break

            if result is None:
                result = self._persist_terminal(
                    task,
                    durable_task,
                    RalphTaskStatus.MAX_ITERATIONS_REACHED,
                    "Maximum iterations reached",
                )
            if result.durability_error is None:
                await self._observe(
                    "terminal",
                    result.task,
                    result.task.last_error or result.task.status.value,
                    observer,
                )
            return result
        finally:
            if (
                result is not None
                and result.durability_error is None
                and summaries
            ):
                await asyncio.to_thread(
                    self._stage_memory, result.task, summaries, session_state
                )

    def _persist_terminal(
        self,
        task: RalphTask,
        durable_task: RalphTask,
        status: RalphTaskStatus,
        message: str,
    ) -> RalphRunResult:
        candidate = _clone_task(task)
        candidate.status = status
        candidate.last_error = message if status in (
            RalphTaskStatus.INTERRUPTED,
            RalphTaskStatus.FAILED,
        ) else None
        return self._save_or_error(candidate, durable_task) or RalphRunResult(candidate)

    def _save_or_error(
        self, task: RalphTask, durable_task: RalphTask
    ) -> RalphRunResult | None:
        try:
            self._store.save(task)
        except Exception as exc:
            return RalphRunResult(
                _clone_task(durable_task),
                durability_error=f"Unable to persist Ralph task: {exc}",
            )
        return None

    async def _observe(
        self,
        kind: str,
        task: RalphTask,
        message: str,
        observer: Callable[[RalphProgressEvent], Any] | None = None,
    ) -> None:
        target = observer or self._observer
        if target is None:
            return
        event = RalphProgressEvent(
            kind=kind,
            task_id=task.id,
            status=task.status,
            iteration=task.current_iteration,
            max_iterations=task.max_iterations,
            message=message,
        )
        try:
            observed = target(event)
            if inspect.isawaitable(observed):
                await observed
        except Exception:
            logger.exception("Ralph observer failed: task_id=%s kind=%s", task.id, kind)

    def _stage_memory(
        self, task: RalphTask, summaries: list[str], session_state: Any
    ) -> None:
        manager = (
            getattr(session_state, "context_manager", None)
            or self._context_manager
        )
        if manager is None or not summaries:
            return
        try:
            manager.staging.append(
                "user",
                f"[Ralph/{task.id}] goal: {task.goal} | status: {task.status.value} "
                f"| iters: {task.current_iteration}/{task.max_iterations}",
            )
            manager.staging.append("assistant", "\n".join(summaries[-5:]))
            manager.mark_activity()
            if manager.should_enqueue_consolidation():
                manager.enqueue_consolidation("ralph_task_end")
        except Exception:
            logger.exception("Ralph memory staging failed: task_id=%s", task.id)


async def _persist_terminal_after_cancellation(
    service: RalphService,
    task: RalphTask,
    durable_task: RalphTask,
) -> RalphRunResult:
    # The task file is small and saved atomically. Persist synchronously at this
    # exceptional boundary so cancellation cannot abandon an unkillable worker
    # thread whose late os.replace() could overwrite a resumed task.
    return service._persist_terminal(
        task,
        durable_task,
        RalphTaskStatus.INTERRUPTED,
        "Ralph run was cancelled",
    )


def _default_context_factory() -> Any:
    from agent.core.agent import AgentContext

    return AgentContext()


def _clone_task(task: RalphTask) -> RalphTask:
    return RalphTask.from_dict(task.to_dict())


def _is_cancelled(token: Any) -> bool:
    return bool(token is not None and getattr(token, "is_cancelled", False))


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[-limit:]


def _diagnostic(exc: BaseException) -> str:
    return _bounded(f"{type(exc).__name__}: {exc}", RALPH_DIAGNOSTIC_LIMIT)


def _verification_diagnostic(verification: VerificationResult) -> str:
    parts = [f"verification status: {verification.status.value}"]
    if verification.exit_code is not None:
        parts.append(f"exit code: {verification.exit_code}")
    output = verification.stderr_tail or verification.stdout_tail or verification.error
    if output:
        parts.append(_bounded(output.strip(), RALPH_DIAGNOSTIC_LIMIT))
    return "\n".join(parts)


def _append_verification_diagnostic(
    iteration: RalphIterationResult,
    verification: VerificationResult,
) -> str:
    return _bounded(
        f"{iteration.summary}\n\n{_verification_diagnostic(verification)}",
        RALPH_SUMMARY_LIMIT,
    )


def _progress_entry(iteration: RalphIterationResult) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "iteration": iteration.iteration,
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool_calls": list(iteration.tool_calls),
        "summary": iteration.summary,
    }
    if iteration.completed_by:
        entry["completed_by"] = iteration.completed_by
    if iteration.error:
        entry["error"] = iteration.error
    if iteration.verification:
        entry["verification"] = iteration.verification.to_dict()
    return entry


def _build_prompt(
    task: RalphTask,
    iteration_number: int,
    interjections: list[dict[str, Any]],
) -> str:
    criteria = "\n".join(f"- {item}" for item in task.completion_criteria) or "- None"
    recent = task.progress[-3:]
    progress = ""
    if recent:
        progress = "\n\n## Recent Progress\n" + "\n".join(
            f"- Iteration {item.get('iteration')}: {item.get('summary', '')}"
            for item in recent
        )
    mailbox = ""
    if interjections:
        lines = []
        for item in interjections:
            urgency = str(item.get("urgency", "normal"))
            lines.append(f"- [{urgency}] {item.get('text', '')}")
        mailbox = "\n\n## User Interjections\n" + "\n".join(lines)
    return (
        f"## Current Task\n{task.goal}\n\n"
        f"## Acceptance Criteria\n{criteria}\n\n"
        f"Once all criteria are satisfied, output at the end of your reply: "
        f"`{task.completion_promise}`"
        f"{progress}{mailbox}\n\n"
        f"This is iteration {iteration_number} of {task.max_iterations}."
    )


__all__ = [
    "RALPH_DIAGNOSTIC_LIMIT",
    "RALPH_SUMMARY_LIMIT",
    "RalphProgressEvent",
    "RalphRunResult",
    "RalphService",
]
