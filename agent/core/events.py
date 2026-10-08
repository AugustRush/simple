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


# The bus implementation lives in the kernel: it is a kernel concept, and the
# tool pipeline is one of its users rather than its owner.
from agent.kernel.event import EventBus, Listener  # noqa: E402


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

#: The bus session lifecycle events are published to.
#:
#: A session is not a turn: a turn finishes many times per session, and this finishes
#: once.  A separate name for the same reason the others have one -- a reader can see
#: which pipeline a listener is joining -- and the same class, because a domain is
#: vocabulary rather than transport.
SESSION_EVENTS = EventBus()


#: The four domain buses, by domain name.  What a composition provides so that every
#: construction path -- the turn loop, the scheduler, a channel session -- resolves the
#: *same* bus.
_DOMAINS = ("tool", "turn", "llm", "session")


def default_buses() -> dict[str, EventBus]:
    """A fresh set of buses, for a composition that wants its own.

    The module-level buses above are the **process default**, which is what a program with
    one composition should use.  This exists for the program with two: a test suite, a
    gateway serving several runtimes, a scheduler that builds its own turn loop.

    The globals were justified by "the executor is built in several places, so a
    per-instance bus would mean a plugin's gate applied to whichever construction path
    happened to pass one" -- which is a good argument for **one bus per composition** and
    not for one per process.  A composition that provides these gets that property
    *and* isolation; one that provides nothing keeps the behaviour it had.
    """
    return {name: EventBus() for name in _DOMAINS}


def bus_name(domain: str) -> str:
    """The service name a domain's bus is provided under."""
    return f"event_bus.{domain}"
