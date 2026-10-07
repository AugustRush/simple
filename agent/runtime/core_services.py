"""The core services, named, and what each one is for.

Why this exists
---------------
The container already holds these: ``RuntimeComponents.services`` is a
:class:`~agent.runtime.services.ServiceRegistry` that **adopts** the component
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


CORE_SERVICES: tuple[CoreService, ...] = (
    CoreService(
        name="registry",
        owners=(
            "agent/bootstrap.py",
            "agent/tools/builtin_tools.py",
        ),
        consumers=(
            "agent/_builtin/plugins/evolution/__init__.py",
            "agent/bootstrap.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/plugins/catalog.py",
            "agent/runtime/contracts.py",
            "agent/runtime/system_prompt.py",
        ),
        summary="The tool registry: definitions, capabilities, and the context keys below.",
    ),
    CoreService(
        name="skill_catalog",
        owners=(
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/cli.py",
            "agent/runtime/contracts.py",
            "agent/tools/builtin_tools.py",
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
        ),
        summary="Loaded plugins and everything they contributed.",
    ),
    CoreService(
        name="context_manager",
        owners=(
            "agent/bootstrap.py",
            "agent/runtime/contracts.py",
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/channels/web.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
            "agent/config.py",
        ),
        summary="The memory system: staging, retrieval, consolidation, per session.",
    ),
    CoreService(
        name="agent_core",
        owners=(
            "agent/bootstrap.py",
        ),
        consumers=(
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
        ),
        summary="The turn runner's owner: skill refresh, maintenance, session state.",
    ),
    CoreService(
        name="turn_runner",
        owners=(
            "agent/bootstrap.py",
        ),
        consumers=(
            "agent/runtime/contracts.py",
        ),
        summary="Executes one turn against AgentCore, with continuation accounting.",
    ),
    CoreService(
        name="memory",
        owners=(
            "agent/bootstrap.py",
            "agent/config.py",
        ),
        consumers=(
            "agent/_builtin/plugins/evolution/__init__.py",
            "agent/bootstrap.py",
            "agent/channels/base.py",
            "agent/cli.py",
            "agent/commands/builtin.py",
        ),
        summary="The long-term store behind the context manager.",
    ),
    CoreService(
        name="mcp_client",
        owners=(
            "agent/bootstrap.py",
            "agent/tools/builtin_tools.py",
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
