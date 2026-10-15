"""Every name the runtime resolves out of its component mapping -- one declaration.

Why one file, and why so little on each entry
---------------------------------------------
There used to be two, and they overlapped without either implying the other:

* ``core_services.py`` -- 15 entries with owners, consumers and a contract;
* ``service_vocabulary.py`` -- 44 public names and 27 reserved ones, with reasons.

Which meant that "is this name a service?" had two answers, that the ``memory`` entry
could point at the wrong protocol for months with nothing asking, and that a plugin's
vocabulary and the runtime's ledger could each grow without the other noticing.

The deeper problem was the *shape*.  Every entry declared ``owners`` (who writes it) and
``consumers`` (who reads it) -- facts that are **derivable from the source** and were
therefore a hand-maintained mirror of it, kept in step by a 1900-line AST scanner and
several guards whose only job was to notice when the mirror fogged.  A declaration that
restates the code is a copy, and a copy drifts.

So this table declares only what the source cannot say:

* ``plugin_visible`` -- may a *plugin* touch it (the rest is the composition's);
* ``contract`` -- the protocol an implementation must satisfy, when there is one;
* ``summary`` -- what it is, in a sentence, for a person reading a plugin's ``inject``;
* ``reason`` -- for the composition-internal names, why a plugin must not have it.

Who writes and reads each name is *computed* from the source by the scanner in
``tests/test_core_services.py`` -- analysis belongs in a guard, not in a module the
runtime imports -- and checked there: an entry is here because the source writes it,
a source write is here because it is declared, and a name a plugin touches has to be
``plugin_visible``.  That direction -- code is the truth, the declaration is the policy --
is the one that cannot drift.

Three states, not two
---------------------
A name is one of:

* a **service** -- something a plugin may depend on and, for most of them, provide;
* a **value** -- the composition computed it (a path, a config section, a resolved
  policy) and plugins read it, but nobody "provides" it and there is no protocol;
* **reserved** -- it lives in the same mapping and is not a plugin's to touch.

The middle state is why the old ledger could not simply absorb the vocabulary: ``cfg``
and ``workspace_root`` are not services, and calling them services to avoid a second list
would have made "declared contract" meaningless.  Here they are declared as what they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "ServiceDeclaration",
    "SERVICES",
    "CONTRACTED_SERVICES",
    "BY_NAME",
    "entry_for",
    "is_public",
    "plugin_service_policy",
    "public_names",
    "reserved_reasons",
]


@dataclass(frozen=True)
class ServiceDeclaration:
    """One name in the component mapping, and everything about it the source cannot say.

    Frozen because it is a declaration: a mutable ledger is one somebody edits at
    runtime, and then "which services exist" depends on when you asked.
    """

    name: str
    #: Whether a **plugin** may read or provide it.  ``False`` means the composition's
    #: own -- see ``reason``, which says why.
    plugin_visible: bool
    #: ``agent/path/to/module.py:Protocol`` a provided implementation must satisfy.
    #: Empty for a *value*: the composition computed it, so there is nothing to conform.
    contract: str = ""
    #: One sentence for a person reading a plugin's ``inject`` list.
    summary: str = ""
    #: For ``plugin_visible=False``: why a plugin must not touch it.  Required there --
    #: "reserved" without a reason is indistinguishable from "forgotten".
    reason: str = ""

    def __post_init__(self) -> None:
        # Refused at construction, not only in ``_validate_declarations``: a declaration
        # built anywhere -- a test, a fork, a profile -- has to carry the same two facts,
        # and a rule enforced only over this module's own tuple is a rule about this file
        # rather than about the type.
        if not self.name.strip():
            raise ValueError("a service declaration needs a name")
        if not self.plugin_visible and not self.reason.strip():
            raise ValueError(
                f"{self.name!r} is reserved with no reason; 'reserved' and 'forgotten' "
                f"have to be distinguishable"
            )


#: **The** declaration.  Ordered by state, then by name, so a diff reads as a change to
#: one of the three groups rather than as a reshuffle.
SERVICES: tuple[ServiceDeclaration, ...] = (
    # ── Services: a protocol, a provider, and a plugin that may be it ────────────
    ServiceDeclaration(
        name="registry",
        plugin_visible=True,
        contract="agent/tools/protocols.py:ToolRegistryService",
        summary="The tool registry: definitions, capabilities, and the context keys below.",
    ),
    ServiceDeclaration(
        name="skill_catalog",
        plugin_visible=True,
        contract="agent/skills/protocols.py:SkillCatalogService",
        summary="Loaded skills, and the dirty flag that decides when the prompt is stale.",
    ),
    ServiceDeclaration(
        name="plugin_catalog",
        plugin_visible=True,
        contract="agent/plugins/protocols.py:PluginCatalogService",
        summary="Loaded plugins and everything they contributed.",
    ),
    ServiceDeclaration(
        name="agent",
        plugin_visible=True,
        contract="agent/core/protocols.py:AgentService",
        summary="The agent a turn runs on: the object that answers a user message.",
    ),
    ServiceDeclaration(
        name="agent_loop",
        plugin_visible=True,
        contract="agent/runtime/protocols.py:AgentLoopService",
        summary=(
            "Executes one turn against the agent, with continuation accounting.  The "
            "loop offers itself, so a composition replaces it by providing the name."
        ),
    ),
    ServiceDeclaration(
        name="context_manager",
        plugin_visible=True,
        contract="agent/memory/contracts.py:ContextService",
        summary="The memory system: staging, retrieval, consolidation, per session.",
    ),
    ServiceDeclaration(
        name="ltm_store",
        plugin_visible=True,
        contract="agent/memory/contracts.py:MemoryStore",
        summary=(
            "The long-term store the palace and the context manager share.  Replacing it "
            "alone swaps storage while keeping this project's retrieval, consolidation "
            "and compaction."
        ),
    ),
    ServiceDeclaration(
        name="memory",
        plugin_visible=True,
        contract="agent/memory/contracts.py:MemoryService",
        summary="The memory palace: the long-term store's reading and writing surface.",
    ),
    ServiceDeclaration(
        name="llm",
        plugin_visible=True,
        contract="agent/core/transport.py:ModelTransport",
        summary=(
            "The model adapter: the one place provider dispatch happens.  An ABC with "
            "three implementations, so naming it is what lets a composition choose one "
            "without editing the code that builds the agent."
        ),
    ),
    ServiceDeclaration(
        name="channels",
        plugin_visible=True,
        contract="agent/channels/protocols.py:ChannelRegistryService",
        summary="The channel registry: the capability a channel plugin contributes to.",
    ),
    ServiceDeclaration(
        name="contributed_mcp_servers",
        plugin_visible=True,
        contract="agent/tools/protocols.py:MCPServerRegistryService",
        summary=(
            "The MCP server configs plugins contribute, attributed and unwound with "
            "their scopes.  Named for what it holds because ``mcp_servers`` is the MCP "
            "config's own field."
        ),
    ),
    ServiceDeclaration(
        name="mcp_client",
        plugin_visible=True,
        contract="agent/tools/protocols.py:MCPClientService",
        summary=(
            "The MCP client, when a composition connected any servers.  Absent -- never "
            "``None`` -- when there is nothing to connect."
        ),
    ),
    ServiceDeclaration(
        name="commands",
        plugin_visible=True,
        contract="agent/commands/protocols.py:CommandRegistryService",
        summary=(
            "The slash-command registry.  A **floor**: the loader registers a bundle's "
            "commands while loading it and restores this registry on a reload rollback, "
            "so it must exist before any plugin activates.  The *contribution* goes "
            "through ``ctx.commands`` like every other kind."
        ),
    ),
    ServiceDeclaration(
        name="agents",
        plugin_visible=True,
        contract="agent/core/protocols.py:AgentRegistryService",
        summary=(
            "The sub-agent role registry.  A floor for the same reasons as ``commands``."
        ),
    ),
    ServiceDeclaration(
        name="agent_core",
        plugin_visible=False,
        contract="agent/runtime/protocols.py:AgentCoreService",
        summary=(
            "The turn runner's owner: skill refresh, maintenance, session state.  It *is* "
            "a view of the composition -- it resolves services at call time and publishes "
            "the system prompt through the whole mapping."
        ),
        reason=(
            "a facade over the composition itself.  Handing it to a plugin would make "
            "\"which services does this plugin need\" unanswerable, which is the property "
            "the plugin model rests on -- so no plugin may read or replace it"
        ),
    ),
    # ── Values: the composition computed them, plugins read them ─────────────────
    ServiceDeclaration(
        name="cfg",
        plugin_visible=True,
        summary="The config the composition loaded, as a dict plugins read sections from.",
    ),
    ServiceDeclaration(
        name="client",
        plugin_visible=True,
        summary="The provider SDK client the active provider's transport was built with.",
    ),
    ServiceDeclaration(
        name="client_cache_key",
        plugin_visible=True,
        summary=(
            "Which cached provider client a transport should reuse.  Absent means \"trust "
            "the inherited client\"."
        ),
    ),
    ServiceDeclaration(
        name="provider_client_cache",
        plugin_visible=True,
        summary="The process-lifetime cache of provider clients behind ``llm``.",
    ),
    ServiceDeclaration(
        name="model",
        plugin_visible=True,
        summary="The model id the active provider resolves to.",
    ),
    ServiceDeclaration(
        name="api_format",
        plugin_visible=True,
        summary="The wire format of the active provider: ``anthropic`` or ``openai``.",
    ),
    ServiceDeclaration(
        name="max_tokens",
        plugin_visible=True,
        summary="The active provider's completion cap.",
    ),
    ServiceDeclaration(
        name="workspace_root",
        plugin_visible=True,
        summary="The workspace a turn may read and write.",
    ),
    ServiceDeclaration(
        name="output_dir",
        plugin_visible=True,
        summary="Where a turn's artifacts go, inside the workspace.",
    ),
    ServiceDeclaration(
        name="resource_root",
        plugin_visible=True,
        summary="The agent home resources are read from (prompts, builtin skills).",
    ),
    ServiceDeclaration(
        name="user_plugins_dir",
        plugin_visible=True,
        summary="The user-level plugin directory (``~/.agent/plugins``).",
    ),
    ServiceDeclaration(
        name="user_skills_dir",
        plugin_visible=True,
        summary="The user-level skill directory (``~/.agent/skills``).",
    ),
    ServiceDeclaration(
        name="user_tools_dir",
        plugin_visible=True,
        summary="The user-level tool directory (``~/.agent/tools``).",
    ),
    ServiceDeclaration(
        name="file_service",
        plugin_visible=True,
        summary="The resolved file provider: the workspace/output access policy in force.",
    ),
    ServiceDeclaration(
        name="session_id",
        plugin_visible=True,
        summary="The session a run belongs to.",
    ),
    ServiceDeclaration(
        name="system_prompt",
        plugin_visible=True,
        summary="The composed system prompt every participant contributed to.",
    ),
    ServiceDeclaration(
        name="post_turn_maintenance",
        plugin_visible=True,
        summary="The work the composition owes after a turn (consolidation, compaction).",
    ),
    ServiceDeclaration(
        name="runtime_factory",
        plugin_visible=True,
        summary="Builds a runtime for a run that found a changed config on disk.",
    ),
    ServiceDeclaration(
        name="scheduler_settings",
        plugin_visible=True,
        summary="Resolved scheduler settings, when the caller overrode the config.",
    ),
    ServiceDeclaration(
        name="scheduler_config_loader",
        plugin_visible=True,
        summary="Loads a named schedule bundle, when the caller named one.",
    ),
    ServiceDeclaration(
        name="event_bus.tool",
        plugin_visible=True,
        summary="The composition's bus for tool events.",
    ),
    ServiceDeclaration(
        name="event_bus.turn",
        plugin_visible=True,
        summary="The composition's bus for turn events.",
    ),
    ServiceDeclaration(
        name="event_bus.session",
        plugin_visible=True,
        summary="The composition's bus for session events.",
    ),
    ServiceDeclaration(
        name="event_bus.llm",
        plugin_visible=True,
        summary="The composition's bus for model-call events.",
    ),
    # Services a plugin provides, listed with the values because their declarations are
    # equally thin -- the contract is what matters and it is above.
    ServiceDeclaration(
        name="scheduler",
        plugin_visible=True,
        summary="The scheduler service: tasks, runs and their history.",
    ),
    ServiceDeclaration(
        name="ralph_service",
        plugin_visible=True,
        summary="The task loop: drives a task to completion against its criteria.",
    ),
    ServiceDeclaration(
        name="evolution",
        plugin_visible=True,
        summary=(
            "The correction-learning engine.  The evolution plugin reads it (a "
            "composition may supply its own) and provides it when nobody did."
        ),
    ),
    ServiceDeclaration(
        name="mcp_server_configs",
        plugin_visible=True,
        summary="The merged MCP server list the client connects: configured plus contributed.",
    ),
    ServiceDeclaration(
        name="mcp_status",
        plugin_visible=True,
        summary="What the MCP client reports about the servers it was asked to connect.",
    ),
    ServiceDeclaration(
        name="plugin.meta",
        plugin_visible=True,
        summary="The manifest of the plugin being set up, while its ``setup`` runs.",
    ),
    # ── Reserved: in the same mapping, and not a plugin's to touch ───────────────
    ServiceDeclaration(
        name="mcp_task",
        plugin_visible=False,
        reason=(
            "the background task connecting MCP servers: an ``asyncio.Task`` owned by the "
            "composition, with no contract and no lifetime a plugin could reason about"
        ),
    ),
    ServiceDeclaration(
        name="use_memory_worker_pool",
        plugin_visible=False,
        reason="a composition decision about how memory work is scheduled",
    ),
    ServiceDeclaration(
        name="_shares_global_runtime",
        plugin_visible=False,
        reason="a composition-internal flag",
    ),
    ServiceDeclaration(
        name="config_revision",
        plugin_visible=False,
        reason=(
            "a counter the web channel bumps so open pages reload; bumped by whoever wrote "
            "the config, meaningless to a plugin"
        ),
    ),
    ServiceDeclaration(
        name="command_router",
        plugin_visible=False,
        reason="the command dispatcher the composition installs",
    ),
    ServiceDeclaration(
        name="command_coordinator_factory",
        plugin_visible=False,
        reason="a factory the channel runner calls",
    ),
    ServiceDeclaration(
        name="session_components_factory",
        plugin_visible=False,
        reason="a factory the channel runner calls",
    ),
    ServiceDeclaration(
        name="session_store_factory",
        plugin_visible=False,
        reason="a factory the channel runner calls",
    ),
    ServiceDeclaration(
        name="base_system_prompt",
        plugin_visible=False,
        reason=(
            "the un-composed prompt the composition keeps so it can re-publish; "
            "``system_prompt`` is the composed one a plugin reads"
        ),
    ),
    ServiceDeclaration(
        name="file_access_policy",
        plugin_visible=False,
        reason="the composition's resolved file policy, not a plugin input",
    ),
    ServiceDeclaration(
        name="workspace",
        plugin_visible=False,
        reason="the resolved workspace descriptor, not a service",
    ),
    ServiceDeclaration(
        name="permissions",
        plugin_visible=False,
        reason="the composition's resolved permission state",
    ),
    ServiceDeclaration(
        name="shell_allowed_commands",
        plugin_visible=False,
        reason="the composition's resolved shell posture",
    ),
    ServiceDeclaration(
        name="shell_blocked_commands",
        plugin_visible=False,
        reason="the composition's resolved shell posture",
    ),
    ServiceDeclaration(
        name="shell_secret_paths",
        plugin_visible=False,
        reason="the composition's resolved shell posture",
    ),
    ServiceDeclaration(
        name="shell_devices",
        plugin_visible=False,
        reason="the composition's resolved shell posture",
    ),
    ServiceDeclaration(
        name="user_tools_enabled",
        plugin_visible=False,
        reason="a composition decision about whether user tools load at all",
    ),
    ServiceDeclaration(
        name="user_tool_catalog",
        plugin_visible=False,
        reason="a composition-built catalog of user tools",
    ),
    ServiceDeclaration(
        name="plugin_failures",
        plugin_visible=False,
        reason="a report the composition writes after mounting, for the CLI and gateway",
    ),
    ServiceDeclaration(
        name="plugin_pending",
        plugin_visible=False,
        reason="a report the composition writes after mounting, for the CLI and gateway",
    ),
    ServiceDeclaration(
        name="memory_worker_pool",
        plugin_visible=False,
        reason="a composition-owned pool for memory work",
    ),
    ServiceDeclaration(
        name="orphan_recovery_worker",
        plugin_visible=False,
        reason="a composition-owned worker for orphaned staging recovery",
    ),
    ServiceDeclaration(
        name="turn_request",
        plugin_visible=False,
        reason="per-turn state the turn loop writes on the tool registry",
    ),
)


BY_NAME: dict[str, ServiceDeclaration] = {entry.name: entry for entry in SERVICES}

#: The entries that name a protocol.  A contract is what makes reimplementation
#: checkable, so these are the ones `agent/runtime/service_contracts.py` audits and
#: `tests/test_service_contracts.py` builds rows for.
CONTRACTED_SERVICES: tuple[ServiceDeclaration, ...] = tuple(
    entry for entry in SERVICES if entry.contract
)


def entry_for(name: str) -> ServiceDeclaration:
    """The declaration for *name*.  Raises for an undeclared one.

    Loud rather than returning a blank entry: reaching this with an undeclared name is
    the drift this table exists to catch.
    """
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a declared service; add it to SERVICES in "
            f"agent/runtime/services.py with its visibility and (if it has one) contract"
        ) from None


def is_public(name: str) -> bool:
    entry = BY_NAME.get(name)
    return entry is not None and entry.plugin_visible


def plugin_service_policy(name: str, action: str) -> bool:
    """``PluginHost.service_policy``: may a plugin touch *name*?

    *action* is ``"read"`` or ``"provide"``.  Both are answered the same way today -- a
    name a plugin may see is a name it may offer -- but the parameter is in the
    signature because a composition may reasonably want to allow reading one without
    allowing a plugin to *replace* it, and a policy that had to change shape to express
    that would change the kernel with it.
    """
    return is_public(name)


def public_names() -> frozenset[str]:
    """Every name a plugin may touch.  Derived, never listed twice."""
    return frozenset(entry.name for entry in SERVICES if entry.plugin_visible)


def reserved_reasons() -> dict[str, str]:
    """``name -> why a plugin must not touch it``, for the composition-internal set."""
    return {
        entry.name: entry.reason for entry in SERVICES if not entry.plugin_visible
    }


def _validate_declarations() -> None:
    """Fail at import for the two mistakes this table can make on its own.

    A duplicate name makes ``BY_NAME`` silently keep the last one, and a reserved entry
    with no reason is indistinguishable from a forgotten one.  Both are cheap to check
    and expensive to find later, and neither needs the source tree to answer.
    """
    seen: set[str] = set()
    for entry in SERVICES:
        if entry.name in seen:
            raise ValueError(f"{entry.name!r} is declared twice")
        seen.add(entry.name)
        if not entry.plugin_visible and not entry.reason:
            raise ValueError(f"{entry.name!r} is reserved with no reason given")


_validate_declarations()
