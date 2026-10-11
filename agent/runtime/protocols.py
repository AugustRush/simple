"""The runtime services' protocols: the turn loop and the core that drives it.

Both are **floors** in the ownership ledger, and for the same reason: they are the
runtime's own view of the composition.  ``agent_loop`` is the loop a composition runs
its turns through, and ``agent_core`` is the object that assembles retrieval,
compaction, hooks and the loop for one turn.  Neither is a contribution -- but both are
*services other code resolves by name*, so their interfaces belong in writing: the
command layer calls ``handle_turn``, and the core drives the loop through ``run`` and
``complete_turn``.

The protocol is also what makes "the loop is replaceable" concrete.  A composition that
provides ``agent_loop`` gets its loop used **verbatim** (the core deliberately does not
rebind a loop a composition chose -- see ``AgentCore._turn_runner``), so a replacement
has to work for the container it was provided into, with these two calls and nothing
else.  ``bind`` is not here: it is the *stock* loop's own classmethod, and a replacement
never needs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - types only
    from agent.runtime.contracts import RuntimeSessionState, TurnExecution, TurnInput, TurnResult

__all__ = ["AgentCoreService", "AgentLoopService"]


@runtime_checkable
class AgentLoopService(Protocol):
    """The turn loop a composition provides as ``agent_loop``."""

    async def run(
        self,
        turn_input: "TurnInput",
        ctx: Any,
        *,
        stream_callback: "Optional[Callable[[str], None]]" = None,
    ) -> "TurnResult":
        """Run one turn's step loop against *ctx*."""

    async def complete_turn(
        self,
        turn_input: "TurnInput",
        state: "RuntimeSessionState",
        result: "TurnResult",
    ) -> "list[Any]":
        """Finish a turn; returns the hook results the caller checks for ``continue``."""


@runtime_checkable
class AgentCoreService(Protocol):
    """The runtime core a composition provides as ``agent_core``."""

    async def handle_turn(
        self,
        turn_input: "TurnInput",
        state: "RuntimeSessionState",
        *,
        sink: Any = None,
        stream_callback: "Optional[Callable[[str], None]]" = None,
        max_continuations: int = 1,
    ) -> "TurnExecution":
        """Run a whole turn: retrieval, the loop, hooks, maintenance, continuations."""
