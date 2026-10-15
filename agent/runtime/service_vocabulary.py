"""Which service names a **plugin** may touch, and which belong to the composition.

Why this file exists
--------------------
``ServiceRegistry.adopt`` makes the plugin registry and the runtime's component
mapping **one storage**, which is what lets a plugin replace a service the runtime
resolves.  The cost is that everything else the composition puts in that mapping --
``mcp_task``, ``config_revision``, a session's staged components -- becomes
reachable by the same one-line ``ctx.get``.  Those values have no contract, no
declared lifetime and no reason for a plugin to depend on them; before this file
there was nothing that said so, and the ledger (``CORE_SERVICES``) could not say it
either, because it only describes names somebody bothered to write down.

So there are two questions, and a closed vocabulary needs both answers:

* *did this plugin declare the name?* -- ``inject`` / ``optional``; enforced by
  ``Context.require_declared``, and it is what the unload/replace cascade walks.
* *is the name a plugin's to use at all?* -- this file; enforced by
  ``Context.require_allowed`` through ``PluginHost.service_policy``.

Why the kernel does not hold this list
--------------------------------------
A vocabulary compiled into ``agent/kernel/`` would be the domain coupling that
layer exists to prevent: the kernel would have to know that ``mcp_task`` is an
internal handle and ``registry`` is a service.  The kernel takes a *policy*
instead, and this module is the composition's answer.  A ``PluginCatalog`` built
without one keeps the permissive behaviour, which is why the kernel's own tests can
still use invented names like ``greeter``.

How the list is kept honest
---------------------------
It is **derived, not trusted**.  ``tests/test_service_vocabulary.py`` scans every
plugin module for the names it declares, reads and provides, and fails if any of
them is missing here -- and fails in the other direction too, if a name here is used
by no plugin and provided by none.  Adding a name to this file without a user, or
using a name without adding it, is a test failure rather than a silent widening.
"""

from __future__ import annotations

__all__ = [
    "PUBLIC_SERVICES",
    "RESERVED_COMPONENT_KEYS",
    "RESERVED_REASONS",
    "FOR_THIRD_PARTIES",
    "is_public",
    "plugin_service_policy",
]


#: Every name a plugin may read or provide.  Grouped by where the value comes from,
#: because that is the question a reader of a plugin's ``inject`` is asking.
PUBLIC_SERVICES: frozenset[str] = frozenset(
    {
        # -- configuration and the model endpoint: written by the composition,
        #    read by almost every plugin.  A plugin cannot resolve its own provider.
        "cfg",
        "client",
        "model",
        "api_format",
        "max_tokens",
        "client_cache_key",
        # -- paths the composition decides: a plugin writes files, it does not
        #    choose the workspace.
        "workspace_root",
        "output_dir",
        "resource_root",
        "user_skills_dir",
        "user_plugins_dir",
        "user_tools_dir",
        "file_service",
        # -- the buses.  A typed subscription resolves its domain's bus by name
        #    (``agent.core.event_domains.bus_for``), so the bus is a read like any
        #    other and is listed here for the same reason.
        "event_bus.tool",
        "event_bus.turn",
        "event_bus.session",
        "event_bus.llm",
        # -- capability services: what a plugin contributes *to*, and what its
        #    scoped view comes from.
        "registry",
        "skill_catalog",
        "channels",
        "contributed_mcp_servers",
        "commands",
        "agents",
        # -- the services a plugin may provide, and therefore may also read back.
        "agent",
        "agent_loop",
        "llm",
        "provider_client_cache",
        "memory",
        "ltm_store",
        "context_manager",
        "plugin_catalog",
        "mcp_client",
        "mcp_server_configs",
        "mcp_status",
        "ralph_service",
        "scheduler",
        "evolution",
        "plugin.meta",
        # -- per-session facts the composition stages for the plugins that need
        #    them (a role registry's prompt, the session a run belongs to).
        "system_prompt",
        "post_turn_maintenance",
        "session_id",
        "runtime_factory",
        "scheduler_settings",
        "scheduler_config_loader",
    }
)


#: Names that live in the same mapping and are **not** a plugin's to touch, each
#: with the reason.  Recorded rather than merely absent, so "not listed" cannot be
#: confused with "nobody thought about it".
RESERVED_REASONS: dict[str, str] = {
    "mcp_task": (
        "the background task that connects MCP servers; an asyncio.Task owned by the "
        "composition, with no contract and no lifetime a plugin could reason about"
    ),
    "use_memory_worker_pool": "a composition decision, not a service",
    "_shares_global_runtime": "a composition-internal flag",
    "config_revision": (
        "a counter the web channel bumps so open pages reload; bumped by whoever "
        "wrote the config, meaningless to a plugin"
    ),
    "session_components": (
        "a session's staged component mapping, held by the channel runner; reaching "
        "it would make a plugin a second owner of session state"
    ),
    "command_router": "the command dispatcher the composition installs",
    "command_coordinator_factory": "a factory the channel runner calls",
    "session_components_factory": "a factory the channel runner calls",
    "session_store_factory": "a factory the channel runner calls",
    "base_system_prompt": (
        "the un-composed prompt the composition keeps so it can re-publish; "
        "`system_prompt` is the composed one a plugin reads"
    ),
    "file_access_policy": "the composition's resolved file policy, not a plugin input",
    "workspace": "the resolved workspace descriptor, not a service",
    "permissions": "the composition's resolved permission state",
    "shell_sandbox": "the composition's resolved shell posture",
    "shell_level": "the composition's resolved shell posture",
    "shell_allowed_commands": "the composition's resolved shell posture",
    "shell_blocked_commands": "the composition's resolved shell posture",
    "shell_secret_paths": "the composition's resolved shell posture",
    "shell_devices": "the composition's resolved shell posture",
    "user_tools_enabled": "a composition decision",
    "user_tool_catalog": "a composition-built catalog",
    "plugin_failures": "a report the composition writes after mounting",
    "plugin_pending": "a report the composition writes after mounting",
    "memory_worker_pool": "a composition-owned pool",
    "orphan_recovery_worker": "a composition-owned worker",
    "turn_request": "per-turn state the turn loop writes on the tool registry",
    "eval_agent": "an evaluation-only seam",
}


def is_public(name: str) -> bool:
    return name in PUBLIC_SERVICES


def plugin_service_policy(name: str, action: str) -> bool:
    """``PluginHost.service_policy``: may a plugin touch *name*?

    *action* is ``"read"`` or ``"provide"``.  The two are answered the same way
    today -- a name in the vocabulary may be either -- but the parameter is in the
    signature because a composition may reasonably want to allow reading one
    without allowing a plugin to *replace* it, and a policy that had to change
    shape to express that would change the kernel with it.
    """
    return name in PUBLIC_SERVICES


#: The reserved names, as a set, derived from the reasons so the two cannot drift.
RESERVED_COMPONENT_KEYS: frozenset[str] = frozenset(RESERVED_REASONS)


#: Vocabulary a **third-party** plugin is documented to use, with no in-tree user
#: yet.  Kept apart from :data:`PUBLIC_SERVICES`'s evidence base so the guard can be
#: strict about both directions: every other entry has to be used by a plugin in this
#: tree, and these two have to carry a reason instead.  Without the separation, the
#: "no dead vocabulary" half would either be dropped (and the list could grow
#: unchecked) or would force deleting a documented extension path.
FOR_THIRD_PARTIES: dict[str, str] = {
    "commands": (
        "the documented way for a plugin to add a slash command: "
        "`inject = (\"commands\",)` then `ctx.commands.add(name, handler)`.  In-tree "
        "bundles go through the loader's markdown adapter instead, so nothing here "
        "reads it yet -- tests/test_service_vocabulary.py exercises the view."
    ),
    "agents": (
        "the same shape for sub-agent roles: `inject = (\"agents\",)` then "
        "`ctx.agents.add(...)`.  Roles shipped with a bundle are registered by the "
        "loader, so no in-tree plugin uses the view yet."
    ),
}
