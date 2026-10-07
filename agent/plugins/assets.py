"""What a plugin contributes, as a service rather than as methods on the context.

Why this is a service
---------------------
``add_slash_command`` and its siblings used to be methods on the context every
plugin was handed.  That put contribution vocabulary *in the plugin interface*, so
the kernel's context had to know what a slash command is -- which is the coupling
the kernel exists to avoid.  As a service, the catalog provides it, a plugin that
wants it declares ``inject = ("plugin.assets",)``, and the kernel never learns the
word.

Why it is reachable without declaring anything
----------------------------------------------
Because gating on ``inject`` cannot work here: a service provided *during* a
plugin's own setup cannot satisfy an injection checked *before* that setup.  The
catalog therefore provides this service to each plugin just before calling its
``setup``, and a plugin reaches it as ``ctx.assets`` -- which is the injected-name
shortcut, not an injection test, so nothing is gated.

Registration is an effect
-------------------------
Every method registers its own undo on the calling plugin's lifetime, so the
catalog no longer removes a plugin's contributions by filtering its lists on
plugin name.  That filtering is what a scope replaces: it is the "who unregisters
this?" bookkeeping, and it is 10 lines that have to be revisited whenever a sixth
kind of contribution is added.

The owner is recorded, not inferred
-----------------------------------
Each contribution is still attributed to its plugin -- the catalog reports which
plugin owns a conflicting slash command, and the skill loader labels a root
``plugin:<name>``.  The difference is that attribution is now for *reporting*
rather than for *cleanup*: nothing has to match the name to undo the contribution.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["PluginAssets"]


class PluginAssets:
    """The contribution service the plugin catalog provides.

    A singleton in the registry; reached as ``ctx.assets``, which resolves to a
    :class:`_ScopedAssets` bound to the calling plugin.
    """

    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog

    def scoped(self, ctx: Any) -> "_ScopedAssets":
        """The view a plugin uses.  Called by the kernel, not by plugins."""
        return _ScopedAssets(self._catalog, ctx)

    # The catalog's own view of itself, for its declarative loader: that path
    # registers assets on behalf of a plugin discovered from files, and has no
    # context to register an undo on -- it runs before any plugin exists.
    @property
    def catalog(self) -> Any:
        return self._catalog


class _ScopedAssets:
    """One plugin's handle on the catalog's contribution lists.

    Every method writes the catalog's list **and** registers the matching removal
    on the caller's lifetime, in one place, so the two cannot disagree -- the
    failure that made the old name-filtering version fragile: a contribution added
    without a matching filter is one that survives its plugin.
    """

    __slots__ = ("_catalog", "_ctx")

    def __init__(self, catalog: Any, ctx: Any) -> None:
        self._catalog = catalog
        self._ctx = ctx

    # -- identity ------------------------------------------------------------

    @property
    def owner(self) -> str:
        """The plugin these contributions belong to, for reporting."""
        return self._ctx.name

    # -- contributions -------------------------------------------------------

    def add_slash_command(self, name: str, handler: Callable[..., Any]) -> str:
        """Contribute one slash command, removed with the plugin."""
        from agent.plugins.catalog import _command_metadata

        catalog = self._catalog
        catalog._slash_commands[name] = handler
        catalog._slash_command_metadata[name] = _command_metadata(handler)
        catalog._slash_command_owners[name] = self.owner

        def forget() -> None:
            catalog._slash_commands.pop(name, None)
            catalog._slash_command_metadata.pop(name, None)
            catalog._slash_command_owners.pop(name, None)

        self._ctx.on_unwind(forget)
        return name

    def add_bundled_skill(self, path: Any) -> None:
        """Contribute a skills root, scanned by the skill catalog."""
        entry = (self.owner, path)
        self._catalog._bundled_skills.append(entry)
        self._ctx.on_unwind(lambda: _drop_identity(self._catalog._bundled_skills, entry))

    def add_bundled_mcp(self, config: dict) -> None:
        """Contribute one MCP server configuration."""
        entry = (self.owner, config)
        self._catalog._bundled_mcp.append(entry)
        self._ctx.on_unwind(lambda: _drop_identity(self._catalog._bundled_mcp, entry))

    def add_agent_definition(self, name: str, definition: dict) -> None:
        """Contribute one sub-agent role, keyed the way the catalog reads it."""
        catalog = self._catalog
        key = f"plugin:{self.owner}:{name}"
        catalog._agent_defs[key] = {**definition, "plugin": self.owner}
        self._ctx.on_unwind(lambda: catalog._agent_defs.pop(key, None))

    def add_tool(self, tool: Any) -> Any:
        """Contribute one tool, if the composition mounted a registry.

        Absent one this is a no-op rather than an error: a plugin must stay
        loadable in a composition that does not mount tools, which is the same
        reason every other optional capability in this codebase degrades quietly.
        """
        registry = getattr(self._catalog, "_tool_registry", None)
        if registry is None:
            return None
        source = f"plugin:{self.owner}"
        registry.register(
            getattr(tool, "name", None),
            getattr(tool, "description", "") or "",
            getattr(tool, "parameters", {}) or {"type": "object", "properties": {}},
            getattr(tool, "fn", tool),
            source=source,
            capabilities=getattr(tool, "capabilities", None),
            replace=True,
        )
        # By source prefix rather than by name: one plugin may contribute several
        # tools, and the prefix is what identifies them as a group.
        self._ctx.on_unwind(lambda: registry.unregister_by_source_prefix(source))
        return None


def _drop_identity(items: list, entry: Any) -> None:
    """Remove *entry* from *items* by identity.

    By identity, not equality: two plugins may contribute an equal-looking config,
    and removing the wrong one would silently disable a plugin that is still
    loaded.
    """
    for index, item in enumerate(items):
        if item is entry:
            del items[index]
            return
