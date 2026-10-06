"""Driving a sub-agent: the retry loop and its wall-clock budget.

Why this module exists
----------------------
``_execute_agent`` is a 288-line orchestration, and 111 of those lines are this
loop.  Measuring it first is what made the extraction safe: the loop reaches
exactly four attributes of its parent (``sub_agent_retries``,
``sub_agent_timeout_seconds``, and two callables), so everything else it needs
can be passed in -- including the function that runs one attempt, which is what
makes the retry policy testable with a scripted callable instead of a live model.

The budget rule, stated once
----------------------------
``sub_agent_timeout_seconds`` bounds the **whole subtask**, not each attempt.
Giving every retry a fresh full timeout would let one subtask consume
``attempts x timeout`` of wall clock while still reporting the single-attempt
figure as its deadline -- so the deadline is computed once, and each attempt is
given only what remains.

A timeout ends the loop immediately rather than trying again: it has already
consumed the remaining budget, so there is nothing left to retry with.  An
ordinary failure retries only while the budget lasts, and only for profiles where
a retry is safe -- see :func:`retries_for_profile`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

__all__ = ["AttemptOutcome", "retries_for_profile", "run_with_retries"]

#: Profiles whose work is a retry safe operation.  An implementation worker may
#: have written files or produced other side effects that a retry cannot roll
#: back, and ``full`` is excluded for the same reason.
_RETRY_SAFE_PROFILES = frozenset({"read_only", "research"})


def retries_for_profile(profile: str, configured_retries: int) -> int:
    """How many times a subtask of *profile* may be retried.

    Zero for anything that could have written something.
    """
    if profile not in _RETRY_SAFE_PROFILES:
        return 0
    return max(0, int(configured_retries))


@dataclass(frozen=True)
class AttemptOutcome:
    """What the loop produced.

    ``payload`` is set only when the loop ended in failure and has something to
    report (a timeout, or the last failure); otherwise the caller inspects
    ``result``.  Both can be ``None`` when the budget ran out before any attempt
    completed, which is why the caller has a third fallback.
    """

    result: Any = None
    payload: Optional[dict] = None
    started_at: float = 0.0
    attempts: int = 0


async def run_with_retries(
    *,
    role: str,
    shaped_task: str,
    capability_profile: str,
    write_scope_count: int,
    configured_retries: int,
    timeout_seconds: float,
    send: Callable[[], Awaitable[Any]],
    fresh_context: Callable[[], Any],
    current_context: Callable[[], Any],
    emit: Callable[..., None],
    format_error: Callable[[BaseException], str],
) -> AttemptOutcome:
    """Run one subtask, retrying transient failures while the budget lasts.

    ``send`` runs one attempt; ``fresh_context`` builds the conversation state
    for an attempt (a retry must not inherit a failed run's messages);
    ``current_context`` reads the context of the attempt in flight, which is
    needed to salvage partial text from a timeout.
    """
    retries = retries_for_profile(capability_profile, configured_retries)
    max_attempts = retries + 1

    # The deadline is computed once: it bounds the subtask, not each attempt.
    total_budget = float(timeout_seconds)
    budget_deadline = time.monotonic() + total_budget

    result: Any = None
    failure_payload: Optional[dict] = None
    started_at = time.monotonic()
    attempt = 0

    for attempt in range(1, max_attempts + 1):
        remaining_budget = budget_deadline - time.monotonic()
        if attempt > 1 and remaining_budget <= 0:
            # Budget exhausted: stop retrying rather than overrun it.
            break
        if attempt > 1:
            emit(
                kind="agent_retry",
                role=role,
                task=shaped_task,
                message=f"{role} retry {attempt}/{max_attempts}",
                metrics={
                    "capability_profile": capability_profile,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                },
            )
        if attempt > 1:
            # Replace the conversation state before the redone attempt.  A
            # failed run's messages must not be inherited: the retry would
            # otherwise continue a conversation the model already answered, and
            # the parent would read two attempts as one turn.
            fresh_context()
        started_at = time.monotonic()
        try:
            result = await asyncio.wait_for(
                send(), timeout=max(0.0, budget_deadline - time.monotonic())
            )
            # A success clears the previous attempt's payload.  Without this a
            # subtask that failed once and then succeeded would report the old
            # error, because the caller reads `payload` whenever it is set.
            failure_payload = None
            break
        except asyncio.TimeoutError:
            payload: dict[str, Any] = {
                "ok": False,
                "role": role,
                "task": shaped_task,
                "timed_out": True,
                "error": (
                    f"sub-agent exhausted its {total_budget:g}s budget after "
                    f"{_spent(total_budget, budget_deadline):.1f}s across "
                    f"{attempt} attempt(s)"
                ),
            }
            partial = _partial_text(current_context())
            if partial:
                payload["partial_content"] = partial
            failure_payload = payload
            # A timeout consumed the remaining budget, so report instead of
            # silently overrunning it.
            emit(
                kind="agent_failed",
                role=role,
                task=shaped_task,
                message=payload["error"],
                metrics={
                    "capability_profile": capability_profile,
                    "write_scope_count": write_scope_count,
                    "duration_seconds": time.monotonic() - started_at,
                    "attempts": attempt,
                },
            )
            return AttemptOutcome(payload=payload, started_at=started_at, attempts=attempt)
        except Exception as exc:
            payload = {
                "ok": False,
                "role": role,
                "task": shaped_task,
                "error": f"sub-agent failed: {format_error(exc)}",
            }
            failure_payload = payload
            if attempt < max_attempts:
                # Retry a transient failure only while budget remains; the
                # check at the top of the loop enforces that.
                continue
            emit(
                kind="agent_failed",
                role=role,
                task=shaped_task,
                message=payload["error"],
                metrics={
                    "capability_profile": capability_profile,
                    "write_scope_count": write_scope_count,
                    "duration_seconds": time.monotonic() - started_at,
                    "attempts": attempt,
                },
            )
            return AttemptOutcome(payload=payload, started_at=started_at, attempts=attempt)

    return AttemptOutcome(
        result=result,
        payload=failure_payload,
        started_at=started_at,
        attempts=attempt,
    )


def _spent(total_budget: float, budget_deadline: float) -> float:
    """Seconds of the budget already used.

    ``now + total - deadline`` rather than ``total - remaining`` so the figure
    does not depend on a second clock reading drifting from the first.
    """
    return time.monotonic() + total_budget - budget_deadline


def _partial_text(context: Any) -> str:
    """The last assistant text in *context*, truncated, or ``""``.

    A timed-out sub-agent may have said something useful before it was cut off,
    and discarding that would make the failure less informative than it can be.
    """
    for message in reversed(getattr(context, "messages", []) or []):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])[:500]
    return ""
