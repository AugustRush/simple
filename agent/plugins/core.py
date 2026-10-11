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
    #: Mounted only when a composition asks for it -- by naming it in ``core`` or by
    #: naming it as the replacement for a service it provides.  A plugin that would
    #: collide with a default one has to be opt-in, and saying so here is better than
    #: a special case in the selection code.
    opt_in: bool = False

    def build(self, context: CoreContext) -> Any:
        return self.factory(context)


def _ralph(_: CoreContext) -> Any:
    from agent.ralph.plugin import RalphPlugin

    return RalphPlugin()


def _model(_: CoreContext) -> Any:
    from agent.core.model_plugin import ModelPlugin

    return ModelPlugin()


def _memory(_: CoreContext) -> Any:
    from agent.memory.plugin import MemoryPlugin

    return MemoryPlugin()


def _sqlite_fts_memory(_: CoreContext) -> Any:
    from agent.memory.backends.sqlite_fts import SqliteFtsMemoryPlugin

    return SqliteFtsMemoryPlugin()


def _mcp(_: CoreContext) -> Any:
    from agent.tools.mcp_plugin import MCPPlugin

    return MCPPlugin()


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
        name="model",
        summary=(
            "the model adapter; provides `llm` and the process-lifetime "
            "provider client cache behind it"
        ),
        factory=_model,
        provides=("llm", "provider_client_cache"),
    ),
    CorePlugin(
        name="memory",
        summary="the store and the palace; provides `ltm_store`, `memory`",
        factory=_memory,
        provides=("ltm_store", "memory"),
    ),
    CorePlugin(
        name="sqlite-fts-memory",
        summary=(
            "an alternative store (one SQLite file, FTS5 search) providing "
            "`ltm_store` and `memory`; opt-in, because by default the built-in "
            "memory plugin provides those"
        ),
        factory=_sqlite_fts_memory,
        provides=("ltm_store", "memory"),
        opt_in=True,
    ),
    CorePlugin(
        name="mcp",
        summary=(
            "the MCP client; provides `mcp_client`, the merged server list the "
            "composition connects, and the status it reports"
        ),
        factory=_mcp,
        provides=("mcp_client", "mcp_server_configs", "mcp_status"),
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

    *enabled* ``None`` means every plugin that is on by default, which is the only safe
    default: every existing composition mounts all six, and a default that silently dropped
    one would be a behaviour change nobody asked for.  A plugin marked ``opt_in`` is not
    part of that default -- it exists to *replace* one of them, so mounting both would be
    two providers of one name.

    *replacements* maps a **service name** to the **plugin** that provides it instead.  A core
    plugin whose ``provides`` intersects that set is left out, because mounting both would be
    two providers of one name -- which the kernel refuses at the second ``provide``, with an
    error naming neither the profile nor the replacement.  Dropping it here is how "my plugin
    provides ``memory``" becomes a composition rather than a collision.

    The caller is expected to *check* the replacement afterwards: this decides what not to
    mount, and cannot know whether the named plugin actually came up.
    """
    if enabled is None:
        chosen = [plugin for plugin in CORE_PLUGINS if not plugin.opt_in]
    else:
        unknown = sorted(set(enabled) - set(_BY_NAME))
        if unknown:
            raise KeyError(f"no such core plugin: {unknown}")
        chosen = [plugin for plugin in CORE_PLUGINS if plugin.name in set(enabled)]

    if replacements:
        replaced = set(replacements)
        displaced = {
            plugin.name for plugin in chosen if set(plugin.provides) & replaced
        }
        chosen = [plugin for plugin in chosen if plugin.name not in displaced]
        # Naming a plugin as the replacement for a service *is* asking for it, so a
        # core plugin that is otherwise opt-in joins the composition here.  A name
        # that is not one of ours is a discovered plugin, mounted by the loader.
        for plugin_name in dict.fromkeys(replacements.values()):
            entry = _BY_NAME.get(plugin_name)
            if entry is None or any(p.name == entry.name for p in chosen):
                continue
            chosen.append(entry)

    return tuple(chosen)
