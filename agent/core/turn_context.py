"""Which turn is in flight, and how a turn publishes an event.

Extracted from ``agent/core/agent.py`` when the turn loop started moving out of
``BaseAgent``: the loop needs to ask "which agent is this?" and to publish domain
events, and importing those from ``agent.py`` would be a cycle -- ``agent`` imports
the loop.

They are not agent state, which is why they can move down rather than being passed
around.  ``_active_agent_context`` is a :class:`contextvars.ContextVar`, so its
value is per-task rather than per-agent: two agents running concurrently in
different tasks each see their own, and a call outside any turn sees none.  That
property is the reason the loop can publish about "the agent in flight" without
being handed one.

The marker rather than an invented id
-------------------------------------
:func:`active_agent_id` returns ``"unknown"`` outside a turn, where a continuation
or a probe runs.  Inventing an id there would make two unrelated calls look like one
agent's, which is worse than admitting there is no agent to name.
"""

from __future__ import annotations

import asyncio
import contextvars
from agent.core.output import _active_event_collector
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.core.agent import AgentContext

#: The metadata key that marks a turn as running.  Read by the maintenance path to
#: tell "between turns" from "in one", so it is the counterpart of
#: :data:`TURN_REQUEST_KEY`.
TURN_IN_PROGRESS_KEY = "_turn_in_progress"

#: The metadata key a turn's opening request is stored under.  Written by the loop
#: when it prepares a turn and appended to by the interjection drain, so it lives
#: beside them rather than in ``agent.py``.
TURN_REQUEST_KEY = "_turn_request_message"

#: Whether the "provider reported no usage" line has been printed this process.
#: Module state rather than a class attribute because it is a fact about the
#: process, not about an agent -- and because the loop code that reads it no longer
#: lives in ``BaseAgent``.
missing_usage_warned = False


def claim_missing_usage_warning() -> bool:
    """True the first time this process asks, False afterwards.

    A function rather than a flag the caller reads and sets, because the caller is a
    module-level function in ``turn_loop``: assigning the flag there would make it a
    local and ``UnboundLocalError`` on the read.  ``global`` in the caller would work
    and would put the "once per process" rule in two places instead of one.
    """
    global missing_usage_warned
    if missing_usage_warned:
        return False
    missing_usage_warned = True
    return True


#: This turn's orchestration run records; moves with the orchestration driver.
active_orchestration_runs: contextvars.ContextVar[Optional[list[dict[str, Any]]]] = (
    contextvars.ContextVar("active_orchestration_runs", default=None)
)

__all__ = [
    "TURN_IN_PROGRESS_KEY",
    "TURN_REQUEST_KEY",
    "active_orchestration_runs",
    "emit_event",
    "claim_missing_usage_warning",
    "interaction_log",
    "preview_text",
    "trace_latency",
    "missing_usage_warned",
    "active_agent_context",
    "active_agent_id",
    "emit_domain_event",
    "schedule_domain_event",
    "set_active_agent_context",
]

#: The agent whose turn is executing in the current task, if any.
active_agent_context: contextvars.ContextVar[Optional["AgentContext"]] = (
    contextvars.ContextVar("active_agent_context", default=None)
)


def set_active_agent_context(ctx: Optional["AgentContext"]) -> contextvars.Token:
    """Bind *ctx* as the turn in flight; the token restores the previous value.

    Returned rather than pushed onto a stack: a token cannot be mis-ordered, and the
    caller that set the value is the one that resets it.
    """
    return active_agent_context.set(ctx)


def active_agent_id() -> str:
    """The agent id of the turn in flight, or ``"unknown"`` when there is none."""
    ctx = active_agent_context.get()
    return str(getattr(ctx, "agent_id", "") or "unknown")


def schedule_domain_event(bus: Any, event_type: type, **fields: object) -> None:
    """Publish from a synchronous call site, if a loop is running.

    ``_parse_response`` is synchronous and on the hot path, so it cannot await.
    When there is no running loop there is nothing to deliver to -- a listener only
    exists inside a turn, which runs on a loop -- and scheduling then would leak a
    coroutine.
    """
    if bus is None or not bus.has_listeners(event_type):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(bus.emit(event_type(**fields)))


async def emit_domain_event(bus: Any, event_type: type, **fields: object) -> None:
    """Publish one typed event, if anyone is listening.

    The ``has_listeners`` check is not an optimisation detail: this sits on the hot
    path of every turn, and a turn must not pay for a feature nobody mounted.  The
    bus isolates a listener that raises, so a subscriber cannot break the turn it
    observes -- which is the property that makes mounting a plugin safe.
    """
    if bus is None or not bus.has_listeners(event_type):
        return
    await bus.emit(event_type(**fields))


def emit_event(name: str, **fields: object) -> None:
    """Record a runtime event when a turn collector is listening.

    Observation must never be able to break the turn it observes, so a
    missing collector is a no-op rather than an error.  Mirrors
    ``ToolExecutor._emit`` and ``_emit_consolidation``.
    """
    collector = _active_event_collector.get()
    if collector is not None:
        collector.emit(name, **fields)


def trace_latency(stage: str, **fields: object) -> None:
    """Record a latency trace attributed to the agent component."""
    from agent import shared

    shared._trace_latency("agent", stage, **fields)


def interaction_log(event: str, **fields: object) -> None:
    """Record an interaction-log line attributed to the agent component."""
    from agent import shared

    shared._interaction_log("agent", event, **fields)


def preview_text(text: object, limit: int = 80) -> str:
    """See :func:`agent.shared._preview_text`."""
    from agent import shared

    return shared._preview_text(text, limit=limit)
