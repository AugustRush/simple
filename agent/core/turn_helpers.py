"""The few turn helpers that need nothing but the turn's own context.

Small on purpose.  These were static/class methods on ``BaseAgent`` that read no instance
state, which means the turn loop can use them without an agent -- and that is the point: the
loop's dependencies should be obtainable from services and settings, with an agent being one
thing that happens to hold them rather than the only route.

The budget arithmetic is the same story one level up: its four entries on ``BaseAgent`` were
already delegations to :mod:`agent.core.token_budget`, so the implementation had left the agent
and only the *route* still went through it.
"""

from __future__ import annotations

from typing import Any

from agent.core.turn_context import TURN_REQUEST_KEY

__all__ = ["protected_turn_messages"]


def protected_turn_messages(ctx: Any) -> list[dict]:
    """The messages a cut running inside this turn must keep.

    The turn's own request, and nothing else: history is evictable, the reason the turn exists
    is not.  Empty outside a turn, where the caller has no object to hand over and
    ``ContextManager._protected_indexes`` falls back to the newest message that looks like a
    request.
    """
    message = ctx.metadata.get(TURN_REQUEST_KEY)
    return [message] if isinstance(message, dict) else []
