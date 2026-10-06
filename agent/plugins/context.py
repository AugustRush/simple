"""What a plugin is handed when it is loaded, and what it must give back.

The problem this solves
-----------------------
A plugin could contribute five kinds of thing -- slash commands, skills, MCP
servers, agent definitions, and (since Task 3) tools -- and each was registered
into a different structure by a different code path.  Unloading meant knowing
all five, and the tree did not: eviction rebuilt every registry from scratch and
hoped, while two documented leaks stayed open.  A removed plugin's MCP
subprocess could not be disconnected, and ``on_session_end`` did not fire on
eviction, so a plugin had no moment at which it could release anything that was
not a registry entry.

The shape here is the one the rest of the refactor settled on: registration is
an effect, and the effect's owner unwinds it.  ``PluginContext`` writes through a
:class:`~agent.runtime.services.ServiceScope`, so a contribution made through it
disappears when the scope does, and the catalog never has to enumerate the kinds.

Two halves, deliberately
------------------------
``setup(ctx)`` is the registration half, and ``teardown()`` is the escape hatch
for what a scope cannot express -- closing a subprocess handle, flushing a file.
The escape hatch is not a failure of the design: a scope can only undo things
that were registered *through* it, and a plugin that started something else has
to say so itself.  What changed is that it now has a moment to say it in.

Order
-----
``on_session_end`` fires first, then the scope unwinds, then ``teardown``.  The
hook runs while the plugin's registrations still exist, because a hook that
cannot see its own resources cannot clean them up; and ``teardown`` runs last
because it is the code most likely to depend on the plugin still being whole.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

#: Distinguishes the absence of a default from a default of None.
_MISSING = object()

__all__ = ["PluginContext", "PluginInstall"]


class PluginContext:
    """The registration surface handed to a plugin's ``setup``.

    Every method records its own undo on the install's scope, so the catalog
    never needs to know how many kinds of contribution exist.  Adding a sixth
    kind later means adding a method here, not editing an eviction path.
    """

    __slots__ = ("_install",)

    def __init__(self, install: "PluginInstall") -> None:
        self._install = install

    # -- generic escape hatch ------------------------------------------------

    def on_unwind(self, handler: Callable[[], None]) -> None:
        """Register an arbitrary teardown action, run when the plugin unloads.

        The general form.  A plugin with a resource that is not a registry
        entry -- a thread, a connection, a temp file -- does not have to wait
        for the next release to get a hook for it.
        """
        self._install.scope.on_unwind(handler)

    # -- services, which is what makes this declarative ---------------------

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        """Offer *name* to other plugins and to the runtime.

        The registration primitive already existed (``ServiceScope``); this is
        the way a plugin reaches it.  The service is unwound with the plugin, and
        any other plugin can resolve it through
        :meth:`PluginCatalog.resolve_service` -- which is the difference between
        a plugin that fills a slot the catalog knew about and a plugin that hands
        something to its neighbours.
        """
        scope = self._install.scope
        if scope is None:  # pragma: no cover - installs always carry a scope
            raise RuntimeError(
                f"plugin {self._install.name!r} has no scope to register into"
            )
        scope.provide(name, value, replace=replace)
        # Only a *newly offered* service can unblock a waiting plugin, so the
        # catalog is told here rather than re-checking after every load.  The
        # flag is read and cleared by the catalog once setup returns, so a
        # provider registers and unblocks in one pass.
        self._install.provided = True
        if name not in self._install.provided_names:
            self._install.provided_names.append(name)

    def resolve(self, name: str, default: Any = _MISSING) -> Any:
        """Resolve a service another plugin provided.

        Reads through the catalog rather than this install's scope, so a plugin
        sees services from plugins loaded before it -- not only its own.
        """
        if default is _MISSING:
            return self._install.catalog.resolve_service(name)
        return self._install.catalog.resolve_service(name, default)

    # -- the five kinds of contribution --------------------------------------

    def add_slash_command(self, name: str, handler: Callable[..., Any]) -> None:
        """Contribute one slash command, removed with the plugin."""
        catalog = self._install.catalog
        catalog._slash_commands[name] = handler
        catalog._slash_command_metadata[name] = _command_metadata(handler)
        catalog._slash_command_owners[name] = self._install.name
        self.on_unwind(lambda: _forget_command(catalog, name))

    def add_bundled_skill(self, path: Any) -> None:
        """Contribute a skills root, scanned by the skill catalog."""
        catalog = self._install.catalog
        entry = (self._install.name, path)
        catalog._bundled_skills.append(entry)
        self.on_unwind(lambda: _drop_identity(catalog._bundled_skills, entry))

    def add_bundled_mcp(self, config: dict) -> None:
        """Contribute one MCP server configuration."""
        catalog = self._install.catalog
        entry = (self._install.name, config)
        catalog._bundled_mcp.append(entry)
        self.on_unwind(lambda: _drop_identity(catalog._bundled_mcp, entry))

    def add_agent_definition(self, name: str, definition: dict) -> None:
        """Contribute one sub-agent role, keyed the way the catalog reads it."""
        catalog = self._install.catalog
        key = f"plugin:{self._install.name}:{name}"
        catalog._agent_defs[key] = {**definition, "plugin": self._install.name}
        self.on_unwind(lambda: catalog._agent_defs.pop(key, None))

    def add_tool(self, tool: Any) -> None:
        """Contribute one tool, if the catalog has a registry to add it to.

        Tools were the contribution a plugin could not make at all before
        Task 3: the plugin catalog never touched the tool registry, and tool
        providers were a separate mechanism.  Absent a registry this is a
        no-op rather than an error, because a plugin must stay loadable in a
        composition that does not mount tools.
        """
        registry = getattr(self._install.catalog, "_tool_registry", None)
        if registry is None:
            return
        name = getattr(tool, "name", None)
        source = f"plugin:{self._install.name}"
        registry.register(
            name,
            getattr(tool, "description", "") or "",
            getattr(tool, "parameters", {}) or {"type": "object", "properties": {}},
            getattr(tool, "fn", tool),
            source=source,
            capabilities=getattr(tool, "capabilities", None),
            replace=True,
        )
        self.on_unwind(lambda: registry.unregister_by_source_prefix(source))


def _command_metadata(handler: Callable[..., Any]) -> dict:
    """Command metadata for one plugin-provided handler.

    Imported lazily from the catalog because the catalog owns the vocabulary;
    duplicating the shape here would let the two drift.
    """
    from agent.plugins.catalog import _command_metadata as build

    return build(handler)


def _forget_command(catalog: Any, name: str) -> None:
    catalog._slash_commands.pop(name, None)
    catalog._slash_command_metadata.pop(name, None)
    catalog._slash_command_owners.pop(name, None)


def _drop_identity(items: list, entry: tuple) -> None:
    """Remove *entry* from *items* by identity, not by value.

    By identity because two plugins may legitimately contribute equal-looking
    entries -- the same MCP command under the same name -- and a value-based
    removal would take the other plugin's with it.
    """
    for index, item in enumerate(items):
        if item is entry:
            del items[index]
            return


class PluginInstall:
    """One loaded plugin: its object, its scope, and its catalog.

    ``inject`` is what the plugin declared it needs; ``pending`` is whether it is
    waiting for those to exist.  A pending install has run no ``setup``, so it
    holds no registrations and its scope is empty -- which is what makes
    deferring safe rather than a half-loaded state.
    """

    __slots__ = (
        "name",
        "plugin",
        "meta",
        "catalog",
        "scope",
        "ctx",
        "unloaded",
        "inject",
        "pending",
        "provided",
        "provided_names",
    )

    def __init__(
        self,
        name: str,
        plugin: Any,
        meta: Any,
        catalog: Any,
        *,
        scope: Optional[ServiceScope] = None,
    ) -> None:
        # Imported here, not at module scope: agent.runtime imports contracts,
        # which imports the skill catalog, which imports the tool registry,
        # which imports agent.plugins.catalog -- and this module is imported by
        # it.  Deferring breaks the cycle at the point where the symbol is
        # genuinely needed.
        from agent.runtime.services import ServiceScope

        self.name = name
        self.plugin = plugin
        self.meta = meta
        self.catalog = catalog
        self.scope = scope if scope is not None else ServiceScope(_NullRegistry())
        self.ctx = PluginContext(self)
        self.unloaded = False
        #: Required service names, from the plugin object's ``inject``.
        self.inject: tuple[str, ...] = tuple(
            str(item) for item in (getattr(plugin, "inject", ()) or ())
        )
        #: True until every injected name resolves.
        self.pending: bool = False
        #: Set by ``ctx.provide`` during this install's setup; read and cleared
        #: by the catalog to decide whether waiting plugins should be re-checked.
        self.provided: bool = False
        #: Names this install offered, so unloading it can find its dependents
        #: without a second record that could drift from the scope.
        self.provided_names: list[str] = []


class _NullRegistry:
    """A throwaway registration sink.

    A :class:`ServiceScope` needs an owner to build restore actions against.
    This one is only used by an install whose plugin never asked for a scope,
    so nothing ever reads it back; it exists to keep ``PluginInstall``
    constructible without a catalog-wide registry.
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._open_scopes: list[Any] = []

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        self._services[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        return self._services.get(name, default)

    def has(self, name: str) -> bool:
        return name in self._services

    def names(self) -> tuple[str, ...]:
        return tuple(self._services)

    def _capture_previous(self, name: str) -> Callable[[], None]:
        had = name in self._services
        previous = self._services.get(name)

        def restore() -> None:
            if had:
                self._services[name] = previous
            else:
                self._services.pop(name, None)

        return restore
