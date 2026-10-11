"""The core services, named, and what each one is for.

Why this exists
---------------
The container already holds these: ``RuntimeComponents.services`` is a
:class:`~agent.kernel.service.ServiceRegistry` that **adopts** the component
mapping, so ``services.get("agent_core")`` has always returned the real object.
What was missing is that nothing *said* which names are services.  They travelled
as string keys through a plain ``dict``, read at sixty-odd call sites, so:

- nothing distinguished a service from a bootstrap by-product (``mcp_task``,
  ``use_memory_worker_pool``, ``_shares_global_runtime``);
- a reader could not tell whether a name was public vocabulary or a private
  convenience, and therefore whether replacing it was supported;
- a typo produced ``None``, which for a service is indistinguishable from a
  composition that legitimately lacks it.

This module is the vocabulary.  It is a declaration, not a new mechanism: the
mechanism is ``RuntimeComponents.require``, which resolves through the registry and
raises for a missing name instead of returning ``None``.

Why each entry carries an owner
-------------------------------
Because "who fills this" is the question that makes a service *replaceable*.  A
name with no owner cannot be substituted knowingly -- a plugin author has nothing
to look up.  It is also the check that catches drift: the guard in
``tests/test_core_services.py`` asserts every declared owner really writes it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CoreService",
    "CORE_SERVICES",
    "BY_NAME",
    "core_service_names",
    "entry_for",
]


@dataclass(frozen=True)
class CoreService:
    """One service the runtime resolves by name.

    ``owners`` is every module that writes it into a component mapping.  Plural
    because four of these have more than one writer, which is the same
    scattered-construction debt the scheduler's stores had -- recorded rather than
    hidden, because a service whose value depends on who ran last is not one a
    plugin can knowingly replace.

    ``consumers`` names the modules that read it: the list a re-implementation has
    to satisfy, and the reason this is a ledger rather than a set of constants.
    """

    name: str
    owners: tuple[str, ...]
    consumers: tuple[str, ...]
    summary: str
    #: Where the *contract* for this service is written down, when one is.
    #: A name with an owner says who fills it; a contract says what a different
    #: implementation has to satisfy to fill it instead.  Empty for services whose
    #: interface is still implicit -- which is a fact worth being able to see.
    contract: str = ""


CORE_SERVICES: tuple[CoreService, ...] = (
    CoreService(
        name="registry",
        owners=(
            "agent/bootstrap.py",
            "agent/tools/plugin_tools.py",
        ),
        consumers=(
            "agent/_builtin/plugins/evolution/__init__.py",
            "agent/bootstrap.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/plugins/catalog.py",
            "agent/runtime/contracts.py",
            "agent/runtime/system_prompt.py",
            "agent/core/agent_plugin.py",  # the agent's tool registry
            "agent/scheduler/plugin.py",  # a scheduled run's tools come from it
            "agent/tools/plugin.py",  # the built-in tools register on it
        ),
        summary="The tool registry: definitions, capabilities, and the context keys below.",
    ),
    CoreService(
        name="skill_catalog",
        owners=(
            "agent/bootstrap.py",
            "agent/tools/plugin_tools.py",
        ),
        consumers=(
            "agent/_builtin/plugins/evolution/__init__.py",
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/channels/web.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/commands/composition.py",
            "agent/plugins/catalog.py",
            "agent/runtime/contracts.py",
            "agent/runtime/system_prompt.py",
            "agent/ralph/plugin.py",  # a task may be driven by a skill
        ),
        summary="Loaded skills, and the dirty flag that decides when the prompt is stale.",
    ),
    CoreService(
        name="plugin_catalog",
        owners=(
            "agent/bootstrap.py",
            "agent/cli.py",
        ),
        consumers=(
            "agent/_builtin/plugins/evolution/__init__.py",
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/channels/web.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/commands/composition.py",
            "agent/config.py",
            "agent/runtime/contracts.py",
            "agent/runtime/system_prompt.py",
            "agent/core/agent_plugin.py",  # the agent reaches the composition through it
        ),
        summary="Loaded plugins and everything they contributed.",
    ),
    CoreService(
        name="agent",
        owners=(
            "agent/core/agent_plugin.py",
            # A session builds its own agent, from the config on disk -- that is what a
            # rebuild is for -- so `bootstrap` writes this name too.
            "agent/bootstrap.py",
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/runtime/contracts.py",
            "agent/_builtin/plugins/evolution/__init__.py",  # the engine scores a turn against the agent's provider
            "agent/ralph/plugin.py",  # the task loop drives an agent per task
            "agent/scheduler/build.py",  # a scheduled run is a turn, so it needs one
        ),
        summary="The agent a turn runs on: the object that answers a user message.",
    ),
    CoreService(
        name="context_manager",
        owners=(
            "agent/memory/session_plugin.py",
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/channels/web.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/config.py",
            "agent/core/agent_plugin.py",  # the agent assembles context through it
            "agent/ralph/plugin.py",  # a task turn records through it
            "agent/scheduler/build.py",  # a scheduled run records through it
            "agent/tools/plugin.py",  # the built-in tools read and write session context
        ),
        summary="The memory system: staging, retrieval, consolidation, per session.",
        contract="agent/memory/contracts.py:ContextService",
    ),
    CoreService(
        name="ltm_store",
        owners=(
            "agent/memory/plugin.py",
            "agent/memory/backends/sqlite_fts.py",  # the opt-in alternative backend
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/memory/session_plugin.py",
        ),
        summary=(
            "The long-term store the palace and the context manager share.  "
            "Replacing it alone swaps storage while keeping this project's "
            "retrieval, consolidation and compaction."
        ),
        contract="agent/memory/contracts.py:MemoryStore",
    ),
    CoreService(
        name="agent_core",
        owners=(
            "agent/bootstrap.py",
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/runtime/contracts.py",
            "agent/commands/builtin.py",
        ),
        summary="The turn runner's owner: skill refresh, maintenance, session state.",
    ),
    CoreService(
        name="agent_loop",
        owners=(
            "agent/runtime/contracts.py",
        ),
        consumers=(
            "agent/runtime/contracts.py",
        ),
        summary=(
            "Executes one turn against the agent, with continuation accounting. "
            "Provided by the TurnRunner plugin, which is why its owner is the "
            "module that defines it rather than the composition root: the loop "
            "offers itself, the root only mounts it."
        ),
    ),
    CoreService(
        name="llm",
        owners=(
            "agent/core/model_plugin.py",  # the process-wide routing transport
            "agent/bootstrap.py",  # the session-scoped transport of a web session
        ),
        consumers=(
            "agent/core/agent.py",
            "agent/core/agent_plugin.py",  # the agent's transport
        ),
        contract="agent/core/transport.py:ModelTransport",
        summary=(
            "The model adapter: the one place provider dispatch happens.  An ABC "
            "with three implementations, so the seam existed before the container "
            "did; naming it is what lets a composition choose one without editing "
            "the code that builds the agent."
        ),
    ),
    CoreService(
        name="memory",
        owners=(
            "agent/memory/plugin.py",
            "agent/memory/backends/sqlite_fts.py",  # the opt-in alternative backend
            "agent/config.py",
        ),
        consumers=(
            "agent/_builtin/plugins/evolution/__init__.py",  # experience is scored against stored memory
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/scheduler/plugin.py",  # a scheduled run consolidates into it
            "agent/tools/plugin.py",  # the memory tools
        ),
        summary="The long-term store behind the context manager.",
        contract="agent/memory/contracts.py:MemoryStore",
    ),
    CoreService(
        name="mcp_client",
        owners=(
            "agent/bootstrap.py",
            "agent/tools/plugin_tools.py",
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/config.py",
            "agent/plugins/catalog.py",
        ),
        summary="The MCP client, when a composition connected any servers.",
    ),
)

BY_NAME: dict[str, CoreService] = {entry.name: entry for entry in CORE_SERVICES}


def core_service_names() -> tuple[str, ...]:
    return tuple(entry.name for entry in CORE_SERVICES)


def entry_for(name: str) -> CoreService:
    """The ledger entry for *name*.  Raises for an undeclared one.

    Loud rather than returning a blank entry: reaching this with an undeclared
    name is the drift the ledger exists to catch.
    """
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a declared core service; add it to CORE_SERVICES in "
            f"agent/runtime/core_services.py with its owner and consumers"
        ) from None
