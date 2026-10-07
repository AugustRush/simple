"""The context a plugin is handed, and the whole of its vocabulary.

Six methods, and the plan's sentence is the specification: *let plugins be
composed, depend on each other, provide services, listen to events, and be
unloaded or replaced.*

``plugin(p)``
    Load another plugin.  Its install is recorded on this context's scope, so it is
    unloaded with the plugin that loaded it.
``provide(name, value)``
    Offer a service for the lifetime of the scope.  Registered as an undo, so it
    disappears when the scope does.
``get(name, default)``
    Resolve a service.  With no default a missing name raises -- a silent ``None``
    is how an absent dependency becomes an ``AttributeError`` three frames away
    from the thing that was actually missing.
``on(event_type, handler)``
    Subscribe.  Returns an unsubscribe callable *and* records it on the scope, so
    unloading cannot leave a listener behind.
``emit(event)``
    Notify listeners of an event's type.
``scope()``
    A nested scope, for a plugin that wants to register something it can reverse
    earlier than its own unload.

What is deliberately missing
----------------------------
No ``ctx.agent``, ``ctx.tools``, ``ctx.sessions``.  Those are services, reached
through ``get`` (or, better, declared in ``inject`` and reached as attributes --
see below).  A named accessor per service would put the service vocabulary in the
kernel, which is exactly the coupling this design removes.

The one convenience: injected names become attributes
-----------------------------------------------------
A plugin that declares ``inject = ("tools",)`` gets ``ctx.tools``.  That is not a
privileged name -- it is the same ``get`` under the declared name -- and it exists
because the alternative is the "grab the whole runtime and drill down" pattern the
plan calls out by name:

    plugin.runtime.agent.memory.tool_registry   # no
    ctx.tools                                   # yes
"""

from __future__ import annotations

from typing import Any, Callable

from agent.kernel.service import (
    MissingServiceError,
    ServiceRegistry,
    ServiceScope,
)

_MISSING = object()

__all__ = ["Context"]


class Context:
    """A plugin's handle on the kernel.

    Bound to one :class:`~agent.kernel.plugin.Install`: its scope is where every
    registration goes, and its name is what a diagnostic reports.
    """

    __slots__ = ("_host", "_install", "_extra")

    def __init__(self, *, host: Any, install: Any) -> None:
        self._host = host
        self._install = install
        #: Names the injectable-attribute shortcut should not shadow.  A service
        #: called ``get`` would otherwise replace this class's own method.
        self._extra = frozenset(Context.__slots__) | frozenset(
            n for n in dir(Context) if not n.startswith("__")
        )

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._install.name

    @property
    def services(self) -> ServiceRegistry:
        """The registry behind ``get``, for a caller that needs membership tests."""
        return self._host.services

    # -- services ------------------------------------------------------------

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        """Offer *name* to the whole composition, for as long as this plugin lives.

        Recorded on the scope, so unloading removes it and dependents waiting on it
        are taken down with it.
        """
        self._install.scope.provide(name, value, replace=replace)
        # Told, not inferred: only a *newly* offered service can unblock a waiter,
        # so the host re-checks pending plugins after a setup that provided one --
        # rather than after every load, which would activate a plugin the moment an
        # unrelated plugin appeared.
        self._install.provided = True
        if name not in self._install.provided_names:
            self._install.provided_names.append(name)

    def get(self, name: str, default: Any = _MISSING) -> Any:
        """Resolve a service, raising for a missing name unless given a default."""
        if default is _MISSING:
            return self.services.get(name)
        return self.services.get(name, default)

    def has(self, name: str) -> bool:
        return self.services.has(name)

    # -- events --------------------------------------------------------------

    def on(self, event_type: type, handler: Callable[[Any], Any]) -> Callable[[], None]:
        """Subscribe *handler* to *event_type*; undone when this plugin unloads."""
        unsubscribe = self._host.events.on(event_type, handler)
        self._install.scope.on_unwind(unsubscribe)
        return unsubscribe

    async def emit(self, event: Any) -> Any:
        """Notify listeners of ``type(event)``."""
        return await self._host.events.emit(event)

    async def emit_blocking(self, event: Any) -> Any:
        """Notify listeners until one refuses; see ``EventBus.emit_blocking``."""
        return await self._host.events.emit_blocking(event)

    # -- composition ---------------------------------------------------------

    def plugin(self, plugin: Any) -> Any:
        """Load *plugin* as a child of this one.

        The child's install is recorded on this plugin's scope, so unloading the
        parent disposes the child: a cascade that is a consequence of the disposal
        rule rather than a feature layered on top of it.
        """
        install = self._host.load(plugin, parent=self._install.name)
        self._install.scope.on_unwind(lambda: self._unload_child(install.name))
        return install

    def _unload_child(self, name: str) -> None:
        """Remove a child that is still loaded.  Safe to call after it has gone."""
        if name in self._host.installs or name in self._host.pending():
            self._host.unload(name)

    # -- scopes --------------------------------------------------------------

    def scope(self) -> ServiceScope:
        """A nested scope, reversible before this plugin unloads."""
        return self.services.scope()

    # -- injected attributes -------------------------------------------------

    def __getattr__(self, item: str) -> Any:
        """``ctx.tools`` for a plugin that declared ``inject = ("tools",)``.

        Only reached when normal attribute lookup fails, so a service cannot
        shadow a real method.  A name that was neither injected nor provided raises,
        which is the point: reaching for a service you did not declare is the
        coupling ``inject`` exists to make visible.
        """
        if item.startswith("_") or item in self._extra:
            raise AttributeError(item)
        if item in self._install.inject:
            try:
                return self.services.get(item)
            except MissingServiceError as exc:
                raise AttributeError(
                    f"{self._install.name!r} injected {item!r} but it is not "
                    f"available; the plugin should not have activated"
                ) from exc
        raise AttributeError(
            f"{item!r} is not a method of Context and {self._install.name!r} did "
            f"not declare it in inject; add it to inject to use it as an attribute"
        )
