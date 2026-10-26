"""One reload: unload the tree, re-discover it, and undo the whole thing if a step fails.

Why this is a collaborator
--------------------------
``PluginCatalog`` had grown to own discovery, two registries, the hook surface, the
declarative-asset adapter *and* this transaction, which is the one piece with a
**lifetime** rather than a job: it takes state, changes it in several places, and owes an
undo for all of them.  Keeping that inside the loader meant the loader's other methods
could change the same state without the transaction noticing, and it made "what does a
failed reload restore?" a question about a 140-line method rather than about a thing.

What the undo owes, and the bug it was hiding
---------------------------------------------
The rollback used to *rebind* the tables it had snapshotted::

    self._plugins = old_discovery_state["plugins"]

which was invisible while the only reader was ``self._plugins``.  It stopped being
invisible when the hook dispatcher was handed the **live** table by reference -- after
that, a rolled-back catalog kept dispatching to the dict it had just replaced, so hooks
ran against plugins that were no longer installed.  Nothing failed loudly; the reload
"restored" and the hooks pointed at the old tree.

So the rule here is: **restore mutates in place, never rebinds.**  A registered snapshot
is only correct if every holder of the thing sees the restored value, and rebinding is
precisely how a holder that was handed the object stops seeing it.

What it does *not* own
----------------------
The registries' own snapshot formats (``CommandRegistry.snapshot``/``restore``,
``AgentRegistry``'s) and the re-discovery itself.  This class decides *when* they are taken
and put back, and what a failure means; they know how.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent import shared

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # the loader imports this module: types only, so no cycle
    from agent.plugins.catalog import PluginCatalog

__all__ = ["ReloadTransaction"]


def _mcp_server_came_up(mcp_client: Any, config: dict) -> bool:
    """Whether *config*'s server is connected now, when the client can say.

    A client without ``is_connected`` (a substitution in a test, an older
    composition) cannot tell a failed connection from a working one, so the
    attempt is reported -- which is all that client ever reported.
    """
    probe = getattr(mcp_client, "is_connected", None)
    if probe is None:
        return True
    return bool(probe(str(config.get("name", "?"))))


async def _stop_mcp_server(mcp_client: Any, server_name: str, registry: Any) -> None:
    """Stop one removed plugin's MCP server, then make sure its tools are gone.

    ``disconnect`` is the real stop: it sets the server's own stop event and
    awaits the task that owns the session, so the child process is reaped here
    and not at process exit.  The prefix removal afterwards is the fallback for a
    client that has no ``disconnect`` and the belt-and-braces for one that does;
    both are no-ops when there is nothing left to remove.
    """
    disconnect = getattr(mcp_client, "disconnect", None)
    if disconnect is not None:
        try:
            result = await disconnect(server_name)
        except Exception as exc:
            shared.CONSOLE.print(
                f"[yellow]Plugin reload: MCP disconnect failed ({server_name}): "
                f"{exc}[/yellow]"
            )
        else:
            if not result.get("ok", True):
                # Not fatal -- the reload's job is the tree, not this server -- but
                # an operator reading the log has to be able to see it.
                shared.CONSOLE.print(
                    f"[yellow]Plugin reload: MCP server {server_name!r} was not "
                    f"stopped: {result.get('error', 'unknown error')}[/yellow]"
                )
    if registry is not None and hasattr(registry, "unregister_by_source_prefix"):
        registry.unregister_by_source_prefix(f"mcp:{server_name}")


class ReloadTransaction:
    """One ``PluginCatalog.reload`` call, with its snapshot and its rollback.

    Constructed per call and thrown away: holding a snapshot between calls would be a
    second source of truth about the tree, which is what the snapshot exists to avoid.
    """

    def __init__(self, catalog: "PluginCatalog") -> None:
        self._catalog = catalog
        #: Whether ``discover_and_load`` ran to completion.  Decides what a failure means:
        #: after discovery the tree is known and only the registries need putting back;
        #: during it, nothing about the tree can be said.
        self._discovered = False

    async def run(self, components: Any) -> dict:
        """Re-scan the plugin directories and apply the diff.  Returns a summary.

        The caller hands over the runtime's components on every reload, so this is where
        the container gets bound -- doing it here rather than asking every caller to
        remember a second call keeps the API: a reload with components is a reload that
        can resolve them.
        """
        catalog = self._catalog
        if components is not None:
            catalog.register_core_services(components)

        before = self._snapshot()
        try:
            return await self._apply(before)
        except Exception as exc:
            self._restore(before, exc)
            raise

    # -- the transaction -----------------------------------------------------

    async def _apply(self, before: dict) -> dict:
        """Unload everything, re-discover, and wire up what the new tree implies.

        Unloading **all** plugins rather than only the ones that disappeared is
        deliberate: re-discovery re-creates every plugin from a freshly imported module,
        so each one genuinely becomes a new install and the old install's scope must not
        stay open beside it.  That is also what gives a plugin about to vanish its end
        hook, its scope unwind and its teardown -- without it, eviction was a table
        rebuild that left the plugin's non-registration resources behind.  The MCP
        subprocess that used to be left behind this way is stopped explicitly now, in
        ``_connect_new_mcp``: the process belongs to the MCP client, not to the plugin
        that declared the config, so the reload is the one holding both ends.
        """
        catalog = self._catalog
        old_plugin_names = before["plugin_names"]

        # Re-read the plugin config *before* discovery: ``enabled`` flags and the
        # profile's entries decide what discovery loads, and a reload that skipped
        # them was a reload that ignored the file it was asked to honour.  (The web
        # page's on/off switch worked around exactly that by poking
        # ``catalog._plugin_config`` by hand.)
        composition = catalog.catch_up()
        if composition.get("restart_required"):
            shared.CONSOLE.print(
                "[yellow]Plugin reload: the config changes the composition itself ("
                + "; ".join(composition["restart_required"])
                + ") — restart to apply[/yellow]"
            )

        mcp_registry = catalog._core("contributed_mcp_servers")
        old_mcp = self._mcp_pairs(mcp_registry)

        for name in sorted(old_plugin_names):
            catalog.unload_plugin(name)

        self._discovered = False
        loaded = catalog.discover_and_load()
        self._discovered = True
        new_plugin_names = set(loaded)
        added = new_plugin_names - old_plugin_names
        removed = old_plugin_names - new_plugin_names

        # The command router was handed the catalog; a reload is a new catalog as far as
        # it is concerned.  A failure here is what the rollback exists for.
        command_router = catalog._core("command_router")
        if command_router is not None:
            command_router.register_plugin_catalog(catalog)

        # Re-read the skill catalog: the plugins that bundled skills were unloaded above
        # and re-discovered below, so their roots were taken back and attached again by
        # their own scopes -- this is the intrinsic layers plus a replay of whatever
        # records are attached.  It used to walk the loader's own list of plugin skill
        # paths and poke ``_rebuild_aliases``/``_dirty``/``_prompt_generation`` directly,
        # with a fallback branch for catalogs that lacked them.
        skill_catalog = catalog._core("skill_catalog")
        if skill_catalog is not None:
            try:
                skill_catalog.reload()
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[yellow]Plugin reload: skill catalog refresh failed: {exc}[/yellow]"
                )

        connected = await self._connect_new_mcp(
            mcp_registry, old_mcp, removed, catalog._core("mcp_client"), catalog._core("registry")
        )

        # No re-fire of a session-start signal: `discover_and_load` re-instantiates every
        # plugin and the host runs each one's `setup` again, so a plugin reads the
        # composition anew.  The old signal existed because the hook protocol handed
        # plugins their dependencies *once*, at a moment the reload had to reproduce by
        # hand.
        return {
            "ok": True,
            "added_plugins": sorted(added),
            "removed_plugins": sorted(removed),
            "newly_connected_mcp": connected,
            "total_loaded": len(loaded),
            # What the config asked for, what was applied, and what needs a restart.
            # Reported on success: "reloaded" and "reloaded the parts of the config
            # this process can apply" are different sentences, and the caller cannot
            # tell them apart from ``ok``.
            "composition": composition,
            "restart_required": list(composition.get("restart_required") or ()),
        }

    async def _connect_new_mcp(
        self, mcp_registry: Any, old_mcp: set, removed: set, mcp_client: Any, registry: Any
    ) -> list[str]:
        """Connect newly-declared servers, and **stop** removed plugins' servers.

        Stopping them, not merely unregistering their tools.  A removed plugin's
        stdio child used to outlive the plugin -- until the whole process exited --
        because the reload had no way to close one server; ``disconnect`` is that
        way now.  It signals the server's own task, which unwinds its
        ``AsyncExitStack`` (an AnyIO cancel scope has to be exited by the task that
        entered it, so nothing here may cancel it) and takes back the tools it
        registered on the way out.

        Best-effort, because a reload must not fail over a server that will not
        die: a client that cannot ``disconnect`` (a substitution, an older
        composition) still gets the tool rows taken back by prefix, which is all
        the reload could ever do.
        """
        # The **records**, not ``_mcp_pairs``: that helper answers "which server, whose?"
        # as a set of ``(owner, name)`` keys for the membership test, and unpacking it as
        # ``(owner, config)`` is how this line first shipped -- a string where a config
        # belongs, caught by the reload's own rollback.
        new_servers = [
            config
            for owner, config in (mcp_registry.records() if mcp_registry is not None else ())
            if (owner, config.get("name", "?")) not in old_mcp
        ]
        connected: list[str] = []
        if new_servers and mcp_client is not None:
            try:
                await mcp_client.connect_from_config({"mcp_servers": new_servers})
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[yellow]Plugin reload: MCP connect failed: {exc}[/yellow]"
                )
            else:
                # "attempted" and "connected" are different answers, and this list is
                # published as "connected MCP".  A per-server failure inside
                # ``connect_from_config`` is counted in the client's status rather than
                # raised, so the client is asked which ones actually came up.
                connected = [
                    str(config.get("name", "?"))
                    for config in new_servers
                    if _mcp_server_came_up(mcp_client, config)
                ]
        if removed:
            for owner, server_name in sorted(old_mcp):
                if owner in removed:
                    await _stop_mcp_server(mcp_client, str(server_name), registry)
        return connected

    # -- snapshot and rollback ----------------------------------------------

    def _snapshot(self) -> dict:
        catalog = self._catalog
        return {
            "plugin_names": set(catalog._plugins.keys()),
            # **Copies**, all of them, and restored in place below: a snapshot that aliases
            # the live object is not a snapshot.
            "plugins": dict(catalog._plugins),
            "plugin_source_dirs": dict(catalog._plugin_source_dirs),
            # One snapshot per registry, because each holds a record's handler, metadata
            # and owner together.  The rollback used to restore three dicts separately,
            # which is three chances to restore two of them.
            "slash_commands": catalog._commands.snapshot(),
            "agent_defs": catalog._agent_defs.snapshot(),
        }

    def _restore(self, before: dict, cause: BaseException) -> None:
        """Put the registries this transaction owns back, **in place**.

        In place, not by rebinding: the hook dispatcher holds the live plugin table by
        reference, so the old ``self._plugins = snapshot`` left it dispatching to the dict
        that had just been replaced.

        And registries only -- **not** a re-discovery.  The first version of this
        re-activated the plugins, on the theory that a restored table would otherwise list
        plugins whose services had been unloaded.  That is true, and re-discovery is still
        the wrong answer: it reads the *disk* again, and the usual reason a reload fails is
        that the disk now holds something invalid, so the rollback would fail the same way
        and leave nothing.  Measured on
        ``test_reload_validation_failure_rolls_back_catalog_and_router``: a command file
        with a duplicate alias, where re-discovery raised identically and emptied the
        catalog.

        So the honest statement of what a reload guarantees: it is **atomic with respect to
        the two registries the loader owns**, and *not* atomic with respect to plugin
        activation -- a failure after discovery leaves the newly discovered tree loaded
        with the previous registries, which is a working composition that says so.

        If discovery itself raised, nothing can be said about the tree, so it is unloaded
        and the catalog reports itself empty rather than listing plugins it may not have.
        """
        catalog = self._catalog
        logger.exception("plugin reload failed; restoring the registries", exc_info=cause)
        shared.CONSOLE.print(
            f"[yellow]Plugin reload failed ({cause}); restoring the previous "
            f"registries[/yellow]"
        )
        catalog._commands.restore(before["slash_commands"])
        catalog._agent_defs.restore(before["agent_defs"])
        if self._discovered:
            return
        shared.CONSOLE.print(
            "[red]Plugin reload failed during discovery; the plugin tree is not in a "
            "known state and has been unloaded[/red]"
        )
        for name in list(catalog._plugins):
            catalog.unload_plugin(name)
        catalog._plugin_source_dirs.clear()

    @staticmethod
    def _mcp_pairs(mcp_registry: Any) -> set:
        """``(owner, server name)`` pairs, or nothing when no registry is wired."""
        if mcp_registry is None:
            return set()
        return {
            (owner, cfg.get("name", "?")) for owner, cfg in mcp_registry.records()
        }
