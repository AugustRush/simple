"""The composition's own plugins, as data.

Why this exists
---------------
Six plugins are mounted by the composition root rather than discovered on disk -- Ralph,
Memory, the session log, the agent, the built-in tools and the turn runner.  They were five
literal ``mount(...)`` calls in a 900-line function, so a question a composition model should
answer -- "this deployment has no memory plugin" -- had **no way to be asked**.  A profile could
narrow the *discovered* plugins and could not touch these at all.

The precondition for this being possible at all was measured last round: **all 24 orders** of
memory / session / agent / tools produce the same complete composition.  A list that can be
reordered freely is a list that can be data.

What a declaration carries, and why a factory
---------------------------------------------
Each entry names a plugin and how to build it, because two of them need an argument:

* ``agent`` takes the class to construct, so ``bootstrap.BaseAgent`` stays the substitution
  point (a test that replaces it must reach the plugin);
* ``tools`` takes the same for the built-in tool set.

The factory receives a :class:`CoreContext` -- the few things the composition root has that a
plugin cannot resolve for itself -- rather than the whole component mapping, so that "what does
this need" stays answerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

__all__ = ["CORE_PLUGINS", "CorePlugin", "CoreContext", "core_plugin_names"]


@dataclass(frozen=True)
class CoreContext:
    """What a core plugin's factory is handed.

    Deliberately not the component mapping: a factory that can reach anything makes "which
    services does this plugin need" unanswerable, which is the property the whole design is
    trying to preserve.
    """

    #: ``bootstrap``'s module-level ``BaseAgent``, so replacing that name still decides which
    #: agent runs.
    agent_class: Any = None
    #: The same, for the built-in tool set.
    tools_class: Any = None
    #: Anything else a specific entry needs, by name, with the entry declaring what it takes.
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CorePlugin:
    """One plugin the composition root mounts."""

    name: str
    #: Why this is a core plugin rather than a discovered one.  Read by a person deciding
    #: whether a new plugin belongs here.
    summary: str
    factory: Callable[[CoreContext], Any]
    #: The services it provides, for a composition that wants to report what it has.
    provides: tuple[str, ...] = ()

    def build(self, context: CoreContext) -> Any:
        return self.factory(context)


def _ralph(_: CoreContext) -> Any:
    from agent.ralph.plugin import RalphPlugin

    return RalphPlugin()


def _memory(_: CoreContext) -> Any:
    from agent.memory.plugin import MemoryPlugin

    return MemoryPlugin()


def _session(_: CoreContext) -> Any:
    from agent.memory.session_plugin import SessionPlugin

    return SessionPlugin()


def _agent(context: CoreContext) -> Any:
    from agent.core.agent_plugin import AgentPlugin

    return AgentPlugin(context.agent_class)


def _tools(context: CoreContext) -> Any:
    from agent.tools.plugin import ToolsPlugin

    return ToolsPlugin(context.tools_class)


def _agent_loop(_: CoreContext) -> Any:
    from agent.runtime.contracts import TurnRunner

    return TurnRunner()


#: Ordered for a reader; the kernel resolves the dependencies, in any order (measured).
CORE_PLUGINS: tuple[CorePlugin, ...] = (
    CorePlugin(
        name="ralph",
        summary="the task loop; provides `ralph_service`",
        factory=_ralph,
        provides=("ralph_service",),
    ),
    CorePlugin(
        name="memory",
        summary="the store and the palace; provides `ltm_store`, `memory`",
        factory=_memory,
        provides=("ltm_store", "memory"),
    ),
    CorePlugin(
        name="session",
        summary="the session log; provides `context_manager`",
        factory=_session,
        provides=("context_manager",),
    ),
    CorePlugin(
        name="agent",
        summary="the agent; provides `agent`",
        factory=_agent,
        provides=("agent",),
    ),
    CorePlugin(
        name="tools",
        summary="the built-in tool set; registers on the tool registry",
        factory=_tools,
    ),
    CorePlugin(
        name="agent_loop",
        summary="the turn runner; provides `agent_loop`",
        factory=_agent_loop,
        provides=("agent_loop",),
    ),
)

_BY_NAME = {plugin.name: plugin for plugin in CORE_PLUGINS}


def core_plugin_names() -> tuple[str, ...]:
    """The names, for a profile or a diagnostic that lists what a composition has."""
    return tuple(plugin.name for plugin in CORE_PLUGINS)


def core_plugins(
    *,
    enabled: tuple[str, ...] | None = None,
    replacements: Mapping[str, str] | None = None,
) -> tuple[CorePlugin, ...]:
    """The core plugins to mount, in declaration order.

    *enabled* ``None`` means all of them, which is the only safe default: every existing
    composition mounts all six, and a default that silently dropped one would be a behaviour
    change nobody asked for.

    *replacements* maps a **service name** to the **plugin** that provides it instead.  A core
    plugin whose ``provides`` intersects that set is left out, because mounting both would be
    two providers of one name -- which the kernel refuses at the second ``provide``, with an
    error naming neither the profile nor the replacement.  Dropping it here is how "my plugin
    provides ``memory``" becomes a composition rather than a collision.

    The caller is expected to *check* the replacement afterwards: this decides what not to
    mount, and cannot know whether the named plugin actually came up.
    """
    chosen = list(CORE_PLUGINS)
    if enabled is not None:
        unknown = sorted(set(enabled) - set(_BY_NAME))
        if unknown:
            raise KeyError(f"no such core plugin: {unknown}")
        chosen = [plugin for plugin in chosen if plugin.name in set(enabled)]

    if replacements:
        replaced = set(replacements)
        displaced = sorted(
            plugin.name for plugin in chosen if set(plugin.provides) & replaced
        )
        if displaced:
            chosen = [plugin for plugin in chosen if plugin.name not in set(displaced)]

    return tuple(chosen)
