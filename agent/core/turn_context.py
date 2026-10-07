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
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.core.agent import AgentContext

__all__ = [
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
