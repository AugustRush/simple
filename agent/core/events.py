"""Typed events for the tool execution pipeline, and the bus that carries them.

Why this module exists
----------------------
``RegularToolExecutor`` used to be constructed with the plugin catalog --
``RegularToolExecutor(self.registry, plugin_catalog=self.plugin_catalog)``
(``agent/core/agent.py:2741-2744``) -- and called ``fire_pre_tool`` /
``fire_post_tool`` on it directly.  So the tool execution layer named the
plugin machinery, and the plugin machinery named the tool events it defined.
Neither could be replaced without the other.

The fix is a one-way dependency: the executor publishes typed events, and the
plugin catalog subscribes.  The executor no longer imports, receives, or knows
about plugins; a security plugin, an audit listener, or a test can all gate a
tool through the same seam.

Two semantics are preserved exactly
-----------------------------------
Both were surprising and neither was documented as intentional, so the typed
version keeps them rather than quietly "fixing" them:

**The first block wins.**  ``fire_pre_tool`` short-circuited on the first
blocking result and discarded the rest.  Emission therefore stops at the first
listener that blocks -- a security gate that a later listener can argue with is
not a gate, and the reason from the listener that actually blocked is the one
that has to survive.

**``ToolPostExecute`` is observational.**  ``fire_post_tool`` computed a
``HookResult`` and the call site dropped it (``executor.py:538``).  The event is
a notification; a return value is ignored, and that is stated here so a
listener author is not left guessing.

What this is not
----------------
Not a general event system.  There is no domain vocabulary, no priority, no
once/off variants, and no filtering by anything but Python type.  The persisted
event log (``agent/memory/models.py:AgentRuntimeEvent``) and the per-turn
``EventCollector`` already exist for the two jobs those do; this is only the
in-process extension point for the tool pipeline.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = [
    "EventBus",
    "ToolPostExecute",
    "ToolPreExecute",
    "TOOL_EVENTS",
    "TURN_EVENTS",
    "LLM_EVENTS",
]


@dataclass(frozen=True)
class ToolPreExecute:
    """One tool call, before it runs.

    ``arguments`` is the caller's own dict, not a copy: a listener inspects the
    exact object the tool will be handed, and a mutation by an earlier listener
    is visible to a later one.  Copying would let a gate reason about a
    snapshot nobody executed.
    """

    tool_name: str
    arguments: dict


@dataclass(frozen=True)
class ToolPostExecute:
    """One tool call, after it ran, carrying the result it produced.

    Observational: a value returned by a listener is ignored.  See the module
    docstring for why that is preserved rather than changed.
    """

    tool_name: str
    arguments: dict
    result: str


Listener = Callable[[Any], Any]


class EventBus:
    """In-process typed event delivery.

    Listeners are matched by exact event class.  Delivery is registration
    ordered, awaits async listeners, and isolates failures: one listener raising
    must not stop the others, because the alternative is that a single broken
    plugin silently disables every gate behind it.
    """

    __slots__ = ("_listeners",)

    def __init__(self) -> None:
        self._listeners: dict[type, list[Listener]] = {}

    def on(self, event_type: type, listener: Listener) -> Callable[[], None]:
        """Subscribe *listener* to *event_type*; returns an unsubscribe callable."""
        self._listeners.setdefault(event_type, []).append(listener)

        def unsubscribe() -> None:
            listeners = self._listeners.get(event_type)
            if not listeners:
                return
            try:
                listeners.remove(listener)
            except ValueError:
                pass
            if not listeners:
                self._listeners.pop(event_type, None)

        return unsubscribe

    def has_listeners(self, event_type: type) -> bool:
        return bool(self._listeners.get(event_type))

    def listeners(self, event_type: type) -> tuple[Listener, ...]:
        return tuple(self._listeners.get(event_type, ()))

    async def emit(self, event: Any) -> Any:
        """Deliver *event*.

        Returns the first non-``None`` listener result, or ``None`` when every
        listener returned nothing.  Emission stops early only when the event
        itself was blocked (see :meth:`emit_pre_execute`); a plain emit always
        reaches every listener.
        """
        result: Any = None
        for listener in self.listeners(type(event)):
            value = await self._invoke(listener, event)
            if value is not None and result is None:
                result = value
        return result

    async def emit_pre_execute(self, event: ToolPreExecute) -> Optional[str]:
        """Deliver a :class:`ToolPreExecute`, stopping at the first refusal.

        A listener refuses by returning (or awaiting) a truthy reason; ``None``
        means "no opinion", which is the only thing a listener defaults to.
        Returns the refusal reason, or ``None`` when the call may proceed.

        Later listeners are not called once a refusal is recorded.  That is the
        preserved short-circuit: a gate a subsequent listener can argue with is
        not a gate.
        """
        for listener in self.listeners(ToolPreExecute):
            value = await self._invoke(listener, event)
            if value:
                return str(value)
        return None

    @staticmethod
    async def _invoke(listener: Listener, event: Any) -> Any:
        try:
            if inspect.iscoroutinefunction(listener):
                return await listener(event)
            return listener(event)
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            # Reported, not swallowed: a listener that fails silently is a gate
            # that silently stops gating.
            from agent import shared

            shared.CONSOLE.print(
                f"[dim]tool event listener failed "
                f"({type(event).__name__}): {exc}[/dim]"
            )
            return None


#: The bus the tool pipeline publishes to.
#:
#: Module-level because the executor is built in several places (the turn loop,
#: the scheduler, tests) and a per-instance bus would mean a plugin's gate
#: applied to whichever construction path happened to pass one.  Listeners are
#: registered by the plugin catalog when it loads; a composition that never
#: mounts plugins simply has no listeners.
TOOL_EVENTS = EventBus()

#: The bus the turn loop publishes to.
#:
#: A second module-level bus rather than a second *kind* of bus: the bus already
#: dispatches by exact event class, so a listener for ``TurnStarted`` never sees a
#: tool event even though both live in this process.  Separate names exist so a
#: reader can see which pipeline a listener is joining; they are deliberately the
#: same class, because a domain is vocabulary rather than transport (see
#: ``agent/core/event_domains.py``).
#:
#: Module-level for the same reason as ``TOOL_EVENTS``: the turn loop is built per
#: agent, and the scheduler builds its own, so a bus owned by one construction
#: would leave a plugin's listener unreachable from the other.
TURN_EVENTS = EventBus()

#: The bus the model-call path publishes to.
LLM_EVENTS = EventBus()
